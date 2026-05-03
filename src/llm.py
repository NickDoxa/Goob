"""Claude vision + agentic tool loop.

Each Discord (or voice) turn calls `ask_claude(text, capture, move,
go_to_pose)`. The function runs an agentic loop with three tools:

- `look` — capture a fresh photo without moving.
- `go_to_pose` — snap to a named preset (home, look_at_hands, look_down,
  look_up, scan_left, scan_right). Faster and more reliable than reasoning
  out 6 joint angles for common scenarios.
- `move_arm` — fine-grained joint control. Used to refine after a preset,
  or for poses that don't match a named preset.

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
`documentation/MOVEMENT.md`. Both load at module import.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import anthropic

from src import config
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

TOOLS = [LOOK_TOOL, GO_TO_POSE_TOOL, MOVE_ARM_TOOL]


@dataclass
class TurnResult:
    text: str
    move_count: int
    look_count: int
    truncated: bool
    last_jpeg: Optional[bytes]  # None if Claude never looked
    messages: list[dict]  # full transcript after this turn — caller persists


_DOCS = Path(__file__).resolve().parent.parent / "documentation"


def _load_system_prompt() -> str:
    # GOOB.md is required (personality + behavior). MOVEMENT.md is optional;
    # if present it appends a Braccio kinematics guide so Claude knows how
    # the joints combine, not just what each one does in isolation.
    parts = [(_DOCS / "GOOB.md").read_text(encoding="utf-8")]
    movement = _DOCS / "MOVEMENT.md"
    if movement.exists():
        parts.append(movement.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


SYSTEM_PROMPT = _load_system_prompt()

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


def ask_claude(
    user_text: str,
    capture: Callable[[], bytes],
    move: Callable[..., None],
    go_to_pose: Callable[[str], None],
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
    messages: list[dict] = list(prior_messages) if prior_messages else []
    messages.append({"role": "user", "content": user_text or "(no text)"})
    move_count = 0
    look_count = 0
    last_jpeg: Optional[bytes] = None
    last_response = None

    for turn in range(max_turns):
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        last_response = response
        usage = response.usage
        logger.info(
            "claude turn %d: stop=%s in=%d out=%d",
            turn, response.stop_reason, usage.input_tokens, usage.output_tokens,
        )

        messages.append(
            {"role": "assistant", "content": [b.model_dump() for b in response.content]}
        )

        if response.stop_reason != "tool_use":
            return TurnResult(
                text=_final_text(response.content),
                move_count=move_count,
                look_count=look_count,
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
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"unknown tool: {block.name}",
                    "is_error": True,
                })
        messages.append({"role": "user", "content": tool_results})

    text = _final_text(last_response.content) if last_response else ""
    if not text:
        text = "(stopped after looking around several times without settling on an answer)"
    return TurnResult(
        text=text,
        move_count=move_count,
        look_count=look_count,
        truncated=True,
        last_jpeg=last_jpeg,
        messages=messages,
    )
