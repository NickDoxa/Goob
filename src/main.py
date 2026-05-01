"""Entry point: open arm + camera, run the Discord bot until killed."""
from __future__ import annotations

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
        client.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
