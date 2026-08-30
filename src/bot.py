"""Discord frontend. Owner-DM-only.

No auto-capture: ask_claude only sees what it asks to see (look or
move_arm). Pure chit-chat costs zero vision tokens. The voice listener (if
enabled) routes transcribed queries through the same agentic flow and
replies via DM.

Session memory: conversation history is kept in-memory across turns so
follow-up like "wrong wall, look at the other one" works. After an idle
timeout (config.SESSION_IDLE_S), history clears. Old images get replaced
with placeholders past config.SESSION_MAX_IMAGES to bound vision tokens.
Voice and Discord share the same history.

Doc-edit proposals: Claude's propose_doc_edit tool registers a pending
GOOB.md/MOVEMENT.md edit here (not applied yet). A Discord-text "yes" /
"apply it" within 10 minutes applies it via src.memory. Voice replies never
trigger approval — that's Discord-text only.
"""
from __future__ import annotations

import asyncio
import copy
import io
import logging
import threading
import time
from typing import Optional

import discord

from src import config, gcal, memory
from src.arm import ArmController
from src.camera import Camera
from src.kinematics import solve_look_at
from src.llm import TurnResult, ask_claude

logger = logging.getLogger(__name__)

_PROPOSAL_TIMEOUT_S = 600.0
_APPROVAL_PHRASES = {"yes", "apply", "apply it", "approve", "approved"}


def _trim_old_images(messages: list[dict], keep_last_n: int) -> list[dict]:
    """Replace image blocks beyond the last N with placeholder text.

    Walks newest → oldest, counting image blocks inside tool_result content.
    Once the budget is spent, further (older) image blocks are replaced with
    "[earlier photo elided]". Tool_use/tool_result IDs stay paired, so the
    API still accepts the trimmed transcript. Returns a deep copy.
    """
    out = copy.deepcopy(messages)
    seen = 0
    for msg in reversed(out):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tr_content = block.get("content")
            if not isinstance(tr_content, list):
                continue
            for i in range(len(tr_content) - 1, -1, -1):
                inner = tr_content[i]
                if isinstance(inner, dict) and inner.get("type") == "image":
                    if seen < keep_last_n:
                        seen += 1
                    else:
                        tr_content[i] = {
                            "type": "text",
                            "text": "[earlier photo elided to save tokens]",
                        }
    return out


