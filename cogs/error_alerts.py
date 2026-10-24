"""Error alerts by DM.

Every ERROR logged by the bot (a crashed command, a failed background job, ...)
is sent as a DM to all members with the Administrator permission. Errors are
collected for a few seconds and bundled; the same error is only reported once
per `min_minutes_between_same_error`.

Short Discord connection hiccups (Discord answers 503, the gateway drops, the bot
reconnects a second later) are NOT reported - they're Discord's problem and fix
themselves. Instead, if the bot was really offline for longer than
`offline_alert_minutes`, the admins get one DM after it's back.

    /error-alerts off   stop the DMs for yourself
    /error-alerts on    turn them back on
    /error-alerts test  send yourself a test alert

Config: SERVER_TEMPLATE["error_alerts"].
"""

import asyncio
import logging
import time
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import get_setting, is_admin, set_setting
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.error_alerts")

OPTOUT_KEY = "error_alerts_optout"
BATCH_SECONDS = 10
MAX_ALERTS_PER_HOUR = 6


def _config() -> dict:
    return SERVER_TEMPLATE.get("error_alerts", {})


CONNECTION_ERRORS = ("WSServerHandshakeError", "ClientConnectorError", "ClientConnectionError", "ClientOSError",
                     "ServerDisconnectedError", "ConnectionClosed", "ConnectionResetError", "GatewayNotFound",
                     "TimeoutError", "CancelledError", "OSError", "WSServerDisconnectedError")


def is_connection_noise(record: logging.LogRecord) -> bool:
    """Discord connection drops that discord.py handles by reconnecting on its own."""
    if not record.name.startswith("discord."):
        return False
    msg = str(record.msg)
    if msg.startswith("Attempting a reconnect") or "reconnect" in msg.lower():
        return True
    exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
    return exc in CONNECTION_ERRORS and record.name in ("discord.client", "discord.gateway", "discord.shard")


def offline_embed(since: float, back: float) -> discord.Embed:
    minutes = max(1, round((back - since) / 60))
    embed = discord.Embed(
        title="\U0001F50C The bot was offline",
        description=f"The connection to Discord was gone for **{minutes} minute{'s' if minutes != 1 else ''}** "
                    f"(<t:{int(since)}:t> - <t:{int(back)}:t>). It's back now and works normally.\n\n"
                    "Usually that's a Discord outage or the host restarting - nothing to do. "
                    "If it happens often, check the hosting panel.",
        color=0xE67E22,
        timestamp=datetime.fromtimestamp(back, timezone.utc))
    embed.set_footer(text="Short reconnects (a few seconds) are not reported.")
    return embed


