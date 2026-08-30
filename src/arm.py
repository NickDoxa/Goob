"""Serial-protocol controller for the Braccio Arduino sketch.

Pairs with arduino/braccio_serial/braccio_serial.ino. Protocol is line-based
ASCII at 115200 baud, terminated with '\\n'. See PLAN.md §Phase 1 for the
canonical spec.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import serial

logger = logging.getLogger(__name__)


class ArmError(RuntimeError):
    """Arduino returned ERR, or the serial channel misbehaved."""


@dataclass(frozen=True)
class ServoLimits:
    base:       tuple[int, int] = (0,   180)
    shoulder:   tuple[int, int] = (15,  165)
    elbow:      tuple[int, int] = (0,   180)
    wrist_v:    tuple[int, int] = (0,   180)
    wrist_r:    tuple[int, int] = (0,   180)
    gripper:    tuple[int, int] = (10,  73)
    step_delay: tuple[int, int] = (10,  30)


LIMITS = ServoLimits()

# Named preset poses. Keys are passed verbatim to Claude via the go_to_pose
# tool, so the names should be self-documenting. Values must include all six
# servo angles. wrist_r=90 is the upright baseline for the camera; Camera
# rotates the image dynamically based on the wrist_r passed at capture, so
# Claude is free to spin the wrist for personality moves.
#
# scan_left / scan_right are USER-perspective, because that's what
# conversation uses. Measured on the hardware (2026-08-30): RAISING the base
# servo swings the arm and its view toward the USER'S LEFT. So base 0 = the
# user's right, base 90 = facing the user, base 180 = the user's left, and
# scan_left is the HIGH base value. tests/test_kinematics_smoke.py checks
# these two against forward kinematics.
POSES: dict[str, dict[str, int]] = {
    "home":          dict(base=90,  shoulder=90,  elbow=90,  wrist_v=90,  wrist_r=90, gripper=10),
    "look_at_hands": dict(base=90,  shoulder=75,  elbow=80,  wrist_v=70,  wrist_r=90, gripper=10),
    "look_down":     dict(base=90,  shoulder=130, elbow=140, wrist_v=40,  wrist_r=90, gripper=10),
    "look_up":       dict(base=90,  shoulder=110, elbow=60,  wrist_v=140, wrist_r=90, gripper=10),
    "scan_left":     dict(base=150, shoulder=90,  elbow=90,  wrist_v=90,  wrist_r=90, gripper=10),
    "scan_right":    dict(base=30,  shoulder=90,  elbow=90,  wrist_v=90,  wrist_r=90, gripper=10),
}


def _clamp(name: str, value: int, lo: int, hi: int) -> int:
    if lo <= value <= hi:
        return value
    logger.warning("clamping %s=%d to [%d, %d]", name, value, lo, hi)
    return max(lo, min(hi, value))


class ArmController:
    """Owns the serial port to the Braccio Arduino. Use as a context manager.

    The Arduino auto-resets when the host opens the port (DTR pulse), then
    runs Braccio.begin() (~3-4s soft-start) and prints READY. We block on
    READY before returning from __enter__.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 10.0,
        ready_timeout: float = 10.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ready_timeout = ready_timeout
        self._ser: Optional[serial.Serial] = None
        # Tracked pose. Camera reads current_wrist_r (below) at capture time
        # so it can rotate the frame back to upright regardless of where the
        # gripper is currently rolled. After Braccio.begin() the arm sits at
        # the home pose, so that's the correct initial value.
        self._pose: dict[str, int] = dict(POSES["home"])
        # None = untested; True/False once a move has confirmed which
        # protocol the connected firmware speaks. Sticky for the session so
        # we don't retry EASE against known-old firmware on every call.
        self._easing_supported: Optional[bool] = None

    @property
    def current_wrist_r(self) -> int:
        return self._pose["wrist_r"]

    @property
    def pose(self) -> dict[str, int]:
        """Copy of the tracked pose. Copy so callers (kinematics warm-start)
        can't mutate the arm's idea of where it is."""
        return dict(self._pose)

    def __enter__(self) -> "ArmController":
        self._ser = serial.Serial(self.port, self.baudrate, timeout=0.5)
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            line = self._readline()
            if line == "READY":
                self._ser.timeout = self.timeout
                logger.info("arm READY on %s", self.port)
                return self
            if line:
                logger.debug("pre-ready: %r", line)
        self.__exit__(None, None, None)
        raise ArmError(f"no READY from arm on {self.port} within {self.ready_timeout:.1f}s")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def _readline(self) -> str:
        assert self._ser is not None, "port not open"
        raw = self._ser.readline()
        return raw.decode("utf-8", errors="replace").strip()

    def _send(self, line: str) -> None:
        assert self._ser is not None, "port not open"
        logger.debug("-> %s", line)
        self._ser.write((line + "\n").encode("utf-8"))
        self._ser.flush()

    def _send_and_wait(self, line: str) -> None:
        self._send(line)
        reply = self._readline()
        logger.debug("<- %s", reply)
        if reply == "OK":
            return
        if reply.startswith("ERR"):
            raise ArmError(reply)
        raise ArmError(f"unexpected reply: {reply!r}")

    def ping(self) -> None:
        self._send_and_wait("PING")

    def home(self) -> None:
        self._send_and_wait("HOME")
        self._pose = dict(POSES["home"])

    def move(
        self,
        base: int,
        shoulder: int,
        elbow: int,
        wrist_v: int,
        wrist_r: int = 90,
        gripper: int = 10,
        step_delay: int = 10,
        duration_ms: Optional[int] = None,
    ) -> None:
        # Defaults: wrist_r=90 is the camera-upright baseline; gripper=10 is
        # open (visual tasks don't care). step_delay is a dead parameter now
        # (kept only so existing callers don't need updating) — it's used
        # solely for the legacy MOVE fallback below, never for EASE.
        b  = _clamp("base",       base,       *LIMITS.base)
        s  = _clamp("shoulder",   shoulder,   *LIMITS.shoulder)
        e  = _clamp("elbow",      elbow,      *LIMITS.elbow)
        wv = _clamp("wrist_v",    wrist_v,    *LIMITS.wrist_v)
        wr = _clamp("wrist_r",    wrist_r,    *LIMITS.wrist_r)
        g  = _clamp("gripper",    gripper,    *LIMITS.gripper)
        d  = _clamp("step_delay", step_delay, *LIMITS.step_delay)
        target = dict(base=b, shoulder=s, elbow=e, wrist_v=wv, wrist_r=wr, gripper=g)

        if duration_ms is None:
            max_delta = max(abs(target[k] - self._pose[k]) for k in target)
            duration_ms = max(300, min(1500, max_delta * 8))

        if self._easing_supported is not False:
            self._send(f"EASE {duration_ms} {b} {s} {e} {wv} {wr} {g}")
            reply = self._readline()
            logger.debug("<- %s", reply)
            if reply == "OK":
                self._easing_supported = True
                self._pose = target
                return
            if reply == "ERR unknown":
                if self._easing_supported is None:
                    logger.warning("arm firmware does not support EASE; falling back to MOVE for this session")
                self._easing_supported = False
            elif reply.startswith("ERR"):
                raise ArmError(reply)
            else:
                raise ArmError(f"unexpected reply: {reply!r}")

        self._send_and_wait(f"MOVE {d} {b} {s} {e} {wv} {wr} {g}")
        # Update tracked state only after the Arduino confirms OK, so a
        # failed move doesn't leave Camera applying stale rotation.
        self._pose = target

    def move_to_pose(self, name: str) -> None:
        """Snap to a named preset from POSES."""
        if name not in POSES:
            raise ArmError(f"unknown pose {name!r}; have {sorted(POSES)}")
        self.move(**POSES[name])
