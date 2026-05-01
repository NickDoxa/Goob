"""Settings loaded from .env / environment.

Grows phase by phase. Keys not yet consumed live in .env.example only.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

ARM_SERIAL_PORT: str = os.getenv("ARM_SERIAL_PORT", "/dev/ttyACM0")
CAMERA_DEVICE_INDEX: int = int(os.getenv("CAMERA_DEVICE_INDEX", "0"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
