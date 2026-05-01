"""Discord frontend. Owner-DM-only; one turn per message.

The agentic loop in `ask_claude` may call the arm and recapture the camera
multiple times before answering. We hand it bound methods, run the whole
thing in a worker thread, and post the final text + last frame.
"""
from __future__ import annotations

import asyncio
import io
import logging

import discord

from src.arm import ArmController
from src.camera import Camera
from src.llm import ask_claude

logger = logging.getLogger(__name__)


class GoobClient(discord.Client):
    def __init__(
        self,
        arm: ArmController,
        camera: Camera,
        owner_id: int,
        attach_frame: bool,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.arm = arm
        self.camera = camera
        self.owner_id = owner_id
        self.attach_frame = attach_frame

    async def on_ready(self) -> None:
        logger.info("logged in as %s, locked to owner %d", self.user, self.owner_id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the room"
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return
        if message.author.id != self.owner_id:
            return

        logger.info("dm from %d, %d chars", message.author.id, len(message.content))

        async with message.channel.typing():
            try:
                initial_jpeg = await asyncio.to_thread(self.camera.capture_jpeg)
            except Exception as exc:
                logger.exception("camera capture failed")
                await message.reply(f"camera error: {exc}")
                return

            try:
                result = await asyncio.to_thread(
                    ask_claude,
                    message.content,
                    initial_jpeg,
                    self.camera.capture_jpeg,
                    self.arm.move,
                )
            except Exception as exc:
                logger.exception("claude call failed")
                await message.reply(f"brain error: {exc}")
                return

            suffix = ""
            if result.move_count == 1:
                suffix = "\n_(moved)_"
            elif result.move_count > 1:
                suffix = f"\n_(moved {result.move_count}×)_"
            if result.truncated:
                suffix += "\n_(stopped at max moves)_"

            text = result.text or "(no response)"

            files: list[discord.File] = []
            if self.attach_frame:
                files.append(
                    discord.File(io.BytesIO(result.last_jpeg), filename="frame.jpg")
                )

            await message.reply(text + suffix, files=files)
