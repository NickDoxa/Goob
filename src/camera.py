"""V4L2 camera capture for the gripper-mounted Arducam.

Pairs with PLAN.md §Phase 2. The capture device is opened lazily on the
first call and kept open for the process lifetime — opening cv2.VideoCapture
is slow on Linux and reopening per-call thrashes the device.

The camera is mounted upside-down on the gripper. capture_jpeg takes the
arm's current wrist_r value and rotates the image so it always comes out
upright — at the baseline (wrist_r=90) that's a 180° rotation, and Claude
can spin the gripper without breaking image-axis reasoning.
"""
from __future__ import annotations

import logging
from typing import Optional

import cv2

logger = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Camera device missing, busy, or producing bad frames."""


def _rotate_image(frame, deg: int):
    """Rotate `frame` by `deg` degrees counter-clockwise. Fast paths for
    90° increments; arbitrary angles fall back to warpAffine and crop the
    output the same size as the input (corner pixels become black).
    """
    deg = deg % 360
    if deg == 0:
        return frame
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    h, w = frame.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    return cv2.warpAffine(frame, M, (w, h))


class Camera:
    def __init__(
        self,
        device: int | str = 0,
        width: int = 1280,
        height: int = 720,
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self._cap: Optional[cv2.VideoCapture] = None

    def _open(self) -> cv2.VideoCapture:
        # Pin the V4L2 backend — auto-detect on Debian sometimes picks
        # GStreamer and stalls on the first read.
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise CameraError(f"could not open camera {self.device!r}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # Hint to the driver: keep the queue at one frame so reads are
        # current. Not all V4L2 drivers honor this, hence the drain in
        # capture_jpeg as a backup.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Auto-exposure is bad for the first couple of frames; toss them.
        for _ in range(3):
            cap.read()
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info(
            "camera opened: device=%s %dx%d (requested %dx%d)",
            self.device, actual_w, actual_h, self.width, self.height,
        )
        return cap

    def capture_jpeg(self, wrist_r: int = 90) -> bytes:
        if self._cap is None:
            self._cap = self._open()
        # Drain stale frames buffered by the V4L2 driver since the last
        # call. Without this, infrequent captures return frames from
        # minutes ago — the queue holds up to ~4 frames.
        for _ in range(4):
            self._cap.grab()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise CameraError("camera read failed")
        # Image rotation = 180° (upside-down mount) + (wrist_r - 90)°
        # deviation from the camera-upright baseline. If the rotation
        # direction comes out backwards in practice, flip the sign of
        # the deviation term — the Braccio servo direction is hardware-
        # specific and easier to verify empirically than to reason about.
        rot_deg = (180 + (wrist_r - 90)) % 360
        frame = _rotate_image(frame, rot_deg)
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
        )
        if not ok:
            raise CameraError("jpeg encode failed")
        return buf.tobytes()

    def close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            finally:
                self._cap = None

    def __enter__(self) -> "Camera":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
