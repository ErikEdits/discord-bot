"""/bot-health: how the bot is doing - sent to you by DM (administrator).

RAM, CPU, uptime, ping, disk space, size of the data folder, extensions that
failed to load and the errors of the last 24 hours (counted by a small log
handler in this cog).
"""

import collections
import logging
import platform
import sys
import time

import discord
import psutil
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, format_duration, is_admin

log = logging.getLogger("setup-bot.bot_health")

START_TIME = time.time()
_PROCESS = psutil.Process()
ERRORS: collections.deque = collections.deque(maxlen=500)   # (time, logger, message)


class _ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ERRORS.append((record.created, record.name, record.getMessage()[:200]))
        except Exception:
            pass


def errors_since(seconds: float, now: float | None = None) -> list[tuple[float, str, str]]:
    now = time.time() if now is None else now
    return [e for e in ERRORS if now - e[0] <= seconds]


def folder_size_mb(path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


def _bar(percent: float) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "█" * filled + "░" * (10 - filled)


def _status_dot(percent: float, warn: float = 75, bad: float = 90) -> str:
    return "\U0001F534" if percent >= bad else "\U0001F7E1" if percent >= warn else "\U0001F7E2"


def health_embed(bot) -> discord.Embed:
    from datetime import timedelta
    try:
        mem = psutil.virtual_memory()
        mem_line = f"{_status_dot(mem.percent)} {_bar(mem.percent)} {mem.percent:.0f}% " \
                   f"({mem.used // 2**20:,} / {mem.total // 2**20:,} MB)"
    except Exception:
        mem_line = "unknown"
    try:
        proc_mb = _PROCESS.memory_info().rss / 2**20
    except Exception:
        proc_mb = 0
    try:
        cpu = psutil.cpu_percent(interval=None)
        cpu_line = f"{_status_dot(cpu)} {_bar(cpu)} {cpu:.0f}% ({psutil.cpu_count() or 1} cores)"
    except Exception:
        cpu_line = "unknown"
    try:
        disk = psutil.disk_usage(str(DATA_DIR))
        disk_line = f"{_status_dot(disk.percent, 80, 95)} {_bar(disk.percent)} {disk.percent:.0f}% " \
                    f"({disk.free / 2**30:.1f} GB free)"
    except Exception:
        disk_line = "unknown"
    latency = bot.latency * 1000 if bot.latency == bot.latency else 0   # NaN before connecting
    errors = errors_since(86400)
    failed = []
    for name in ("__main__", "bot"):   # bot.py runs as __main__ - never import it again
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "FAILED_EXTENSIONS"):
            failed = list(module.FAILED_EXTENSIONS)
            break
    healthy = not failed and len(errors) < 10 and latency < 1000
    embed = discord.Embed(
        title=("\U0001F7E2 Bot is healthy" if healthy else "\U0001F7E1 Bot needs a look"),
        color=0x2ECC71 if healthy else 0xF1C40F,
        timestamp=discord.utils.utcnow())
    embed.add_field(name="Uptime", value=format_duration(timedelta(seconds=int(time.time() - START_TIME))), inline=True)
    embed.add_field(name="Ping", value=f"{latency:.0f} ms", inline=True)
    embed.add_field(name="Bot RAM", value=f"{proc_mb:.0f} MB", inline=True)
    embed.add_field(name="RAM (server)", value=mem_line, inline=False)
    embed.add_field(name="CPU", value=cpu_line, inline=False)
    embed.add_field(name="Disk", value=disk_line, inline=False)
    embed.add_field(name="Data folder", value=f"{folder_size_mb(DATA_DIR):.1f} MB", inline=True)
    embed.add_field(name="Extensions", value=f"{len(bot.extensions)} loaded"
                    + (f", **{len(failed)} failed**" if failed else ""), inline=True)
    embed.add_field(name="Commands", value=str(len(bot.tree.get_commands())), inline=True)
    if failed:
        embed.add_field(name="❌ Failed to load", value=", ".join(f"`{f}`" for f in failed)[:1000], inline=False)
    if errors:
        last = "\n".join(f"<t:{int(t)}:R> `{name.replace('setup-bot.', '')}`: {msg[:90]}"
                         for t, name, msg in errors[-3:])
        embed.add_field(name=f"Errors (24h): {len(errors)}", value=last[:1024], inline=False)
    else:
        embed.add_field(name="Errors (24h)", value="none \U0001F389", inline=False)
    embed.set_footer(text=f"Python {platform.python_version()} · discord.py {discord.__version__} · {sys.platform}")
    return embed


class BotHealth(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.handler = _ErrorCounter()
        logging.getLogger().addHandler(self.handler)
        psutil.cpu_percent(interval=None)   # prime, so the first real value is useful

    def cog_unload(self):
        logging.getLogger().removeHandler(self.handler)

    @app_commands.command(name="bot-health",
                          description="How the bot is doing (RAM, CPU, errors ...) - sent to you by DM.")
    @app_commands.default_permissions(administrator=True)
    async def health(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        try:
            await interaction.user.send(embed=health_embed(self.bot))
        except discord.HTTPException:
            await interaction.response.send_message("I can't DM you - please allow DMs from server members.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message("\U0001F4EC Sent you the bot health by DM.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(BotHealth(bot))
