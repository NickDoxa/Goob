"""Discord frontend. Owner-DM-only; one turn per message.

Capture, Claude, optional arm move, reply. Sync work (capture/serial) is
offloaded to threads so we don't stall the event loop.
"""
from __future__ import annotations

import asyncio
import io
import logging

import discord

from src.arm import ArmController, ArmError
from src.camera import Camera
from src.llm import ask_claude, continue_after_tool

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
                jpeg = await asyncio.to_thread(self.camera.capture_jpeg)
            except Exception as exc:
                logger.exception("camera capture failed")
                await message.reply(f"camera error: {exc}")
                return

            try:
                resp = await asyncio.to_thread(ask_claude, message.content, jpeg)
            except Exception as exc:
                logger.exception("claude call failed")
                await message.reply(f"brain error: {exc}")
                return

            arm_status = ""
            reply_text = (resp.text or "").strip()
            if resp.movement is not None:
                logger.info("arm move: %s", resp.movement)
                try:
                    await asyncio.to_thread(self.arm.move, **resp.movement)
                    tool_result = "ok, arm moved to the requested pose"
                    arm_status = "\n_(moved)_"
                except ArmError as exc:
                    logger.warning("arm error: %s", exc)
                    tool_result = f"arm move failed: {exc}"
                    arm_status = f"\n_(arm error: {exc})_"

                if resp.needs_followup:
                    try:
                        reply_text = await asyncio.to_thread(
                            continue_after_tool, resp, tool_result
                        )
                    except Exception as exc:
                        logger.exception("claude followup failed")

            if not reply_text and not arm_status:
                reply_text = "(no response)"

            files: list[discord.File] = []
            if self.attach_frame:
                files.append(discord.File(io.BytesIO(jpeg), filename="frame.jpg"))

            await message.reply(reply_text + arm_status, files=files)
