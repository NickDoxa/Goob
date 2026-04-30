"""Settings loaded from .env / environment.

Phase 1 only consumes ARM_SERIAL_PORT and LOG_LEVEL. The rest of the keys
in .env.example are reserved for later phases — they're listed here as
defaults so adding them later is a one-liner.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

ARM_SERIAL_PORT: str = os.getenv("ARM_SERIAL_PORT", "/dev/ttyACM0")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
