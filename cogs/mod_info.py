"""Mod information from Modrinth.

    /mod <name>                 info card: description, versions, loaders, downloads, links
    /changelog <mod> [count]    changelogs of the latest versions
    /compatibility-refresh      (admin) redraw the compatibility table now

Background jobs:
- Compatibility table in CHANNELS["compatibility"]: which mod supports which
  Minecraft versions per loader. Refreshed every few hours and after releases.
- Download milestones (100, 1k, 10k, ...) announced in the stats channel.
  The first time a mod is seen, its current milestone is only recorded.
- Release feedback: N days after a new release (announced by the Modrinth
  watcher) a feedback poll is posted in the polls channel. On/off, delay and
  poll duration: /poll auto (stored in data/mod_info.json).

Config: SERVER_TEMPLATE["mod_info"]. State: data/mod_info.json.
"""

import logging
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.mod_info")

STATE_FILE = DATA_DIR / "mod_info.json"
_RELEASE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def _config() -> dict:
    return SERVER_TEMPLATE.get("mod_info", {})


def _username() -> str:
    return SERVER_TEMPLATE.get("modrinth", {}).get("username", "")


def _load() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("compat", {})
    data.setdefault("milestones", {})
    data.setdefault("feedback", [])
    return data


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def compress_versions(versions: list[str], max_listed: int = 7) -> str:
    """'1.20.1, 1.21, 1.21.1' or '1.16.5 – 1.21.1 (14 versions)' for long lists."""
    ordered = sorted(set(versions), key=_vkey)
    if not ordered:
        return "-"
    if len(ordered) <= max_listed:
        return ", ".join(ordered)
    return f"{ordered[0]} – {ordered[-1]} ({len(ordered)} versions)"


def format_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def loader_support(versions: list[dict]) -> dict[str, set[str]]:
    """{loader: {game versions}} across all published versions (release MC versions only)."""
    out: dict[str, set[str]] = {}
    for v in versions or []:
        game = [g for g in v.get("game_versions") or [] if _RELEASE.match(g)]
        for loader in v.get("loaders") or []:
            out.setdefault(loader, set()).update(game)
    return out


def build_compat_embed(rows: list[tuple[dict, dict[str, set[str]]]]) -> discord.Embed:
    from cogs.modrinth import LOADER_EMOJI, LOADER_LABELS
    embed = discord.Embed(
        title="\U0001F9E9 Compatibility",
        description="Which Minecraft versions each mod supports, per loader. "
                    "Get the right file in the mod downloads channel.",
        color=0x1BD96A,
        timestamp=datetime.now(timezone.utc),
    )
    for project, support in rows[:25]:
        lines = []
        for loader in sorted(support, key=lambda l: list(LOADER_LABELS).index(l) if l in LOADER_LABELS else 99):
            if not support[loader]:
                continue
            emoji = LOADER_EMOJI.get(loader, "•")
            lines.append(f"{emoji} **{LOADER_LABELS.get(loader, loader.title())}:** {compress_versions(list(support[loader]))}")
        url = f"https://modrinth.com/{project.get('project_type', 'mod')}/{project.get('slug')}"
        value = ("\n".join(lines) or "*no versions yet*") + f"\n[Modrinth]({url})"
        embed.add_field(name=str(project.get("title", "?"))[:256], value=value[:1024], inline=False)
    embed.set_footer(text="Updated automatically")
    return embed


def auto_poll_settings() -> dict:
    """Release feedback polls: changed with /poll auto, defaults from SERVER_TEMPLATE["mod_info"]."""
    cfg = _config()
    saved = _load().get("auto_poll", {})
    default_days = float(cfg.get("feedback_after_days", 3))
    return {
        "enabled": bool(saved.get("enabled", default_days > 0)),
        "delay_hours": float(saved.get("delay_hours", max(default_days, 0) * 24)),
        "duration_hours": float(saved.get("duration_hours", cfg.get("feedback_poll_hours", 72))),
    }


def save_auto_poll_settings(**changes) -> dict:
    data = _load()
    data.setdefault("auto_poll", {}).update(changes)
    save_json(STATE_FILE, data)
    return auto_poll_settings()


def pending_feedback() -> list[dict]:
    return _load()["feedback"]


def _feedback_due(entry: dict, delay_hours: float) -> float:
    # "released" lets a changed delay apply to polls that are already waiting.
    if "released" in entry:
        return entry["released"] + delay_hours * 3600
    return entry["due"]


def schedule_release_feedback(project: dict, version: dict) -> None:
    """Called by the Modrinth watcher when a new version was announced."""
    if _ACTIVE is not None:
        _ACTIVE.compat_dirty = True  # new version -> redraw the compatibility table
    if version.get("version_type", "release") != "release" or not auto_poll_settings()["enabled"]:
        return
    data = _load()
    if any(f.get("version_id") == version.get("id") for f in data["feedback"]):
        return
    data["feedback"].append({
        "version_id": version.get("id"),
        "title": project.get("title", "?"),
        "version_number": version.get("version_number", ""),
        "released": time.time(),
    })
    save_json(STATE_FILE, data)


