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

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

SYSTEM_PROMPT = """\
You are the brain of a small desk-mounted robotic arm with a camera on its
gripper. The user talks to you via Discord DMs. With each message you receive
the user's text plus a fresh photo from the camera.

Be brief and conversational - 1-3 sentences typically. You're a robot, not an
essay writer.

You have one tool, `move_arm`, which physically moves the arm. Use it when the
user asks you to look around, point, wave, or otherwise gesture. You don't need
to call it on every turn - only when motion is genuinely useful.

Servo coordinate cheat sheet:
- base 90 = facing forward, 0 = right, 180 = left
- shoulder/elbow/wrist all 90 = arm pointing straight up
- gripper 10 = fully open, 73 = fully closed

If you can't tell what's in the image (too dark, blurry, occluded), say so
plainly instead of guessing.
"""
