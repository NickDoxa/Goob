"""Ground-truth direction calibration. Run on the Uno Q, Braccio powered.

    python -m tests.test_direction_calibration

Bypasses Claude entirely: drives raw servo values one joint at a time and
pauses between moves so you can watch. Also saves a photo after each pose
to /tmp/calib_<step>.jpg so the camera view can be checked afterward.

Sit where you normally sit (facing the arm) and write down, for each step,
which way the arm moved FROM YOUR point of view. Report the answers back —
they become the authoritative signs in kinematics and the prompt docs.
"""
from __future__ import annotations

import time
from pathlib import Path

from src import config
from src.arm import ArmController
from src.camera import Camera

HOME = dict(base=90, shoulder=90, elbow=90, wrist_v=90, wrist_r=90, gripper=10)

STEPS = [
    ("A", "BASE 150 — did the arm swing toward YOUR LEFT or YOUR RIGHT?",
     dict(HOME, base=150)),
    ("B", "BASE 30 — toward YOUR LEFT or YOUR RIGHT?",
     dict(HOME, base=30)),
    ("C", "ELBOW 140 — did the forearm swing DOWN/TOWARD you, or UP/BACK over the base?",
     dict(HOME, elbow=140)),
    ("D", "ELBOW 40 — DOWN/TOWARD you, or UP/BACK?",
     dict(HOME, elbow=40)),
    ("E", "WRIST_V 40 — did the gripper tip TOWARD you/down, or AWAY/up?",
     dict(HOME, wrist_v=40)),
    ("F", "WRIST_V 140 — TOWARD you/down, or AWAY/up?",
     dict(HOME, wrist_v=140)),
    ("G", "SHOULDER 60 — did the arm LEAN TOWARD you, or AWAY from you?",
     dict(HOME, shoulder=60)),
]


def main() -> None:
    print("Direction calibration. Watch the arm; each step holds 4 seconds.")
    print("Answer every question from YOUR seat, facing the arm.\n")
    with ArmController(port=config.ARM_SERIAL_PORT) as arm, \
         Camera(device=config.CAMERA_DEVICE) as cam:
        arm.move(**HOME)
        time.sleep(1)
        print("HOME: arm should be straight up. Check /tmp/calib_home.jpg —")
        print("does the camera see YOU / the room, right side up?\n")
        Path("/tmp/calib_home.jpg").write_bytes(cam.capture_jpeg(90))

        for step, question, pose in STEPS:
            print(f"[{step}] {question}")
            arm.move(**pose)
            time.sleep(4)
            Path(f"/tmp/calib_{step}.jpg").write_bytes(
                cam.capture_jpeg(pose["wrist_r"])
            )
            arm.move(**HOME)
            time.sleep(1)

    print("\nDone. Report your answer for each of A-G, plus whether the")
    print("home photo showed you/the room right side up.")


if __name__ == "__main__":
    main()
