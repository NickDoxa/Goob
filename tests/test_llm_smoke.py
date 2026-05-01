"""Phase 3 smoke test. Run on the Uno Q with camera + API key configured.

    python -m tests.test_llm_smoke

Uses a real camera but a *mock* arm — moves are printed, not executed —
so this can run without the Braccio powered on. The mock returns the same
frame to keep the loop deterministic-ish.
"""
from __future__ import annotations

import logging

from src import config
from src.camera import Camera
from src.llm import ask_claude


def _mock_move(**kwargs) -> None:
    print(f"  [mock move] {kwargs}")


def _run(prompt: str, cam: Camera) -> None:
    print(f"\n>>> {prompt}")
    initial = cam.capture_jpeg()
    result = ask_claude(prompt, initial, cam.capture_jpeg, _mock_move)
    print(f"text: {result.text}")
    print(f"moves: {result.move_count}, truncated: {result.truncated}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"opening camera {config.CAMERA_DEVICE}")
    with Camera(device=config.CAMERA_DEVICE) as cam:
        _run("what do you see?", cam)
        _run("look around the room and tell me what's nearby", cam)


if __name__ == "__main__":
    main()