_ACTIVE: "ModInfo | None" = None


class ModInfo(commands.Cog):
    def __init__(self, bot):
        global _ACTIVE
        self.bot = bot
        self.compat_dirty = True
        self._last_compat = 0.0
        _ACTIVE = self
        if SERVER_TEMPLATE.get("modrinth", {}).get("enabled") and _username():
            self.background_loop.start()

    def cog_unload(self):
        global _ACTIVE
        if self.background_loop.is_running():
            self.background_loop.cancel()
        if _ACTIVE is self:
            _ACTIVE = None

    async def _projects(self) -> list[dict]:
        modrinth = self.bot.get_cog("Modrinth")
        if modrinth is not None:
            return await modrinth._cached_projects()
        from cogs.modrinth import _fetch_user_projects
        return await _fetch_user_projects(_username()) or []

    # -------- Background ----------------------------------------------------

    @tasks.loop(minutes=30)
    async def background_loop(self):
        from cogs.modrinth import _fetch_user_projects
        projects = await _fetch_user_projects(_username())
        if not projects:
            return
        await self._check_milestones(projects)
        await self._post_due_feedback()
        hours = float(_config().get("compatibility_refresh_hours", 6))
        if self.compat_dirty or time.time() - self._last_compat > hours * 3600:
            await self.refresh_compat(projects)

    @background_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def refresh_compat(self, projects: list[dict] | None = None) -> int:
        """Redraw the compatibility table in every guild. Returns the number of guilds updated."""
        from cogs.modrinth import API_BASE, _fetch
        from cogs.modrinth import _fetch_user_projects
        projects = projects if projects is not None else await _fetch_user_projects(_username())
        if not projects:
            return 0
        rows = []
        async with aiohttp.ClientSession() as session:
            for p in sorted(projects, key=lambda p: p.get("downloads", 0), reverse=True):
                versions = await _fetch(session, f"{API_BASE}/project/{p['id']}/version") or []
                rows.append((p, loader_support(versions)))
        embed = build_compat_embed(rows)
        data = _load()
        updated = 0
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=CHANNELS.get("compatibility", ""))
            if channel is None:
                continue
            message = None
            mid = data["compat"].get(str(guild.id))
            if mid:
                try:
                    message = await channel.fetch_message(mid)
                except (discord.NotFound, discord.Forbidden):
                    message = None
            try:
                if message:
                    await message.edit(embed=embed)
                else:
                    message = await channel.send(embed=embed)
                    data["compat"][str(guild.id)] = message.id
                updated += 1
            except discord.HTTPException:
                log.warning("Could not update compatibility table in %s", guild.name)
        save_json(STATE_FILE, data)
        self.compat_dirty = False
        self._last_compat = time.time()
        return updated

    async def _check_milestones(self, projects: list[dict]) -> None:
        steps = sorted(int(m) for m in _config().get("milestones", []))
        if not steps:
            return
        data = _load()
        reached = []
        for p in projects:
            pid, downloads = p.get("id"), int(p.get("downloads", 0))
            best = max((m for m in steps if downloads >= m), default=0)
            known = data["milestones"].get(pid)
            if known is None:
                data["milestones"][pid] = best  # first sight: just remember
            elif best > known:
                data["milestones"][pid] = best
                reached.append((p, best))
        save_json(STATE_FILE, data)
        for project, milestone in reached:
            await self._announce_milestone(project, milestone)

    async def _announce_milestone(self, project: dict, milestone: int) -> None:
        url = f"https://modrinth.com/{project.get('project_type', 'mod')}/{project.get('slug')}"
        embed = discord.Embed(
            title=f"\U0001F389 {project.get('title')} reached {milestone:,} downloads!",
            description=f"Thank you all for downloading and playing! ❤️\n[View on Modrinth]({url})",
            color=0xF1C40F,
            timestamp=datetime.now(timezone.utc),
        )
        if project.get("icon_url"):
            embed.set_thumbnail(url=project["icon_url"])
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=_config().get("milestone_channel", ""))
            if channel is None:
                continue
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass
        log.info("Milestone: %s reached %d downloads", project.get("title"), milestone)

    async def _post_due_feedback(self) -> None:
        data = _load()
        settings = auto_poll_settings()
        now = time.time()
        due = [f for f in data["feedback"] if _feedback_due(f, settings["delay_hours"]) <= now]
        if not due:
            return
        poll_cog = self.bot.get_cog("Poll")
        for f in due:
            if not settings["enabled"]:
                log.info("Release feedback poll for %s %s skipped (automatic polls are off)",
                         f["title"], f["version_number"])
            elif poll_cog is not None:
                question = f"How is {f['title']} {f['version_number']} working for you?"
                options = list(_config().get("feedback_options", ["Works great", "Small issues", "Crashes / broken"]))
                for guild in self.bot.guilds:
                    try:
                        await poll_cog.start_poll(guild, question[:250], options, settings["duration_hours"], 0)
                        log.info("Posted release feedback poll for %s %s", f["title"], f["version_number"])
                    except ValueError as e:
                        log.warning("Release feedback poll skipped in %s: %s", guild.name, e)
        data = _load()
        due_ids = {f["version_id"] for f in due}
        data["feedback"] = [f for f in data["feedback"] if f["version_id"] not in due_ids]
        save_json(STATE_FILE, data)

    # -------- Commands ------------------------------------------------------

    async def _mod_autocomplete(self, interaction: discord.Interaction, current: str):
        projects = await self._projects()
        return [
            app_commands.Choice(name=str(p.get("title", ""))[:100], value=str(p.get("slug", ""))[:100])
            for p in projects if current.lower() in str(p.get("title", "")).lower()
        ][:25]

    @app_commands.command(name="mod", description="Show info about one of the mods.")
    @app_commands.describe(name="Start typing to pick a mod")
    @app_commands.autocomplete(name=_mod_autocomplete)
    async def mod(self, interaction: discord.Interaction, name: str):
        from cogs.modrinth import API_BASE, LOADER_EMOJI, LOADER_LABELS, _fetch
        await interaction.response.defer(thinking=True)
        async with aiohttp.ClientSession() as session:
            project = await _fetch(session, f"{API_BASE}/project/{name}")
            if not project:
                await interaction.followup.send(f"No mod called `{name}` found. Pick one from the suggestions.")
                return
            versions = await _fetch(session, f"{API_BASE}/project/{project['id']}/version") or []
        versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        url = f"https://modrinth.com/{project.get('project_type', 'mod')}/{project.get('slug')}"
        embed = discord.Embed(
            title=project.get("title", name),
            url=url,
            description=(project.get("description") or "")[:500] or None,
            color=0x1BD96A,
        )
        if project.get("icon_url"):
            embed.set_thumbnail(url=project["icon_url"])
        embed.add_field(name="Downloads", value=f"{project.get('downloads', 0):,}", inline=True)
        embed.add_field(name="Followers", value=f"{project.get('followers', 0):,}", inline=True)
        if versions:
            latest = versions[0]
            embed.add_field(name="Latest version",
                            value=f"[{latest.get('version_number')}]({url}/version/{latest.get('id')}) "
                                  f"<t:{int(datetime.fromisoformat(latest['date_published'].replace('Z', '+00:00')).timestamp())}:R>",
                            inline=True)
        support = loader_support(versions)
        lines = [
            f"{LOADER_EMOJI.get(l, '')} **{LOADER_LABELS.get(l, l.title())}:** {compress_versions(list(v))}"
            for l, v in support.items() if v
        ]
        if lines:
            embed.add_field(name="Supports", value="\n".join(lines)[:1024], inline=False)
        links = [f"[Modrinth]({url})"]
        for key, label in (("source_url", "Source"), ("issues_url", "Issues"), ("wiki_url", "Wiki"), ("discord_url", "Discord")):
            if project.get(key):
                links.append(f"[{label}]({project[key]})")
        embed.add_field(name="Links", value=" · ".join(links), inline=False)
        if project.get("categories"):
            embed.set_footer(text="Categories: " + ", ".join(project["categories"][:6]))
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="changelog", description="Show the changelogs of a mod's latest versions.")
    @app_commands.describe(name="Start typing to pick a mod", count="How many versions (1-10)")
    @app_commands.autocomplete(name=_mod_autocomplete)
    async def changelog(self, interaction: discord.Interaction, name: str, count: app_commands.Range[int, 1, 10] = 5):
        from cogs.modrinth import API_BASE, _fetch
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            project = await _fetch(session, f"{API_BASE}/project/{name}")
            if not project:
                await interaction.followup.send(f"No mod called `{name}` found.", ephemeral=True)
                return
            versions = await _fetch(session, f"{API_BASE}/project/{project['id']}/version") or []
        versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        if not versions:
            await interaction.followup.send("No versions published yet.", ephemeral=True)
            return
        url = f"https://modrinth.com/{project.get('project_type', 'mod')}/{project.get('slug')}"
        embed = discord.Embed(title=f"Changelog - {project.get('title')}", url=f"{url}/changelog", color=0x1BD96A)
        budget = 5500
        for v in versions[:count]:
            text = (v.get("changelog") or "*no changelog*").strip()
            per_field = min(1000, max(100, budget // max(1, count)))
            if len(text) > per_field:
                text = text[: per_field - 1].rsplit("\n", 1)[0] + "\n…"
            date = (v.get("date_published") or "")[:10]
            name_line = f"{v.get('version_number')} ({date})"
            embed.add_field(name=name_line[:256], value=text[:1024], inline=False)
            budget -= len(text) + len(name_line)
            if budget < 200:
                break
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="compatibility-refresh", description="Redraw the compatibility table now (administrator).")
    @app_commands.default_permissions(administrator=True)
    async def compatibility_refresh(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self.refresh_compat()
        await interaction.followup.send(
            f"Compatibility table updated in {updated} server(s)." if updated else
            f"Nothing updated - is there a #{CHANNELS.get('compatibility')} channel (run `/update`) and is Modrinth reachable?",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(ModInfo(bot))
