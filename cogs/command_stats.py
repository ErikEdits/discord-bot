"""Command statistics: which slash commands, right-click menus and buttons are used.

Counts per day (kept `keep_days`, default 90) in data/command_stats.json, written
every 5 minutes. The web panel page "Commands" shows them, including commands that
were never used. Only counts are stored - not who used them.
"""

import logging
from collections import Counter
from datetime import date, timedelta

import discord
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.command_stats")

STATE_FILE = DATA_DIR / "command_stats.json"


def _config() -> dict:
    return SERVER_TEMPLATE.get("command_stats", {})


def component_key(custom_id: str) -> str:
    """'ticket:close' -> 'ticket:close', 'report:del:1:2:3' -> 'report:del' (ids dropped)."""
    parts = [p for p in custom_id.split(":") if p and not p.isdigit() and not (len(p) >= 8 and all(c in "0123456789abcdef" for c in p))]
    return ":".join(parts[:2]) or custom_id[:30]


def load() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("days", {})      # "2026-10-16" -> {"/ban": 3, "button ticket:close": 1}
    data.setdefault("last_used", {})  # key -> unix time
    return data


def summary(data: dict, today: date | None = None) -> dict[str, dict]:
    """key -> {"total", "week", "last"} over the kept days."""
    today = today or date.today()
    week = {(today - timedelta(days=i)).isoformat() for i in range(7)}
    total, recent = Counter(), Counter()
    for day, counts in data["days"].items():
        total.update(counts)
        if day in week:
            recent.update(counts)
    return {k: {"total": total[k], "week": recent[k], "last": data["last_used"].get(k)} for k in total}


class CommandStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load()
        self.dirty = False
        self.flush_loop.start()

    def cog_unload(self):
        self.flush_loop.cancel()
        self.flush()

    def count(self, key: str) -> None:
        import time
        day = date.today().isoformat()
        bucket = self.data["days"].setdefault(day, {})
        bucket[key] = bucket.get(key, 0) + 1
        self.data["last_used"][key] = int(time.time())
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        keep = int(_config().get("keep_days", 90))
        cutoff = (date.today() - timedelta(days=keep)).isoformat()
        for day in [d for d in self.data["days"] if d < cutoff]:
            del self.data["days"][day]
        save_json(STATE_FILE, self.data)
        self.dirty = False

    @tasks.loop(minutes=5)
    async def flush_loop(self):
        self.flush()

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        name = getattr(command, "qualified_name", None) or getattr(command, "name", "?")
        if isinstance(command, discord.app_commands.ContextMenu):
            self.count(f"menu {name}")
        else:
            self.count(f"/{name}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            cid = (interaction.data or {}).get("custom_id", "")
            if cid:
                self.count(f"button {component_key(cid)}")


def all_commands(bot) -> list[str]:
    """Every registered slash command / subcommand / menu as a stats key."""
    keys = []
    for cmd in bot.tree.get_commands():
        if isinstance(cmd, discord.app_commands.ContextMenu):
            keys.append(f"menu {cmd.name}")
        elif isinstance(cmd, discord.app_commands.Group):
            for sub in cmd.walk_commands():
                if not isinstance(sub, discord.app_commands.Group):
                    keys.append(f"/{sub.qualified_name}")
        else:
            keys.append(f"/{cmd.name}")
    for cmd in bot.tree.get_commands(type=discord.AppCommandType.message):
        keys.append(f"menu {cmd.name}")
    return sorted(set(keys))


async def setup(bot):
    await bot.add_cog(CommandStats(bot))
