# Codex context for goob_brain

## What this is

Discord-controlled Braccio arm with Codex vision. **`documentation/PLAN.md` is the canonical design doc and phased build plan — read it before doing anything substantive.**

## Working agreement

- **Phases are sequential.** PLAN.md breaks the build into Phases 1–4. When the user names a phase, work only that phase and stop at its acceptance criteria.
- **Verify on real hardware before declaring a phase done.** Each phase ends with a manual smoke test the user runs by hand.
- **Don't edit PLAN.md without an explicit ask.** It's the spec, not a working doc.
- **Commit at the end of each phase** so phases are reversible.
- **Stay in scope.** Don't add abstractions, fallbacks, or features beyond the current phase. Three similar lines beats a premature abstraction.

## Hardware

- **Arduino Uno Q** — Debian Trixie, Python 3.13. Runs the Python orchestrator. Camera + USB-serial to the R3 both physically connect here.
- **Elegoo Uno R3** + TinkerKit Braccio shield. Drives the 6 servos. Powered by the bundled 5 V / 4 A wall wart.
- **Arducam** mounted on the gripper. USB UVC (Microdia Vitade AF, `0c45:6366`), connected through the hub. No Media Carrier needed.

## Repo

- `arduino/braccio_serial/braccio_serial.ino` — Phase 1 firmware (line-based ASCII serial @ 115200)
- `src/arm.py` — `ArmController` context manager
- `src/config.py` — env loading; grows phase by phase
- `src/camera.py` — Phase 2 `Camera` (V4L2 capture → JPEG bytes)
- `src/{llm,bot,main}.py` — Phases 3/4 (not yet written)
- `tests/test_*_smoke.py` — manual smoke tests
- `goob_brain.ino` (project root) — superseded demo pose loop, kept until the user removes it

## Conventions

- Python ≥ 3.11, deps in `pyproject.toml` (`pip install -e .`)
- Servo limits clamped both Arduino-side (authoritative) and Python-side (warning)
- No comments unless the *why* is non-obvious — well-named identifiers do the rest
- No emojis in code or docs unless explicitly requested

## Quirks worth knowing

- **Uno Q password split**: App Lab's password and the Linux user's SSH password are separate systems. SSH/scp uses the Linux password — set it explicitly via `passwd` (or `sudo passwd $(whoami)`) from inside App Lab's `>_` remote shell.
- **Braccio power switch**: with it off, `MOVE` commands return `OK` but nothing physically moves.
- **Arduino auto-reset on serial open**: allow ~3–4 s for `Braccio.begin()` soft-start before the sketch sends `READY`.
- **Calibration**: servos must be centered with `testBraccio90` once before any pose work is meaningful.
- **`/dev/video*` numbering on Uno Q**: the Qualcomm Venus hardware video encoder claims `/dev/video0` and `/dev/video1` — the USB Arducam ends up at `/dev/video2` (with `/dev/video3` as its metadata node). `CAMERA_DEVICE_INDEX=2` in `.env`. Confirm with `v4l2-ctl --list-devices` if it ever moves.
- **Camera autofocus warmup**: the Vitade is autofocus; the 3-frame warmup in `Camera._open` is enough if the camera is held still during capture, but motion during the first capture can produce a soft frame.

## What's intentionally not in scope for v1

- Voice / wake word / TTS / touchscreen — see PLAN.md "Out of scope for v1"
- Inverse kinematics — Codex picks angles directly
- Conversation memory across messages — each turn is independent
- Multi-user — the bot is owner-DM-only