class _AlertHandler(logging.Handler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        super().__init__(level=logging.ERROR)
        self.loop = loop
        self.queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("setup-bot.error_alerts"):
            return  # never alert about alerting
        if not (record.name.startswith("setup-bot") or record.name.startswith("discord")):
            return
        if is_connection_noise(record):
            return
        try:
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            item = {
                "logger": record.name,
                "key": (record.name, str(record.msg)[:200], record.exc_info[0].__name__ if record.exc_info else ""),
                "message": record.getMessage()[:500],
                "traceback": tb,
                "time": record.created,
            }
            self.loop.call_soon_threadsafe(self.queue.put_nowait, item)
        except Exception:
            pass


class ErrorAlerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue()
        self.last_sent: dict[tuple, float] = {}
        self.sent_times: list[float] = []
        self.handler: _AlertHandler | None = None
        self.worker: asyncio.Task | None = None
        self.offline_since: float | None = None

    async def cog_load(self):
        if not _config().get("enabled", True):
            return
        self.handler = _AlertHandler(asyncio.get_running_loop(), self.queue)
        logging.getLogger().addHandler(self.handler)
        self.worker = asyncio.create_task(self._run(), name="error-alerts")

    async def cog_unload(self):
        if self.handler:
            logging.getLogger().removeHandler(self.handler)
        if self.worker:
            self.worker.cancel()

    # -------- Real outages (instead of every reconnect) ---------------------

    @commands.Cog.listener()
    async def on_disconnect(self):
        if self.offline_since is None:
            self.offline_since = time.time()

    @commands.Cog.listener()
    async def on_resumed(self):
        await self._back_online()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._back_online()

    async def _back_online(self) -> None:
        since, self.offline_since = self.offline_since, None
        if since is None or not _config().get("enabled", True):
            return
        minutes = (time.time() - since) / 60
        if minutes < float(_config().get("offline_alert_minutes", 5)):
            return
        log.info("Bot was offline for %.0f minutes", minutes)
        embed = offline_embed(since, time.time())
        from cogs.push import safe_notify
        safe_notify("error", "\U0001F50C Bot was offline", f"for {minutes:.0f} minutes - it's back now", "/logs")
        for member in self._recipients():
            try:
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    def _recipients(self) -> list[discord.Member]:
        optout = set(get_setting(OPTOUT_KEY) or [])
        seen, out = set(), []
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.bot or member.id in seen or member.id in optout:
                    continue
                if member.guild_permissions.administrator:
                    seen.add(member.id)
                    out.append(member)
        return out

    def _build_embed(self, items: list[dict]) -> discord.Embed:
        first = items[0]
        embed = discord.Embed(
            title="⚠️ Bot error" + (f" (+{len(items) - 1} more)" if len(items) > 1 else ""),
            description=f"**Where:** `{first['logger']}`\n**What:** {first['message']}",
            color=0xE74C3C,
            timestamp=datetime.fromtimestamp(first["time"], timezone.utc),
        )
        if first["traceback"]:
            tb = first["traceback"].strip()
            embed.add_field(name="Traceback (end)", value=f"```py\n{tb[-900:]}\n```", inline=False)
        others = [f"`{i['logger']}`: {i['message'][:150]}" for i in items[1:6]]
        if others:
            embed.add_field(name="Also", value="\n".join(others)[:1024], inline=False)
        embed.set_footer(text="Turn these DMs off for yourself with /error-alerts off")
        return embed

    async def _send(self, items: list[dict]) -> None:
        now = time.time()
        self.sent_times = [t for t in self.sent_times if now - t < 3600]
        if len(self.sent_times) >= MAX_ALERTS_PER_HOUR:
            log.warning("Error alerts: hourly limit reached, %d error(s) not sent", len(items))
            return
        self.sent_times.append(now)
        embed = self._build_embed(items)
        from cogs.push import safe_notify
        safe_notify("error", "\u26A0 Bot error", f"{items[0]['logger']}: {items[0]['message']}"[:300], "/logs")
        for member in self._recipients():
            try:
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        min_gap = float(_config().get("min_minutes_between_same_error", 30)) * 60
        while True:
            item = await self.queue.get()
            batch = [item]
            await asyncio.sleep(BATCH_SECONDS)
            while not self.queue.empty():
                batch.append(self.queue.get_nowait())
            now = time.time()
            fresh, keys = [], set()
            for i in batch:
                if i["key"] in keys or now - self.last_sent.get(i["key"], 0) < min_gap:
                    continue
                keys.add(i["key"])
                self.last_sent[i["key"]] = now
                fresh.append(i)
            if fresh:
                try:
                    await self._send(fresh)
                except Exception:
                    pass

    group = app_commands.Group(
        name="error-alerts",
        description="Error DMs for administrators.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @group.command(name="off", description="Stop error DMs for yourself.")
    async def off(self, interaction: discord.Interaction):
        optout = set(get_setting(OPTOUT_KEY) or [])
        optout.add(interaction.user.id)
        set_setting(OPTOUT_KEY, sorted(optout))
        await interaction.response.send_message("You won't get error DMs anymore.", ephemeral=True)

    @group.command(name="on", description="Get error DMs again.")
    async def on(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators get error DMs.", ephemeral=True)
            return
        optout = set(get_setting(OPTOUT_KEY) or [])
        optout.discard(interaction.user.id)
        set_setting(OPTOUT_KEY, sorted(optout))
        await interaction.response.send_message("Error DMs are on for you.", ephemeral=True)

    @group.command(name="test", description="Send yourself a test error alert.")
    async def test(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        embed = self._build_embed([{
            "logger": "setup-bot.test", "message": "This is a test alert - everything works.",
            "traceback": "", "time": time.time(), "key": ("test",),
        }])
        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("Test alert sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I can't DM you - allow DMs from server members.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ErrorAlerts(bot))
