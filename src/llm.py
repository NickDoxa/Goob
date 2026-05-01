"""Claude vision + agentic tool loop.

Each Discord (or voice) turn calls `ask_claude(text, capture, move)`. The
function runs an agentic loop with two tools:

- `look` — capture a fresh photo without moving. Used when Claude needs to
  see what's currently in front of the camera.
- `move_arm` — move the servos and return the new view. Used when Claude
  needs to look elsewhere.

The first turn carries no image — pure chit-chat ("how are you?") costs
zero vision tokens. Claude requests vision only when the prompt actually
needs it. After every tool call the resulting frame is fed back as a
tool_result and the loop continues until Claude returns a non-tool reply
or `max_turns` is hit.

Personality lives in `documentation/GOOB.md` so we can iterate on the prompt
without a code change.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import anthropic

from src import config

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

MOVE_ARM_TOOL = {
    "name": "move_arm",
    "description": (
        "Move the 6-servo Braccio robotic arm to a specific pose, then return "
        "a fresh photo of the new view. Use to look at things, look around, "
        "point, wave, or otherwise gesture. Call multiple times in a single "
        "turn to scan or zero in on something. All angles are degrees."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "base":       {"type": "integer", "minimum": 0,  "maximum": 180,
                           "description": "Rotation around the base. 90 is forward, 0 is full right, 180 is full left."},
            "shoulder":   {"type": "integer", "minimum": 15, "maximum": 165,
                           "description": "Shoulder pitch. 90 is upright, lower values lean forward."},
            "elbow":      {"type": "integer", "minimum": 0,  "maximum": 180,
                           "description": "Elbow angle. 90 is straight, 180 is fully folded."},
            "wrist_v":    {"type": "integer", "minimum": 0,  "maximum": 180,
                           "description": "Wrist vertical (pitch). 90 is level."},
            "wrist_r":    {"type": "integer", "minimum": 0,  "maximum": 180,
                           "description": "Wrist rotation (roll). 90 is neutral."},
            "gripper":    {"type": "integer", "minimum": 10, "maximum": 73,
                           "description": "Gripper. 10 is open, 73 is closed."},
            "step_delay": {"type": "integer", "minimum": 10, "maximum": 30,
                           "description": "Per-step delay in ms. 20 is normal speed.", "default": 20},
        },
        "required": ["base", "shoulder", "elbow", "wrist_v", "wrist_r", "gripper"],
    },
}

TOOLS = [LOOK_TOOL, MOVE_ARM_TOOL]


@dataclass
class TurnResult:
    text: str
    move_count: int
    look_count: int
    truncated: bool
    last_jpeg: Optional[bytes]  # None if Claude never looked


_PROMPT_PATH = Path(__file__).resolve().parent.parent / "documentation" / "GOOB.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

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
    max_turns: int = 12,
) -> TurnResult:
    # max_turns is LLM rounds, not tool calls. Claude can call multiple
    # tools per turn, so 12 rounds easily supports 10+ moves/looks of
    # iteration when the prompt encourages it.
    client = _get_client()
    messages: list[dict] = [
        {"role": "user", "content": user_text or "(no text)"}
    ]
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
    )
