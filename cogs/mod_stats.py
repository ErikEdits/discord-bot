"""Download statistics for the Modrinth projects.

Posts an embed with downloads + followers per project (and the change since the
last post) to CHANNELS["mod_stats"] - weekly or daily, see
SERVER_TEMPLATE["mod_stats"]. Only one small snapshot is kept in
data/mod_stats.json to compute the difference.

    /modstats       show the current numbers (only you see it)
    /modstats-post  post the statistics now (administrator)
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.mod_stats")

STATE_FILE = DATA_DIR / "mod_stats.json"


def _config() -> dict:
    return SERVER_TEMPLATE.get("mod_stats", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_config().get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def _period_label() -> str:
    return "today" if _config().get("interval", "weekly") == "daily" else "this week"


def _delta(now: int, before: int | None) -> str:
    if before is None:
        return ""
    diff = now - before
    return f" ({'+' if diff >= 0 else ''}{diff:,})"


async def _fetch_projects() -> list[dict] | None:
    username = SERVER_TEMPLATE.get("modrinth", {}).get("username")
    if not username:
        return None
    from cogs.modrinth import _fetch_user_projects
    return await _fetch_user_projects(username)


def build_stats_embed(projects: list[dict], previous: dict, period: str) -> discord.Embed:
    projects = sorted(projects, key=lambda p: p.get("downloads", 0), reverse=True)
    total = sum(p.get("downloads", 0) for p in projects)
    prev_total = sum(v.get("downloads", 0) for v in previous.values()) if previous else None
    followers = sum(p.get("followers", 0) for p in projects)
    prev_followers = sum(v.get("followers", 0) for v in previous.values()) if previous else None

    username = SERVER_TEMPLATE.get("modrinth", {}).get("username", "")
    embed = discord.Embed(
        title="\U0001F4C8 Mod download statistics",
        url=f"https://modrinth.com/user/{username}" if username else None,
        description=(
            f"**Total downloads:** {total:,}{_delta(total, prev_total)}\n"
            f"**Total followers:** {followers:,}{_delta(followers, prev_followers)}"
            + (f"\n*Numbers in brackets = change {period}.*" if previous else "")
        ),
        color=0x1BD96A,
        timestamp=datetime.now(timezone.utc),
    )
    for p in projects[:20]:
        before = previous.get(p.get("id"), {}) if previous else {}
        dl = p.get("downloads", 0)
        fol = p.get("followers", 0)
        dl_prev = before.get("downloads") if previous else None
        fol_prev = before.get("followers") if previous else None
        if previous and not before:
            dl_prev, fol_prev = 0, 0  # project is new since the last post
        embed.add_field(
            name=str(p.get("title", "Unknown"))[:256],
            value=f"⬇️ **{dl:,}**{_delta(dl, dl_prev)} · ❤️ {fol:,}{_delta(fol, fol_prev)}",
            inline=False,
        )
    if len(projects) > 20:
        embed.set_footer(text=f"+{len(projects) - 20} more projects")
    else:
        embed.set_footer(text="Modrinth")
    return embed


def _snapshot(projects: list[dict]) -> dict:
    return {
        p["id"]: {"downloads": p.get("downloads", 0), "followers": p.get("followers", 0), "title": p.get("title")}
        for p in projects if p.get("id")
    }


class ModStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if _config().get("enabled", True):
            self.schedule_loop.start()

    def cog_unload(self):
        if self.schedule_loop.is_running():
            self.schedule_loop.cancel()

    def _is_due(self, state: dict) -> bool:
        cfg = _config()
        now = datetime.now(_tz())
        if now.hour < int(cfg.get("hour", 10)):
            return False
        if cfg.get("interval", "weekly") != "daily" and now.weekday() != int(cfg.get("weekday", 0)):
            return False
        return state.get("last_post_date") != now.date().isoformat()

    async def post_stats(self) -> int:
        """Post to every guild's stats channel. Returns how many channels got it."""
        projects = await _fetch_projects()
        if not projects:
            log.warning("Mod stats: could not fetch projects")
            return 0
        state = load_json(STATE_FILE)
        embed = build_stats_embed(projects, state.get("snapshot", {}), _period_label())
        posted = 0
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=CHANNELS.get("mod_stats", ""))
            if channel is None:
                continue
            try:
                await channel.send(embed=embed)
                posted += 1
            except discord.Forbidden:
                log.warning("Cannot post in #%s", channel.name)
        state["snapshot"] = _snapshot(projects)
        state["last_post_date"] = datetime.now(_tz()).date().isoformat()
        save_json(STATE_FILE, state)
        log.info("Posted mod stats to %d channel(s)", posted)
        return posted

    @tasks.loop(minutes=15)
    async def schedule_loop(self):
        state = load_json(STATE_FILE)
        if self._is_due(state):
            await self.post_stats()

    @schedule_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="modstats", description="Show the current download numbers of all mods.")
    async def modstats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        projects = await _fetch_projects()
        if not projects:
            await interaction.followup.send("Couldn't load the projects from Modrinth.", ephemeral=True)
            return
        state = load_json(STATE_FILE)
        embed = build_stats_embed(projects, state.get("snapshot", {}), "since the last stats post")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="modstats-post", description="Post the download statistics now (administrator).")
    @app_commands.default_permissions(administrator=True)
    async def modstats_post(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = await self.post_stats()
        if posted:
            await interaction.followup.send("Statistics posted.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"Nothing posted - is there a #{CHANNELS.get('mod_stats')} channel? (run `/update`)", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(ModStats(bot))
