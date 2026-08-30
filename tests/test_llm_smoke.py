"""Phase 3 smoke test. Run on the Uno Q with camera + API key configured.

    python -m tests.test_llm_smoke

Uses a real camera but a *mock* arm — moves are printed, not executed —
so this can run without the Braccio powered on.
"""
from __future__ import annotations

import logging

from src import config
from src.arm import POSES
from src.camera import Camera
from src.kinematics import solve_look_at
from src.llm import ask_claude


def _mock_move(**kwargs) -> None:
    print(f"  [mock move] {kwargs}")


def _mock_pose(name: str) -> None:
    print(f"  [mock pose] {name}")


def _mock_look_at(x_cm: float, y_cm: float, z_cm: float) -> None:
    # Solve for real (pure math, no hardware) so the smoke test exercises
    # the kinematics and shows what Claude's targets resolve to.
    angles = solve_look_at(x_cm, y_cm, z_cm, POSES["home"])
    print(f"  [mock look_at] ({x_cm}, {y_cm}, {z_cm}) -> {angles}")


def _run(prompt: str, cam: Camera) -> None:
    print(f"\n>>> {prompt}")
    result = ask_claude(
        prompt, cam.capture_jpeg, _mock_move, _mock_pose, _mock_look_at
    )
    print(f"text: {result.text}")
    print(
        f"looks: {result.look_count}, moves: {result.move_count}, "
        f"truncated: {result.truncated}"
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"opening camera {config.CAMERA_DEVICE}")
    with Camera(device=config.CAMERA_DEVICE) as cam:
        _run("hey goob, how are you?", cam)
        _run("what do you see?", cam)
        _run("look at my hands", cam)
        _run("look around the room and tell me what's nearby", cam)


if __name__ == "__main__":
    main()
