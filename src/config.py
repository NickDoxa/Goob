"""Settings loaded from .env / environment.

Grows phase by phase. Keys not yet consumed live in .env.example only.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

ARM_SERIAL_PORT: str = os.getenv("ARM_SERIAL_PORT", "/dev/ttyACM0")

# Either an integer index ("0") or a device path. /dev/v4l/by-id/* is stable
# across reboots; /dev/videoN is not.
_camera_raw = os.getenv("CAMERA_DEVICE", "0")
CAMERA_DEVICE: int | str = int(_camera_raw) if _camera_raw.isdigit() else _camera_raw

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
