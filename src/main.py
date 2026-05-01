"""Entry point: open arm + camera, run the Discord bot until killed.

If VOICE_ENABLED, also start a wake-word listener that funnels voice queries
into the same Discord reply path. Voice deps live behind the [voice] extra
and are only imported when the flag is on, so a non-voice install stays lean.
"""
from __future__ import annotations

import asyncio
import logging

from src import config
from src.arm import ArmController
from src.bot import GoobClient
from src.camera import Camera


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not config.DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not config.OWNER_USER_ID:
        raise RuntimeError("OWNER_USER_ID is not set")
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    with ArmController(port=config.ARM_SERIAL_PORT) as arm, \
         Camera(device=config.CAMERA_DEVICE) as camera:
        client = GoobClient(
            arm=arm,
            camera=camera,
            owner_id=config.OWNER_USER_ID,
            attach_frame=config.DEBUG_ATTACH_FRAME,
        )

        if config.VOICE_ENABLED:
            # Deferred import: the voice extra pulls in faster-whisper,
            # openwakeword, sounddevice, and a portaudio system dep.
            from src.voice import VoiceListener

            voice_log = logging.getLogger("src.main.voice")

            def on_voice_query(transcript: str) -> None:
                # Voice thread → discord event loop. client.loop is set by
                # the time on_ready starts the listener, so this is safe.
                # We BLOCK the voice thread until the agentic loop finishes:
                # leaving the wake-word mic stream open during a long query
                # starves PortAudio (USB-bus contention with the Arducam
                # camera + GIL pressure → ALSA xruns), and it also prevents
                # stacking new voice queries on top of an in-flight one.
                future = asyncio.run_coroutine_threadsafe(
                    client.handle_voice_query(transcript), client.loop
                )
                try:
                    future.result(timeout=300)
                except Exception:
                    voice_log.exception("voice query failed or timed out")

            client.voice_listener = VoiceListener(
                on_query=on_voice_query,
                wake_word=config.WAKE_WORD,
                audio_device=config.AUDIO_INPUT_DEVICE,
                whisper_model=config.WHISPER_MODEL,
                max_audio_seconds=config.MAX_AUDIO_SECONDS,
                min_query_interval_s=config.MIN_QUERY_INTERVAL_S,
                max_queries_per_hour=config.MAX_QUERIES_PER_HOUR,
            )

        try:
            client.run(config.DISCORD_TOKEN, log_handler=None)
        finally:
            if client.voice_listener is not None:
                client.voice_listener.stop()


if __name__ == "__main__":
    main()
