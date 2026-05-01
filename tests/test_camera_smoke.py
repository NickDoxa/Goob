"""Phase 2 smoke test. Run on the host where the camera is plugged in.

    python -m tests.test_camera_smoke

Prereqs:
- USB UVC camera (Arducam) on the host's USB bus.
- CAMERA_DEVICE in .env points to a /dev/v4l/by-id/* path (stable across
  reboots) or to a numeric /dev/videoN index.

After it runs, view /tmp/frame.jpg (scp it off the Uno Q) and confirm
it looks like the room.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import config
from src.camera import Camera


OUT_PATH = Path("/tmp/frame.jpg")


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"opening camera {config.CAMERA_DEVICE}")
    with Camera(device=config.CAMERA_DEVICE) as cam:
        jpeg = cam.capture_jpeg()
        print(f"got {len(jpeg)} bytes")
        OUT_PATH.write_bytes(jpeg)
        print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
