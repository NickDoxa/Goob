# G.O.O.B.

### Generative Optical Operational Bot

Discord-controlled robotic arm with Claude vision. DM the bot, it grabs a frame from a camera mounted on a Braccio arm, sends image + text to Claude, and the arm physically responds.

## Hardware

- **Arduino Uno Q** — Debian Linux brain. Runs the Python orchestrator.
- **Elegoo Uno R3** + TinkerKit Braccio shield. Drives the 6 servos over USB serial.
- **Arducam** mounted on the Braccio gripper.
- 5 V / 4 A wall wart bundled with the Braccio.

The serial protocol spec lives at the top of `arduino/braccio_serial/braccio_serial.ino`.

## Repo layout

```
arduino/braccio_serial/   Arduino sketch — line-based serial protocol
src/
  arm.py
  camera.py
  llm.py
  bot.py
  config.py
tests/                    manual smoke tests, one per phase
```

## Quick start (Phase 1)

1. Flash `arduino/braccio_serial/braccio_serial.ino` to the Elegoo Uno R3.
2. On the host (Uno Q for deployment; any machine for early testing):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate    # Windows: .venv\Scripts\activate
   pip install -e .
   cp .env.example .env         # set ARM_SERIAL_PORT if not /dev/ttyACM0
   ```

3. Turn on the Braccio shield's power switch, then:

   ```bash
   python -m tests.test_arm_smoke
   ```

   The arm should home, swing the base to 45°, swing to 135°, and home again.

## Quick start (Phase 2)

After the env is set up:

```bash
python -m tests.test_camera_smoke
```

writes a JPEG of one frame to `/tmp/frame.jpg`. On the Uno Q the `/dev/videoN` indices flip across reboots because the Qualcomm Venus hardware encoder and the Arducam race for them. Use the stable symlink in `.env` instead — `ls /dev/v4l/by-id/` and pick the `*-video-index0` entry (index1 is the metadata node). The default in `.env.example` already points at the Arducam's by-id path.

Setup for later phases (Discord bot, Anthropic API) lands as those phases ship.

## Voice frontend (optional)

Talk to Goob with a wake word. Wake-word detection and speech-to-text both
run locally — no audio leaves the device until the magic word fires and rate
limits clear, at which point the transcript goes to Claude exactly like a
Discord DM would.

1. System dep on Linux:

   ```bash
   sudo apt install libportaudio2 portaudio19-dev
   ```

2. Install the voice extra:

   ```bash
   pip install -e .[voice]
   ```

   This pulls in `faster-whisper`, `openwakeword`, `webrtcvad-wheels`,
   `sounddevice`, and `numpy`. First run downloads the Whisper model
   (`tiny.en` is ~75 MB) and the openWakeWord ONNX models.

3. Find the audio input device:

   ```bash
   python -m sounddevice
   ```

   Note a substring of the mic's name (e.g. `USB`, `Vitade`). Leave
   `AUDIO_INPUT_DEVICE` empty to use the system default.

4. Flip on voice in `.env`:

   ```
   VOICE_ENABLED=true
   WAKE_WORD=hey_jarvis           # or alexa / hey_mycroft
   AUDIO_INPUT_DEVICE=             # or "Vitade", etc.
   WHISPER_MODEL=tiny.en           # or base.en for more accuracy
   MAX_AUDIO_SECONDS=15
   MIN_QUERY_INTERVAL_S=5
   MAX_QUERIES_PER_HOUR=30
   ```

5. Run the smoke test before going live:

   ```bash
   python -m tests.test_voice_smoke
   ```

   It records 5 s, transcribes, then waits for one wake-word hit. No Claude
   calls, no Discord. Useful for verifying mic + models without burning
   tokens.

Custom wake words ("hey goob") aren't built in — openWakeWord ships with
`alexa`, `hey_jarvis`, and `hey_mycroft`. Training a custom model is a
separate project; the hooks are in place if you ever want to swap one in.

When voice is on, `python -m src.main` starts the listener alongside the
Discord client. Spoken queries reply in DM, prefixed with `_(voice)_` so you
can tell which channel a turn came from.
