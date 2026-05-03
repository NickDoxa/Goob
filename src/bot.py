"""Discord frontend. Owner-DM-only.

No auto-capture: ask_claude only sees what it asks to see (look or
move_arm). Pure chit-chat costs zero vision tokens. The voice listener (if
enabled) routes transcribed queries through the same agentic flow and
replies via DM.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import discord

from src.arm import ArmController
from src.camera import Camera
from src.llm import TurnResult, ask_claude

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
        # Set by main.py before run() if VOICE_ENABLED.
        self.voice_listener = None

    async def on_ready(self) -> None:
        logger.info("logged in as %s, locked to owner %d", self.user, self.owner_id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the room"
            )
        )
        if self.voice_listener is not None:
            try:
                self.voice_listener.start()
            except Exception:
                logger.exception("voice listener failed to start")

    async def _run_turn(self, user_text: str) -> TurnResult:
        return await asyncio.to_thread(
            ask_claude,
            user_text,
            self.camera.capture_jpeg,
            self.arm.move,
            self.arm.move_to_pose,
        )

    def _format_suffix(self, result: TurnResult) -> str:
        suffix = ""
        if result.move_count == 1:
            suffix += "\n_(moved)_"
        elif result.move_count > 1:
            suffix += f"\n_(moved {result.move_count}×)_"
        if result.truncated:
            suffix += "\n_(stopped at max moves)_"
        return suffix

    def _attached(self, jpeg: Optional[bytes]) -> list[discord.File]:
        if not self.attach_frame or jpeg is None:
            return []
        return [discord.File(io.BytesIO(jpeg), filename="frame.jpg")]

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
                result = await self._run_turn(message.content)
            except Exception as exc:
                logger.exception("claude call failed")
                await message.reply(f"brain error: {exc}")
                return
            text = result.text or "(no response)"
            await message.reply(
                text + self._format_suffix(result),
                files=self._attached(result.last_jpeg),
            )

    async def handle_voice_query(self, transcript: str) -> None:
        """Called from the voice thread via run_coroutine_threadsafe."""
        logger.info("voice query: %r", transcript)
        try:
            owner = await self.fetch_user(self.owner_id)
            channel = await owner.create_dm()
        except Exception:
            logger.exception("could not open DM with owner for voice reply")
            return

        async with channel.typing():
            await channel.send(f"_(voice)_ {transcript}")
            try:
                result = await self._run_turn(transcript)
            except Exception as exc:
                logger.exception("voice query: claude failed")
                await channel.send(f"brain error: {exc}")
                return
            text = result.text or "(no response)"
            await channel.send(
                text + self._format_suffix(result),
                files=self._attached(result.last_jpeg),
            )
