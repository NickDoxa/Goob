"""Phase 1 smoke test. Run on the host where the Arduino is plugged in.

    python -m tests.test_arm_smoke

Prereqs:
- Arduino flashed with arduino/braccio_serial/braccio_serial.ino
- Braccio shield power switch ON (otherwise commands return OK but nothing moves)
- ARM_SERIAL_PORT in .env (default /dev/ttyACM0; set COMx on Windows)
"""
from __future__ import annotations

import logging
import time

from src import config
from src.arm import ArmController


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"opening {config.ARM_SERIAL_PORT}")
    with ArmController(config.ARM_SERIAL_PORT) as arm:
        print("ping")
        arm.ping()

        print("home")
        arm.home()
        time.sleep(1.0)

        print("base -> 45")
        arm.move(base=45, shoulder=90, elbow=90, wrist_v=90, wrist_r=90, gripper=30)
        time.sleep(1.5)

        print("base -> 135")
        arm.move(base=135, shoulder=90, elbow=90, wrist_v=90, wrist_r=90, gripper=30)
        time.sleep(1.5)

        print("home")
        arm.home()
        time.sleep(1.0)

    print("done")


if __name__ == "__main__":
    main()
