"""Minecraft chat log: the in-game chat in a Discord channel only the owner can see.

Chat lines (PLAYER_CHAT) arriving from the Minecraft plugin (Cloudflare or the
Discord log channel, see cogs/mc_logs.py) are collected and posted every few seconds
as "HH:MM Player: message" into CHANNELS["mc_chat_log"]. The channel is created if
missing and is visible to the Owner role only. Read-only - nothing goes back into
the game. Config: SERVER_TEMPLATE["mc_chat_log"].
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from cogs.common import feature_on
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.mc_chat_log")

MAX_QUEUE = 2000


def _config() -> dict:
    return SERVER_TEMPLATE.get("mc_chat_log", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(SERVER_TEMPLATE.get("mc_logs", {}).get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def chat_text(ev) -> str:
    """The message of a chat event (txt, else what's after 'Name:' in the raw line)."""
    if getattr(ev, "txt", None):
        return ev.txt
    raw = getattr(ev, "raw", "") or ""
    body = raw.split("|", 1)[-1] if "|" in raw else raw
    if ":" in body:
        return body.split(":", 1)[1].strip()
    return getattr(ev, "obj", None) or body.strip()


def format_lines(events, tz) -> list[str]:
    out = []
    for ev in events:
        when = datetime.fromtimestamp(ev.ts, tz).strftime("%H:%M")
        text = discord.utils.escape_mentions(discord.utils.escape_markdown(chat_text(ev)))[:300]
        out.append(f"`{when}` **{discord.utils.escape_markdown(ev.player or '?')}**: {text}")
    return out


class McChatLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue: list = []
        self.lock = asyncio.Lock()
        self.flush_loop.start()

    def cog_unload(self):
        self.flush_loop.cancel()

    @commands.Cog.listener()
    async def on_mc_events(self, events, source):
        if not _config().get("enabled", True) or not feature_on("mc_chat_log"):
            return
        chats = [e for e in events if getattr(e, "type", "") == "PLAYER_CHAT"]
        if chats:
            self.queue.extend(chats)
            del self.queue[:-MAX_QUEUE]

    async def _channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        name = CHANNELS["mc_chat_log"]
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch is not None:
            return ch
        owner_role = discord.utils.get(guild.roles, name=_config().get("role", "Owner"))
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        if owner_role is not None:
            overwrites[owner_role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                                 send_messages=False)
        anchor = discord.utils.get(guild.text_channels, name=CHANNELS.get("mod_logs"))
        try:
            return await guild.create_text_channel(name, overwrites=overwrites, category=anchor.category if anchor else None,
                                                   topic="Minecraft chat (read-only log, owner only).",
                                                   reason="Minecraft chat log")
        except discord.HTTPException:
            log.exception("Couldn't create #%s", name)
            return None

    @tasks.loop(seconds=5)
    async def flush_loop(self):
        if not self.queue:
            return
        async with self.lock:
            events, self.queue = self.queue, []
            lines = format_lines(sorted(events, key=lambda e: e.ts), _tz())
            chunks, cur = [], ""
            for line in lines:
                if len(cur) + len(line) + 1 > 1900:
                    chunks.append(cur)
                    cur = ""
                cur = f"{cur}\n{line}" if cur else line
            if cur:
                chunks.append(cur)
            for guild in self.bot.guilds:
                ch = await self._channel(guild)
                if ch is None:
                    continue
                for chunk in chunks:
                    try:
                        await ch.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        log.warning("Couldn't post MC chat lines")
                        break

    @flush_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(McChatLog(bot))
