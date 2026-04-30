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

    def move(
        self,
        base: int,
        shoulder: int,
        elbow: int,
        wrist_v: int,
        wrist_r: int,
        gripper: int,
        step_delay: int = 20,
    ) -> None:
        b  = _clamp("base",       base,       *LIMITS.base)
        s  = _clamp("shoulder",   shoulder,   *LIMITS.shoulder)
        e  = _clamp("elbow",      elbow,      *LIMITS.elbow)
        wv = _clamp("wrist_v",    wrist_v,    *LIMITS.wrist_v)
        wr = _clamp("wrist_r",    wrist_r,    *LIMITS.wrist_r)
        g  = _clamp("gripper",    gripper,    *LIMITS.gripper)
        d  = _clamp("step_delay", step_delay, *LIMITS.step_delay)
        self._send_and_wait(f"MOVE {d} {b} {s} {e} {wv} {wr} {g}")
