"""Claude vision + tool use for Phase 3.

The arm is exposed as the `move_arm` tool. Two-phase conversation:

1. `ask_claude(text, image)` — first turn. Claude either replies in plain
   text (no movement) or returns a `move_arm` tool_use block. When a tool is
   used Claude usually emits no narration, so the response also carries the
   conversation state needed for the follow-up.
2. `continue_after_tool(response, result)` — second turn, called by the bot
   after the arm physically moved (or failed). Feeds the result back so
   Claude can comment on what it just did.

If Claude happens to emit text alongside the tool_use in turn 1, we use that
and skip the follow-up call.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional

import anthropic

from src import config

logger = logging.getLogger(__name__)

MOVE_ARM_TOOL = {
    "name": "move_arm",
    "description": (
        "Move the 6-servo Braccio robotic arm to a specific pose. "
        "Use this when the user asks you to look somewhere, point at something, "
        "wave, nod, or otherwise move. All angles are in degrees. "
        "If you don't need to move, don't call this tool."
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
                           "description": "Per-step delay in ms. 20 is a normal speed.", "default": 20},
        },
        "required": ["base", "shoulder", "elbow", "wrist_v", "wrist_r", "gripper"],
    },
}


@dataclass
class ClaudeResponse:
    text: str
    movement: Optional[dict] = None
    # Conversation state needed if a follow-up turn is required to convert
    # a tool_use into a verbal response. Set when `movement` was produced
    # and `text` is empty.
    _user_content: Optional[list] = None
    _assistant_content: Optional[list] = None
    _tool_use_id: Optional[str] = None

    @property
    def needs_followup(self) -> bool:
        return self._tool_use_id is not None


_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def ask_claude(user_text: str, image_jpeg: bytes) -> ClaudeResponse:
    client = _get_client()
    image_b64 = base64.standard_b64encode(image_jpeg).decode("ascii")
    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_b64,
            },
        },
        {"type": "text", "text": user_text},
    ]
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=config.SYSTEM_PROMPT,
        tools=[MOVE_ARM_TOOL],
        messages=[{"role": "user", "content": user_content}],
    )

    text_parts: list[str] = []
    movement: Optional[dict] = None
    tool_use_id: Optional[str] = None
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use" and block.name == "move_arm" and movement is None:
            movement = dict(block.input)
            tool_use_id = block.id

    usage = response.usage
    logger.info(
        "claude call: stop=%s in=%d out=%d movement=%s",
        response.stop_reason, usage.input_tokens, usage.output_tokens,
        movement is not None,
    )

    text = "\n".join(text_parts).strip()
    needs_followup = movement is not None and not text
    return ClaudeResponse(
        text=text,
        movement=movement,
        _user_content=user_content if needs_followup else None,
        _assistant_content=[b.model_dump() for b in response.content] if needs_followup else None,
        _tool_use_id=tool_use_id if needs_followup else None,
    )


def continue_after_tool(prev: ClaudeResponse, tool_result: str) -> str:
    """Send a tool_result follow-up. Returns Claude's narration of the action."""
    if not prev.needs_followup:
        raise RuntimeError("no pending tool_use on this response")
    client = _get_client()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=config.SYSTEM_PROMPT,
        tools=[MOVE_ARM_TOOL],
        messages=[
            {"role": "user", "content": prev._user_content},
            {"role": "assistant", "content": prev._assistant_content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": prev._tool_use_id,
                        "content": tool_result,
                    }
                ],
            },
        ],
    )
    text_parts = [b.text for b in response.content if b.type == "text"]
    usage = response.usage
    logger.info(
        "claude followup: stop=%s in=%d out=%d",
        response.stop_reason, usage.input_tokens, usage.output_tokens,
    )
    return "\n".join(text_parts).strip()
