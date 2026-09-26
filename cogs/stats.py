"""Collects statistics for the web panel's Stats page.

Per server and day: member count, joins, leaves, messages (total and per
channel), tickets opened/closed. Plus ticket ratings and who claimed tickets.
Only counts are stored - never message content. Message counts are kept in
memory and written to data/stats.json every 5 minutes. Days older than
SERVER_TEMPLATE["stats"]["keep_days"] are dropped.

Other cogs report events with record_event(...) / record_rating(...) /
record_claim(...); these do nothing if this cog isn't loaded (LOW_POWER).
"""

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.stats")

STATS_FILE = DATA_DIR / "stats.json"
MAX_RATINGS = 1000

_ACTIVE: "Stats | None" = None


def _config() -> dict:
    return SERVER_TEMPLATE.get("stats", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(SERVER_TEMPLATE.get("backups", {}).get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def today_key() -> str:
    return datetime.now(_tz()).date().isoformat()


def load_stats() -> dict:
    data = load_json(STATS_FILE)
    data.setdefault("guilds", {})
    return data


def _day(data: dict, guild_id: int, day: str | None = None) -> dict:
    g = data["guilds"].setdefault(str(guild_id), {"days": {}, "ratings": [], "claims": {}, "channel_names": {}})
    return g["days"].setdefault(day or today_key(), {
        "members": None, "joins": 0, "leaves": 0, "messages": 0, "channels": {},
        "tickets_opened": 0, "tickets_closed": 0,
    })


def record_event(guild_id: int, key: str, amount: int = 1) -> None:
    if _ACTIVE is not None:
        day = _day(_ACTIVE.data, guild_id)
        day[key] = day.get(key, 0) + amount
        _ACTIVE.dirty = True


def record_rating(guild_id: int, stars: int) -> None:
    if _ACTIVE is not None:
        _day(_ACTIVE.data, guild_id)
        ratings = _ACTIVE.data["guilds"][str(guild_id)]["ratings"]
        ratings.append({"ts": time.time(), "stars": stars})
        del ratings[:-MAX_RATINGS]
        _ACTIVE.dirty = True


def record_claim(guild_id: int, user: discord.abc.User) -> None:
    if _ACTIVE is not None:
        _day(_ACTIVE.data, guild_id)
        claims = _ACTIVE.data["guilds"][str(guild_id)]["claims"]
        entry = claims.setdefault(str(user.id), {"name": str(user), "count": 0})
        entry["name"] = str(user)
        entry["count"] += 1
        _ACTIVE.dirty = True


class Stats(commands.Cog):
    def __init__(self, bot):
        global _ACTIVE
        self.bot = bot
        self.data = load_stats()
        self.dirty = False
        _ACTIVE = self
        self.flush_loop.start()

    def cog_unload(self):
        global _ACTIVE
        if self.flush_loop.is_running():
            self.flush_loop.cancel()
        self.flush()
        if _ACTIVE is self:
            _ACTIVE = None

    def snapshot_members(self) -> None:
        for guild in self.bot.guilds:
            day = _day(self.data, guild.id)
            if day.get("members") != guild.member_count:
                day["members"] = guild.member_count
                self.dirty = True

    def prune(self) -> None:
        cutoff = (datetime.now(_tz()).date() - timedelta(days=int(_config().get("keep_days", 180)))).isoformat()
        for g in self.data["guilds"].values():
            for key in [k for k in g["days"] if k < cutoff]:
                del g["days"][key]
                self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            save_json(STATS_FILE, self.data)
            self.dirty = False

    @tasks.loop(minutes=5)
    async def flush_loop(self):
        self.snapshot_members()
        self.prune()
        self.flush()

    @flush_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        day = _day(self.data, message.guild.id)
        day["messages"] += 1
        channel = message.channel
        # Messages in threads/forum posts count for their parent channel.
        parent = getattr(channel, "parent", None)
        if isinstance(channel, discord.Thread) and parent is not None:
            channel = parent
        cid = str(channel.id)
        day["channels"][cid] = day["channels"].get(cid, 0) + 1
        self.data["guilds"][str(message.guild.id)]["channel_names"][cid] = getattr(channel, "name", cid)
        self.dirty = True

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        record_event(member.guild.id, "joins")
        _day(self.data, member.guild.id)["members"] = member.guild.member_count

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        record_event(member.guild.id, "leaves")
        _day(self.data, member.guild.id)["members"] = member.guild.member_count


async def setup(bot):
    await bot.add_cog(Stats(bot))
