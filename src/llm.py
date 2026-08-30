"""Claude vision + agentic tool loop.

Each Discord (or voice) turn calls `ask_claude(text, capture, move,
go_to_pose, look_at, propose_edit)`. The function runs an agentic loop with
seven client-side tools:

- `look` — capture a fresh photo without moving.
- `go_to_pose` — snap to a named preset (home, look_at_hands, look_down,
  look_up, scan_left, scan_right). Faster and more reliable than reasoning
  out 6 joint angles for common scenarios.
- `look_at` — aim the camera at a Cartesian point in the room. Backed by
  src.kinematics; Claude gives a location, not angles.
- `move_arm` — fine-grained joint control. Used to refine after a preset,
  or for poses that don't match a named preset.
- `remember` / `forget` — read/write documentation/LESSONS.md via
  src.memory. No photo, no move_count.
- `propose_doc_edit` — register a pending edit to GOOB.md/MOVEMENT.md for
  the user to approve; the caller (bot.py) owns approval state.

Plus one server-side tool (enabled by config.WEB_SEARCH_ENABLED):

- `web_search` — Anthropic's built-in search. The API handles the fetch
  and citations server-side within one messages.create call; we just add
  the tool to the list and count uses. Costs $10/1k searches on top of
  token cost, so max_uses caps it per turn.

The first turn carries no image — pure chit-chat ("how are you?") costs
zero vision tokens. Claude requests vision only when the prompt actually
needs it. After every tool call the resulting frame is fed back as a
tool_result and the loop continues until Claude returns a non-tool reply
or `max_turns` is hit.

`wrist_r` and `step_delay` are intentionally NOT exposed to Claude:
- wrist_r=90 is locked because Camera.capture_jpeg does a 180° rotation
  to compensate for the upside-down mount, and that compensation only
  stays correct at the baseline wrist roll.
- step_delay defaults to 10 ms (fastest) inside ArmController; Claude
  never benefits from slowing down deliberately.

Personality lives in `documentation/GOOB.md`; arm kinematics live in
`documentation/MOVEMENT.md`; bot-writable memory lives in
`documentation/LESSONS.md`. `get_system_blocks()` re-reads all three
whenever any of their mtimes change, so edits take effect next turn without
a process restart.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import anthropic

from src import config, memory
from src.arm import POSES

logger = logging.getLogger(__name__)

LOOK_TOOL = {
    "name": "look",
    "description": (
        "Take a fresh photo from your camera without moving. Use this when "
        "the user asks about something visual, asks what you see, or you "
        "otherwise need eyes on the room before answering. Returns the "
        "current photo as the tool result."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

GO_TO_POSE_TOOL = {
    "name": "go_to_pose",
    "description": (
        "Snap to a named preset pose, then return a fresh photo of the new "
        "view. Faster and more reliable than reasoning out joint angles. "
        "Available presets:\n"
        "  - home: upright, looking forward (use before final answers)\n"
        "  - look_at_hands: angled toward the user's chest/desk (good first "
        "    move when asked about hands or held objects)\n"
        "  - look_down: top-down view of the desk in front of the arm\n"
        "  - look_up: angled upward toward ceiling/face\n"
        "  - scan_left: panned to the user's left\n"
        "  - scan_right: panned to the user's right\n"
        "After a preset, refine with move_arm if the subject isn't centered."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pose": {
                "type": "string",
                "enum": sorted(POSES.keys()),
                "description": "Name of the preset pose to move to.",
            },
        },
        "required": ["pose"],
    },
}

MOVE_ARM_TOOL = {
    "name": "move_arm",
    "description": (
        "Fine-grained joint control. Use this when no preset fits, or to "
        "refine a pose after go_to_pose. Returns a fresh photo of the new "
        "view.\n\n"
        "DIRECTION CONVENTIONS — read carefully, these are mirrored:\n"
        "- USER says 'right' or 'look right' → swing toward the user's "
        "right side → INCREASE `base` toward 180. The user is facing you, "
        "so their right is your physical left.\n"
        "- USER says 'left' or 'look left' → DECREASE `base` toward 0.\n"
        "- IMAGE-axis centering (subject visible in the current frame): "
        "subject on the IMAGE'S right → DECREASE `base`; image's left → "
        "INCREASE `base`. (The image's right is the user's left because "
        "the camera mirrors them, like a webcam.)\n"
        "- Subject at IMAGE'S bottom → tilt down: DECREASE `wrist_v` or "
        "  `shoulder`. Top → tilt up: INCREASE `wrist_v`.\n"
        "When in doubt about user-language right/left, default to the "
        "user-perspective rule above."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "base":     {"type": "integer", "minimum": 0,  "maximum": 180,
                         "description": (
                             "Pan. 90 = forward (toward user). "
                             "180 = swing to the USER'S right side. "
                             "0 = swing to the USER'S left side."
                         )},
            "shoulder": {"type": "integer", "minimum": 15, "maximum": 165,
                         "description": "Shoulder pitch. 90 = upright; lower leans forward; higher leans back."},
            "elbow":    {"type": "integer", "minimum": 0,  "maximum": 180,
                         "description": "Elbow fold. 90 = straight; higher folds the arm back over itself."},
            "wrist_v":  {"type": "integer", "minimum": 0,  "maximum": 180,
                         "description": "Camera tilt relative to forearm. 90 = level; lower tilts down; higher tilts up."},
            "wrist_r":  {"type": "integer", "minimum": 0,  "maximum": 180,
                         "description": (
                             "Wrist roll (camera spin around the gripper's "
                             "axis). 90 = upright baseline. Set to other "
                             "values for personality moves like spinning the "
                             "camera. The image is rotated back to upright "
                             "automatically, so subsequent analysis still "
                             "works at any wrist_r value. Default 90 if "
                             "you're not deliberately spinning."
                         )},
        },
        "required": ["base", "shoulder", "elbow", "wrist_v"],
    },
}

LOOK_AT_TOOL = {
    "name": "look_at",
    "description": (
        "Aim your camera at a point in the room and return a fresh photo. "
        "You give a location, not joint angles — inverse kinematics works "
        "out the pose and backs the camera off to a sensible viewing "
        "distance. Prefer this over move_arm whenever you can describe "
        "WHERE something is.\n\n"
        "The frame is centred on your base, on the desk, and described from "
        "the USER'S point of view (they're facing you):\n"
        "- x_cm: sideways. Negative = the user's LEFT, positive = the user's "
        "RIGHT, 0 = straight ahead. (Their right is your physical left — the "
        "tool handles the mirror, just say where the thing is from their "
        "side.)\n"
        "- y_cm: how far out from your base, toward the user. Always "
        "positive; you can't look behind yourself.\n"
        "- z_cm: height above the desk surface. 0 = the desk itself.\n\n"
        "Anchors to calibrate against:\n"
        "- the user's hands, seated at the desk: about (0, 35, 25)\n"
        "- the desk surface right in front of you: about (0, 20, 5)\n"
        "- the user's face: about (0, 45, 45)\n"
        "- a mug to the user's right on the desk: about (20, 25, 5)\n\n"
        "If the point is unreachable you get an error explaining why — "
        "adjust and retry, or fall back to go_to_pose. After looking, "
        "refine with move_arm if the subject isn't centred."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "x_cm": {"type": "number",
                     "description": "Sideways offset. Negative = user's left, positive = user's right."},
            "y_cm": {"type": "number",
                     "description": "Distance out from your base toward the user, in cm. Must be positive."},
            "z_cm": {"type": "number",
                     "description": "Height above the desk in cm. 0 = the desk surface."},
        },
        "required": ["x_cm", "y_cm", "z_cm"],
    },
}


REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Save a permanent memory to documentation/LESSONS.md. Use this when "
        "the user corrects your behavior (kind=lesson, e.g. \"when the user "
        "says X, do Y\") or you learn a stable fact about the room or user "
        "(kind=fact, e.g. room layout, habits, environment quirks). Keep "
        "entries short and general; don't save session trivia or duplicates "
        "of existing memories (your current memories are in your system "
        "prompt under \"Goob memory\")."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["lesson", "fact"],
                "description": "lesson = behavioral correction. fact = stable world fact.",
            },
            "text": {
                "type": "string",
                "description": "The memory to save. Single line, <=200 chars.",
            },
        },
        "required": ["kind", "text"],
    },
}

FORGET_TOOL = {
    "name": "forget",
    "description": (
        "Remove an outdated or wrong memory entry from documentation/"
        "LESSONS.md. Provide a substring that uniquely identifies the entry "
        "to remove."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "match": {
                "type": "string",
                "description": "Substring (case-insensitive) identifying the memory entry to remove.",
            },
        },
        "required": ["match"],
    },
}

PROPOSE_DOC_EDIT_TOOL = {
    "name": "propose_doc_edit",
    "description": (
        "Propose a correction to your own instruction files (GOOB.md or "
        "MOVEMENT.md). This does NOT apply immediately — it registers a "
        "pending proposal. You MUST include the find/replace text and your "
        "reason in your reply so the user can review and approve it by "
        "replying \"yes\" or \"apply it\" within 10 minutes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "enum": ["GOOB.md", "MOVEMENT.md"],
                "description": "Which doc to edit.",
            },
            "find": {
                "type": "string",
                "description": "Exact text to find. Must occur exactly once in the file.",
            },
            "replace": {
                "type": "string",
                "description": "Text to replace it with.",
            },
            "reason": {
                "type": "string",
                "description": "Why this edit is needed.",
            },
        },
        "required": ["file", "find", "replace", "reason"],
    },
}


def _build_tools() -> list[dict]:
    tools: list[dict] = [
        LOOK_TOOL, GO_TO_POSE_TOOL, LOOK_AT_TOOL, MOVE_ARM_TOOL,
        REMEMBER_TOOL, FORGET_TOOL, PROPOSE_DOC_EDIT_TOOL,
    ]
    if config.WEB_SEARCH_ENABLED:
        # Anthropic server-side tool. Executed inside messages.create; we
        # never see a client tool_use for it. max_uses caps searches per
        # turn to bound cost ($10 per 1000 searches).
        tools.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": config.WEB_SEARCH_MAX_USES,
        })
    # Cache the whole tools array as part of the exact-match prefix (system
    # + tools). Only the last block needs the marker — caching covers
    # everything before it too.
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    return tools


TOOLS = _build_tools()
# Log at import so it's obvious on restart which tools Claude actually got.
# `type` is present on server-side tools, `name` on client-side.
logger.info(
    "claude tools registered: %s",
    [t.get("name") or t.get("type") for t in TOOLS],
)


@dataclass
class TurnResult:
    text: str
    move_count: int
    look_count: int
    web_search_count: int
    truncated: bool
    last_jpeg: Optional[bytes]  # None if Claude never looked
    messages: list[dict]  # full transcript after this turn — caller persists


_DOCS = Path(__file__).resolve().parent.parent / "documentation"
_GOOB_PATH = _DOCS / "GOOB.md"
_MOVEMENT_PATH = _DOCS / "MOVEMENT.md"
_LESSONS_PATH = _DOCS / "LESSONS.md"

_system_cache_key: Optional[tuple] = None
_system_cache_blocks: Optional[list[dict]] = None


def _doc_stat_key(path: Path) -> tuple:
    try:
        return (True, path.stat().st_mtime_ns)
    except FileNotFoundError:
        return (False, 0)


def _load_system_prompt() -> str:
    # GOOB.md is required (personality + behavior). MOVEMENT.md and
    # LESSONS.md are optional; if present they append a Braccio kinematics
    # guide and bot-writable memory respectively, in that order, so Claude
    # knows how the joints combine and what it has already learned.
    parts = [_GOOB_PATH.read_text(encoding="utf-8")]
    for path in (_MOVEMENT_PATH, _LESSONS_PATH):
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def get_system_blocks() -> list[dict]:
    # mtime-cached: memory writes and applied doc-edit proposals should take
    # effect on the very next turn without a process restart, but a turn
    # that doesn't touch any doc shouldn't pay for a re-read + fresh 1h
    # cache-write on every single call.
    global _system_cache_key, _system_cache_blocks
    key = (
        _doc_stat_key(_GOOB_PATH),
        _doc_stat_key(_MOVEMENT_PATH),
        _doc_stat_key(_LESSONS_PATH),
    )
    if key != _system_cache_key or _system_cache_blocks is None:
        _system_cache_blocks = [
            {
                "type": "text",
                "text": _load_system_prompt(),
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]
        _system_cache_key = key
    return _system_cache_blocks


_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode("ascii"),
        },
    }


def _final_text(content) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def _strip_cache_control(messages: list[dict]) -> None:
    # prior_messages (session history) may carry a stale breakpoint from a
    # previous turn's last message, now buried mid-transcript. Clear all of
    # them so only the new moving breakpoint below is set.
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block.pop("cache_control", None)


def _mark_moving_breakpoint(messages: list[dict]) -> None:
    # Cache prefix breakpoint on the last content block of the last message,
    # so the growing agentic transcript gets incremental hits round-to-round.
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
        last["content"] = content
    if not isinstance(content, list) or not content:
        return
    block = content[-1]
    if isinstance(block, dict):
        block["cache_control"] = {"type": "ephemeral"}


def ask_claude(
    user_text: str,
    capture: Callable[[], bytes],
    move: Callable[..., None],
    go_to_pose: Callable[[str], None],
    look_at: Callable[[float, float, float], None],
    propose_edit: Callable[[str, str, str, str], str],
    prior_messages: Optional[list[dict]] = None,
    max_turns: int = 12,
) -> TurnResult:
    # max_turns is LLM rounds, not tool calls. Claude can call multiple
    # tools per turn, so 12 rounds easily supports 10+ moves/looks of
    # iteration when the prompt encourages it.
    #
    # prior_messages is the transcript from previous turns in the same
    # session. Pass None (or []) for a fresh conversation. Caller is
    # responsible for image-trimming and idle-expiry.
    client = _get_client()
    system_blocks = get_system_blocks()  # one snapshot per turn, not per round
    messages: list[dict] = list(prior_messages) if prior_messages else []
    messages.append({"role": "user", "content": user_text or "(no text)"})
    move_count = 0
    look_count = 0
    web_search_count = 0
    last_jpeg: Optional[bytes] = None
    last_response = None

    for turn in range(max_turns):
        _strip_cache_control(messages)
        _mark_moving_breakpoint(messages)
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=system_blocks,
            tools=TOOLS,
            messages=messages,
        )
        last_response = response
        usage = response.usage
        logger.info(
            "claude turn %d: stop=%s in=%d out=%d cache_read=%d cache_creation=%d",
            turn, response.stop_reason, usage.input_tokens, usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

        # Count server-side tool uses (web_search) so the UI can report them.
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
                web_search_count += 1

        messages.append(
            {"role": "assistant", "content": [b.model_dump() for b in response.content]}
        )

        # Exit only on a real completion. "tool_use" means client-side tool
        # follow-up needed; "pause_turn" means a server tool paused and we
        # should re-invoke without adding a user message.
        if response.stop_reason not in ("tool_use", "pause_turn"):
            return TurnResult(
                text=_final_text(response.content),
                move_count=move_count,
                look_count=look_count,
                web_search_count=web_search_count,
                truncated=False,
                last_jpeg=last_jpeg,
                messages=messages,
            )

        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "look":
                try:
                    last_jpeg = capture()
                    look_count += 1
                except Exception as exc:
                    logger.warning("look failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"camera capture failed: {exc}",
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [
                        {"type": "text", "text": "current view from the camera:"},
                        _image_block(last_jpeg),
                    ],
                })
            elif block.name == "move_arm":
                args = dict(block.input)
                logger.info("agentic move %d: %s", move_count + 1, args)
                try:
                    # wrist_r, gripper, step_delay default inside ArmController.
                    move(**args)
                except Exception as exc:
                    logger.warning("move failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"move failed: {exc}",
                        "is_error": True,
                    })
                    continue
                move_count += 1
                try:
                    last_jpeg = capture()
                except Exception as exc:
                    logger.warning("recapture failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"moved but camera recapture failed: {exc}",
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [
                        {"type": "text", "text": "moved; here is the new view from the camera:"},
                        _image_block(last_jpeg),
                    ],
                })
            elif block.name == "look_at":
                args = dict(block.input)
                logger.info("agentic look_at %d: %s", move_count + 1, args)
                try:
                    look_at(
                        float(args["x_cm"]),
                        float(args["y_cm"]),
                        float(args["z_cm"]),
                    )
                except Exception as exc:
                    # Includes KinematicsError for unreachable targets. Its
                    # message is written to be read by Claude, which can then
                    # pick a different point instead of giving up.
                    logger.warning("look_at failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"can't aim there: {exc}",
                        "is_error": True,
                    })
                    continue
                move_count += 1
                try:
                    last_jpeg = capture()
                except Exception as exc:
                    logger.warning("recapture failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"aimed but camera recapture failed: {exc}",
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [
                        {"type": "text", "text": "aimed there; here is the new view:"},
                        _image_block(last_jpeg),
                    ],
                })
            elif block.name == "go_to_pose":
                pose_name = block.input.get("pose", "")
                logger.info("agentic pose %d: %s", move_count + 1, pose_name)
                try:
                    go_to_pose(pose_name)
                except Exception as exc:
                    logger.warning("pose failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"pose failed: {exc}",
                        "is_error": True,
                    })
                    continue
                move_count += 1
                try:
                    last_jpeg = capture()
                except Exception as exc:
                    logger.warning("recapture failed: %s", exc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"posed but camera recapture failed: {exc}",
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [
                        {"type": "text", "text": f"moved to {pose_name}; here is the new view:"},
                        _image_block(last_jpeg),
                    ],
                })
            elif block.name == "remember":
                args = dict(block.input)
                try:
                    result_text = memory.remember(args.get("kind", ""), args.get("text", ""))
                except Exception as exc:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(exc),
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            elif block.name == "forget":
                args = dict(block.input)
                try:
                    result_text = memory.forget(args.get("match", ""))
                except Exception as exc:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(exc),
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            elif block.name == "propose_doc_edit":
                args = dict(block.input)
                try:
                    result_text = propose_edit(
                        args.get("file", ""),
                        args.get("find", ""),
                        args.get("replace", ""),
                        args.get("reason", ""),
                    )
                except Exception as exc:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(exc),
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"unknown tool: {block.name}",
                    "is_error": True,
                })
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        # If tool_results is empty and stop_reason was "pause_turn", the
        # server-side tool has already appended its result to the assistant
        # message. Re-invoke with no new user turn and let Claude continue.

    text = _final_text(last_response.content) if last_response else ""
    if not text:
        text = "(stopped after looking around several times without settling on an answer)"
    return TurnResult(
        text=text,
        move_count=move_count,
        look_count=look_count,
        web_search_count=web_search_count,
        truncated=True,
        last_jpeg=last_jpeg,
        messages=messages,
    )
