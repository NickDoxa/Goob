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

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
_owner_raw = os.getenv("OWNER_USER_ID", "0")
OWNER_USER_ID: int = int(_owner_raw) if _owner_raw else 0
DEBUG_ATTACH_FRAME: bool = os.getenv("DEBUG_ATTACH_FRAME", "false").lower() == "true"

# --- Voice frontend (optional) ---
# Off by default. The voice listener pulls in heavyweight deps (faster-whisper,
# openwakeword, sounddevice) and needs portaudio on the host, so it must be
# explicitly enabled.
VOICE_ENABLED: bool = os.getenv("VOICE_ENABLED", "false").lower() == "true"

# openWakeWord built-in model name. Bundled options include "alexa",
# "hey_jarvis", "hey_mycroft". Custom wake words require training a model.
WAKE_WORD: str = os.getenv("WAKE_WORD", "hey_jarvis")

# Audio input device. Empty string = system default. Use the substring of the
# device name shown by `python -m sounddevice` to disambiguate (e.g. "USB" or
# "Vitade").
AUDIO_INPUT_DEVICE: str = os.getenv("AUDIO_INPUT_DEVICE", "")

# faster-whisper model size. tiny.en is ~75MB and runs in real time on the
# Uno Q's CPU; base.en is ~145MB and noticeably more accurate.
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "tiny.en")

# Hard caps to prevent runaway token spend if the wake word misfires.
_max_audio = os.getenv("MAX_AUDIO_SECONDS", "15")
MAX_AUDIO_SECONDS: float = float(_max_audio) if _max_audio else 15.0
_min_interval = os.getenv("MIN_QUERY_INTERVAL_S", "5")
MIN_QUERY_INTERVAL_S: float = float(_min_interval) if _min_interval else 5.0
_max_per_hour = os.getenv("MAX_QUERIES_PER_HOUR", "30")
MAX_QUERIES_PER_HOUR: int = int(_max_per_hour) if _max_per_hour else 30

# --- Session memory ---
# Goob keeps conversation history between turns so "wrong wall, look at the
# other one" works. History is in-memory, single-owner. Both Discord and
# voice queries share the same context.
#
# Idle timeout: drop history after this many seconds of silence so a stale
# topic from yesterday doesn't poison today's chat.
_session_idle = os.getenv("SESSION_IDLE_S", "300")
SESSION_IDLE_S: float = float(_session_idle) if _session_idle else 300.0
# Image budget: keep this many recent images in history; older ones get
# replaced with a placeholder. Vision tokens are expensive; text is cheap.
_session_imgs = os.getenv("SESSION_MAX_IMAGES", "3")
SESSION_MAX_IMAGES: int = int(_session_imgs) if _session_imgs else 3

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
