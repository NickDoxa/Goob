"""Always-listening voice frontend for Goob.

Architecture:

    [mic] --> 16 kHz mono PCM --> openWakeWord
                                    |
                                    v wake detected
                            rate-limit check
                                    |
                                    v allowed
                          record speech (VAD-bounded,
                              MAX_AUDIO_SECONDS hard cap)
                                    |
                                    v
                       faster-whisper transcribe (local)
                                    |
                                    v non-empty
                            on_query(transcript)

Why local STT: keeps audio off the network and means there's no second API
key to fan out a budget across. Wake word is also local — no audio leaves
the device until the user says the magic word and clears rate limits.

Hard caps (all from config):
- MAX_AUDIO_SECONDS — recording terminates here regardless of VAD
- MIN_QUERY_INTERVAL_S — cooldown after a successful transcribe
- MAX_QUERIES_PER_HOUR — sliding-window cap, drops queries when exceeded
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np
import openwakeword
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# 16 kHz mono PCM is what openWakeWord, webrtcvad, and Whisper all want.
SAMPLE_RATE = 16000
# webrtcvad accepts 10/20/30 ms frames at 16 kHz; openWakeWord wants 80 ms
# (1280-sample) chunks for its built-in models. We feed it in 80 ms blocks
# and slice into 20 ms pieces for the VAD.
CHUNK_MS = 80
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000  # 1280
VAD_FRAME_MS = 20
VAD_FRAME_SAMPLES = SAMPLE_RATE * VAD_FRAME_MS // 1000  # 320

# Detection threshold. openWakeWord scores are 0..1; the prebuilt models
# fire reliably above ~0.5. Tighten if you get false positives.
WAKE_THRESHOLD = 0.5

# Trailing silence (in 80 ms chunks) that ends a recording. ~750 ms.
SILENCE_CHUNKS = 9


class VoiceError(RuntimeError):
    """Voice frontend misconfigured or hardware unavailable."""


class VoiceListener:
    """Background-thread mic loop. Construct, set on_query, call start()."""

    def __init__(
        self,
        on_query: Callable[[str], None],
        wake_word: str,
        audio_device: Optional[str],
        whisper_model: str,
        max_audio_seconds: float,
        min_query_interval_s: float,
        max_queries_per_hour: int,
    ) -> None:
        self.on_query = on_query
        self.wake_word = wake_word
        self.audio_device = audio_device or None
        self.whisper_model_name = whisper_model
        self.max_audio_seconds = max_audio_seconds
        self.min_query_interval_s = min_query_interval_s
        self.max_queries_per_hour = max_queries_per_hour

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_query_t: float = 0.0
        self._query_history: collections.deque[float] = collections.deque()
        self._oww: Optional[openwakeword.Model] = None
        self._whisper: Optional[WhisperModel] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Eagerly load models so we surface errors before the thread starts.
        logger.info("loading faster-whisper model %r", self.whisper_model_name)
        self._whisper = WhisperModel(
            self.whisper_model_name, device="cpu", compute_type="int8"
        )
        logger.info("loading openWakeWord model %r", self.wake_word)
        self._oww = openwakeword.Model(wakeword_models=[self.wake_word])

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_safe, name="VoiceListener", daemon=True
        )
        self._thread.start()
        logger.info(
            "voice listener started: wake=%s device=%s caps=%.0fs/%ds/%dh",
            self.wake_word,
            self.audio_device or "default",
            self.max_audio_seconds,
            self.min_query_interval_s,
            self.max_queries_per_hour,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _allowed(self) -> bool:
        now = time.monotonic()
        gap = now - self._last_query_t
        if gap < self.min_query_interval_s:
            logger.info("rate limit: %.1fs since last query, cooldown %.1fs",
                        gap, self.min_query_interval_s)
            return False
        cutoff = now - 3600
        while self._query_history and self._query_history[0] < cutoff:
            self._query_history.popleft()
        if len(self._query_history) >= self.max_queries_per_hour:
            logger.warning(
                "rate limit: %d queries in last hour, cap %d — dropping",
                len(self._query_history), self.max_queries_per_hour,
            )
            return False
        return True

    def _record_one(self) -> str:
        """Returns Claude transcript; '' if recording was empty/unintelligible."""
        assert self._whisper is not None
        vad = webrtcvad.Vad(2)
        frames: list[bytes] = []
        silence_chunks = 0
        max_chunks = int(self.max_audio_seconds * 1000 / CHUNK_MS)
        speech_started = False

        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=CHUNK_SAMPLES,
            channels=1,
            dtype="int16",
            device=self.audio_device,
        ) as stream:
            for _ in range(max_chunks):
                if self._stop.is_set():
                    return ""
                buf, _overflow = stream.read(CHUNK_SAMPLES)
                chunk = bytes(buf)
                frames.append(chunk)
                # VAD on the chunk: any 20 ms slice with speech counts.
                has_speech = False
                for i in range(0, CHUNK_SAMPLES, VAD_FRAME_SAMPLES):
                    slice_ = chunk[i * 2 : (i + VAD_FRAME_SAMPLES) * 2]
                    if len(slice_) == VAD_FRAME_SAMPLES * 2 and vad.is_speech(
                        slice_, SAMPLE_RATE
                    ):
                        has_speech = True
                        break
                if has_speech:
                    speech_started = True
                    silence_chunks = 0
                elif speech_started:
                    silence_chunks += 1
                    if silence_chunks >= SILENCE_CHUNKS:
                        break

        if not speech_started:
            logger.info("recording: no speech detected, skipping")
            return ""

        audio = b"".join(frames)
        samples = (
            np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        )
        logger.info("recording: %.1fs, transcribing", len(samples) / SAMPLE_RATE)
        segments, _info = self._whisper.transcribe(
            samples, language="en", beam_size=1
        )
        text = " ".join(s.text for s in segments).strip()
        return text

    def _run_safe(self) -> None:
        try:
            self._run()
        except Exception:
            logger.exception("voice listener crashed")

    def _wait_for_wake(self) -> bool:
        """Block until the wake word fires AND rate limits allow a query.

        Returns False only when stop has been requested.
        """
        assert self._oww is not None
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=CHUNK_SAMPLES,
            channels=1,
            dtype="int16",
            device=self.audio_device,
        ) as stream:
            logger.info("listening for wake word %r", self.wake_word)
            while not self._stop.is_set():
                buf, _overflow = stream.read(CHUNK_SAMPLES)
                samples = np.frombuffer(bytes(buf), dtype=np.int16)
                scores = self._oww.predict(samples)
                top = max(scores.values()) if scores else 0.0
                if top < WAKE_THRESHOLD:
                    continue
                logger.info("wake word detected (score=%.2f)", top)
                if not self._allowed():
                    self._oww.reset()
                    continue
                return True
        return False

    def _run(self) -> None:
        assert self._oww is not None
        # PortAudio doesn't share input devices well, so the wake-detect
        # stream and the record stream open and close in turn rather than
        # running concurrently. Keep them tightly scoped.
        while not self._stop.is_set():
            if not self._wait_for_wake():
                return
            transcript = self._record_one()
            if transcript:
                now = time.monotonic()
                self._last_query_t = now
                self._query_history.append(now)
                logger.info("transcript: %r", transcript)
                try:
                    self.on_query(transcript)
                except Exception:
                    logger.exception("on_query callback failed")
            self._oww.reset()
