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

Setup for later phases (camera, Discord bot, Anthropic API) lands as those phases ship.