def _trim_to_complete(messages: list[dict]) -> list[dict]:
    """Drop trailing dangling tool_use blocks.

    Anthropic's API rejects a transcript whose last assistant turn has
    tool_use blocks without matching tool_result follow-ups. If a turn
    truncated at max_turns, the assistant's last message is exactly
    that. Walk back to the last assistant message that's pure text
    (a clean completion) and return everything up to and including it.
    """
    end = len(messages)
    while end > 0:
        m = messages[end - 1]
        if m.get("role") == "assistant":
            content = m.get("content", [])
            if isinstance(content, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            ):
                return messages[:end]
        end -= 1
    return []


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
        # Session memory. Lock guards both fields together so a voice query
        # mid-DM-reply can't read a half-updated history.
        self._history: list[dict] = []
        self._history_at: float = 0.0
        self._history_lock = threading.Lock()
        # Pending propose_doc_edit awaiting Discord-text "yes"/"apply it".
        # Can be set from the voice thread too (ask_claude runs there), so
        # it shares the history lock rather than getting its own.
        self._pending_proposal: Optional[dict] = None

    def _take_prior(self) -> list[dict]:
        """Return the trimmed prior transcript, or [] if expired/empty."""
        with self._history_lock:
            if not self._history:
                return []
            age = time.monotonic() - self._history_at
            if age > config.SESSION_IDLE_S:
                logger.info(
                    "session idle %.0fs > %.0fs cap, forgetting context",
                    age, config.SESSION_IDLE_S,
                )
                self._history = []
                return []
            prior = self._history
        return _trim_old_images(prior, config.SESSION_MAX_IMAGES)

    def _commit_history(self, messages: list[dict]) -> None:
        with self._history_lock:
            self._history = messages
            self._history_at = time.monotonic()

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

    def _look_at(self, x_cm: float, y_cm: float, z_cm: float) -> None:
        # Warm-start from where the arm actually is so the solver prefers a
        # nearby posture and the move stays short. solve_look_at raises
        # KinematicsError for unreachable targets; llm.py turns that into a
        # tool error Claude can read and retry from.
        angles = solve_look_at(x_cm, y_cm, z_cm, self.arm.pose)
        self.arm.move(**angles)

    def _capture(self) -> bytes:
        # Camera reads the arm's current wrist roll so it can rotate the
        # frame back to upright. Lets Claude spin the gripper for fun
        # without breaking image-axis reasoning on subsequent looks.
        return self.camera.capture_jpeg(self.arm.current_wrist_r)

    def _propose_edit(self, file: str, find: str, replace: str, reason: str) -> str:
        # Dry-run validates the find string occurs exactly once before we
        # ever store the proposal, so a bad proposal fails loudly to Claude
        # instead of silently waiting for an approval that can't apply.
        memory.check_doc_edit(file, find)
        with self._history_lock:
            self._pending_proposal = {
                "file": file,
                "find": find,
                "replace": replace,
                "reason": reason,
                "at": time.monotonic(),
            }
        return "proposal registered — awaiting user approval (reply 'yes' or 'apply it')"

    def _get_calendar_events(self, time_min_iso: str, time_max_iso: str, max_results: int) -> list[dict]:
        return gcal.get_events(time_min_iso, time_max_iso, max_results)

    async def _run_turn(self, user_text: str) -> TurnResult:
        prior = self._take_prior()
        turn_started = time.monotonic()
        result = await asyncio.to_thread(
            ask_claude,
            user_text,
            self._capture,
            self.arm.move,
            self.arm.move_to_pose,
            self._look_at,
            self._propose_edit,
            self._get_calendar_events,
            prior,
        )
        # A proposal is only live until the next message: anything pending
        # from before this turn is stale — clear it so a later "yes" meant
        # for something else can't apply an edit the user no longer has in
        # front of them.
        with self._history_lock:
            if (
                self._pending_proposal is not None
                and self._pending_proposal["at"] < turn_started
            ):
                self._pending_proposal = None
        # _trim_to_complete is a no-op on clean completions and salvages
        # the prefix on max-turns truncations.
        self._commit_history(_trim_to_complete(result.messages))
        return result

    def _proposal_suffix(self) -> str:
        # The user must see exactly what they'd be approving — Claude
        # restating it in prose is a hope, not a mechanism.
        with self._history_lock:
            p = self._pending_proposal
        if p is None:
            return ""
        return (
            f"\n\n_(pending edit to {p['file']} — reply \"apply it\" to approve)_\n"
            f"> {p['reason'][:200]}\n"
            f"```diff\n- {p['find'][:300]}\n+ {p['replace'][:300]}\n```"
        )

    def _format_suffix(self, result: TurnResult) -> str:
        suffix = ""
        if result.move_count == 1:
            suffix += "\n_(moved)_"
        elif result.move_count > 1:
            suffix += f"\n_(moved {result.move_count}×)_"
        if result.web_search_count == 1:
            suffix += "\n_(searched online)_"
        elif result.web_search_count > 1:
            suffix += f"\n_(searched online {result.web_search_count}×)_"
        if result.truncated:
            suffix += "\n_(stopped at max moves)_"
        return suffix

    def _attached(self, jpeg: Optional[bytes]) -> list[discord.File]:
        if not self.attach_frame or jpeg is None:
            return []
        return [discord.File(io.BytesIO(jpeg), filename="frame.jpg")]

    async def _maybe_apply_proposal(self, message: discord.Message) -> bool:
        """Handle a Discord-text approval reply. Returns True if handled."""
        text = message.content.strip().lower()
        with self._history_lock:
            pending = self._pending_proposal
            if pending is not None and time.monotonic() - pending["at"] > _PROPOSAL_TIMEOUT_S:
                logger.info("pending proposal expired, clearing")
                self._pending_proposal = None
                pending = None

            if pending is None or text not in _APPROVAL_PHRASES:
                return False
            self._pending_proposal = None
        try:
            result_text = memory.apply_doc_edit(
                pending["file"], pending["find"], pending["replace"]
            )
        except Exception as exc:
            logger.exception("proposal apply failed")
            await message.reply(f"couldn't apply proposal: {exc}"[:1500])
            return True
        # The edit is already on disk here — the confirmation must survive
        # even if the full text (which embeds find/replace) trips Discord's
        # 2000-char limit.
        try:
            await message.reply(
                f"{result_text[:1400]} — applied — takes effect next turn"
            )
        except Exception:
            logger.exception("edit applied but full confirmation failed")
            try:
                await message.reply("edit applied — takes effect next turn")
            except Exception:
                logger.exception("could not deliver apply confirmation at all")
        return True

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return
        if message.author.id != self.owner_id:
            return

        logger.info("dm from %d, %d chars", message.author.id, len(message.content))

        if await self._maybe_apply_proposal(message):
            return

        async with message.channel.typing():
            try:
                result = await self._run_turn(message.content)
            except Exception as exc:
                logger.exception("claude call failed")
                await message.reply(f"brain error: {exc}")
                return
            text = result.text or "(no response)"
            await message.reply(
                (text + self._format_suffix(result) + self._proposal_suffix())[:1990],
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
                (text + self._format_suffix(result) + self._proposal_suffix())[:1990],
                files=self._attached(result.last_jpeg),
            )
