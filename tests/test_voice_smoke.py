"""Voice frontend smoke test. Run on the Uno Q with the mic plugged in.

    python -m tests.test_voice_smoke

What it does:
1. Lists audio input devices so you can sanity-check the mic is detected.
2. Records ~5 s and transcribes with faster-whisper to confirm the mic +
   STT pipeline works end-to-end.
3. Loads openWakeWord and waits up to 30 s for one wake-word hit, just to
   prove the wake path is alive.

What it does NOT do: call Claude, touch Discord, or move the arm. This is
purely the audio stack — useful for verifying setup without spending tokens.

Requires `pip install -e .[voice]`.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import openwakeword

from src import config
from src.voice import (
    CHUNK_SAMPLES,
    SAMPLE_RATE,
    WAKE_THRESHOLD,
    _resolve_onnx_model,
)

RECORD_SECONDS = 5
WAKE_TIMEOUT_S = 30


def _device() -> str | None:
    return config.AUDIO_INPUT_DEVICE or None


def step_list_devices() -> None:
    print("\n=== audio input devices ===")
    print(sd.query_devices())
    print(f"\nconfigured device: {_device() or '(default)'}")


def step_record_and_transcribe() -> None:
    print(f"\n=== recording {RECORD_SECONDS}s — speak now ===")
    audio = sd.rec(
        int(RECORD_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        device=_device(),
    )
    sd.wait()
    print("recorded, transcribing...")

    samples = audio.flatten().astype(np.float32) / 32768.0
    model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(samples, language="en", beam_size=1)
    text = " ".join(s.text for s in segments).strip()
    print(f"heard: {text!r}")
    if not text:
        print("  (nothing transcribed — check the mic and try again)")


def step_wake_word() -> None:
    print(
        f"\n=== say {config.WAKE_WORD!r} — waiting up to {WAKE_TIMEOUT_S}s ==="
    )
    onnx_path = _resolve_onnx_model(config.WAKE_WORD)
    oww = openwakeword.Model(
        wakeword_models=[onnx_path], inference_framework="onnx"
    )
    deadline = time.monotonic() + WAKE_TIMEOUT_S
    fired = False
    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=CHUNK_SAMPLES,
        channels=1,
        dtype="int16",
        device=_device(),
    ) as stream:
        while time.monotonic() < deadline:
            buf, _overflow = stream.read(CHUNK_SAMPLES)
            samples = np.frombuffer(bytes(buf), dtype=np.int16)
            scores = oww.predict(samples)
            top = max(scores.values()) if scores else 0.0
            if top >= WAKE_THRESHOLD:
                print(f"  wake word fired (score={top:.2f})")
                fired = True
                break
    if not fired:
        print("  timed out — wake word never fired above threshold")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    step_list_devices()
    step_record_and_transcribe()
    step_wake_word()
    print("\ndone.")


if __name__ == "__main__":
    main()
