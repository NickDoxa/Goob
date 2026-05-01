"""Phase 3 smoke test. Run on the Uno Q with camera + API key configured.

    python -m tests.test_llm_smoke

Captures one frame from the live camera, sends two prompts to Claude:
- "what do you see?" — expect a sane description, no movement
- "look to your left" — expect text + a movement dict with base < 90
"""
from __future__ import annotations

import logging

from src import config
from src.camera import Camera
from src.llm import ask_claude, continue_after_tool


def _run(prompt: str, jpeg: bytes) -> None:
    print(f"\n>>> {prompt}")
    resp = ask_claude(prompt, jpeg)
    print(f"text: {resp.text!r}")
    print(f"movement: {resp.movement}")
    if resp.needs_followup:
        followup = continue_after_tool(resp, "ok, arm moved to the requested pose")
        print(f"followup: {followup}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"opening camera {config.CAMERA_DEVICE}")
    with Camera(device=config.CAMERA_DEVICE) as cam:
        jpeg = cam.capture_jpeg()
        print(f"captured {len(jpeg)} bytes")
        _run("what do you see?", jpeg)
        _run("look to your left", jpeg)


if __name__ == "__main__":
    main()
