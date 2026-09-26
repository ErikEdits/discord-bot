"""Modrinth release-watcher.

Polls the configured Modrinth user's projects on an interval and posts
new versions to a dedicated channel. State (last seen version per project)
is persisted to data/modrinth.json so restarts don't re-announce, and releases
published while the bot was offline are announced after the next start.

Config in SERVER_TEMPLATE['modrinth']:
    enabled         bool
    username        str  - Modrinth username to watch (e.g. "ErikEdits")
    channel         str  - target channel name (uses CHANNELS['mod_releases'])
    poll_minutes    int  - poll interval, default 15
    mention_role    str|None - optional role name to ping per release
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.modrinth")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "modrinth.json"

API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "ErikEditsBot/1.0 (Discord setup bot; +github.com/ErikEdits)"

LOADER_EMOJI = {
    "fabric": "\U0001F9F5",     # thread
    "neoforge": "\U0001F525",   # fire
    "forge": "\U0001F528",      # hammer
    "quilt": "\U0001F9F6",      # knot
    "paper": "\U0001F4DC",      # scroll
    "spigot": "\U0001F7E1",     # yellow circle
    "minecraft": "\U0001F9F1",  # brick (resource pack / default)
}

# Max. releases per project announced after downtime (oldest first).
MAX_CATCH_UP_PER_PROJECT = 5

VERSION_TYPE_COLOR = {
    "release": 0x2ECC71,
    "beta":    0xF1C40F,
    "alpha":   0xE67E22,
}


def _config() -> dict:
    return SERVER_TEMPLATE.get("modrinth", {})


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"projects": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("projects", {})
        return data
    except Exception:
        log.exception("Failed to read modrinth state")
        return {"projects": {}}


def _save_state(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _fetch(session: aiohttp.ClientSession, url: str, attempts: int = 2) -> Any | None:
    """GET a Modrinth API URL. Timeouts, connection errors and Modrinth-side errors
    (429/5xx) are retried once and then only logged as a warning: Modrinth being slow
    or down for a moment isn't a bot error, so it shouldn't trigger error-alert DMs.
    The callers simply try again on their next run."""
    for attempt in range(1, attempts + 1):
        try:
            async with session.get(url, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
                problem = f"HTTP {r.status}"
                if r.status != 429 and r.status < 500:
                    log.warning("Modrinth GET %s -> %s", url, problem)
                    return None
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            problem = type(exc).__name__ if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
        except Exception:
            log.exception("Modrinth fetch failed: %s", url)
            return None
        if attempt < attempts:
            await asyncio.sleep(2)
    log.warning("Modrinth not reachable right now (%s): %s - will retry on the next run", problem, url)
    return None


def _format_version_embed(project: dict, version: dict) -> discord.Embed:
    color = VERSION_TYPE_COLOR.get(version.get("version_type", "release"), 0x5865F2)
    loaders = version.get("loaders") or project.get("loaders") or []
    loader_str = " ".join(f"{LOADER_EMOJI.get(l, '')} {l}".strip() for l in loaders) or "—"
    game_versions = version.get("game_versions") or []
    if len(game_versions) > 8:
        game_str = f"{', '.join(game_versions[:4])} … {', '.join(game_versions[-2:])}"
    else:
        game_str = ", ".join(game_versions) or "—"

    files = version.get("files") or []
    download_url = files[0]["url"] if files else None

    title = f"{project.get('title', 'Unknown')} {version.get('version_number', '')}"
    page_url = f"https://modrinth.com/{project.get('project_type', 'mod')}/{project.get('slug')}/version/{version.get('id')}"

    embed = discord.Embed(
        title=title,
        url=page_url,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    icon = project.get("icon_url")
    if icon:
        embed.set_thumbnail(url=icon)
    embed.add_field(name="Type", value=str(version.get("version_type", "release")).title(), inline=True)
    embed.add_field(name="Loaders", value=loader_str, inline=True)
    embed.add_field(name="Game versions", value=game_str, inline=False)
    changelog = (version.get("changelog") or "").strip()
    if changelog:
        embed.add_field(name="Changelog", value=changelog[:900] + ("…" if len(changelog) > 900 else ""), inline=False)
    if download_url:
        size_kb = files[0].get("size", 0) // 1024
        embed.add_field(name="Download", value=f"[{files[0]['filename']}]({download_url}) ({size_kb} KB)", inline=False)
    embed.set_footer(text=f"Modrinth · {project.get('downloads', 0)} total downloads")
    return embed


async def _fetch_user_projects(username: str) -> list | None:
    async with aiohttp.ClientSession() as session:
        return await _fetch(session, f"{API_BASE}/user/{username}/projects")


class ModDownloadSelect(discord.ui.Select):
    """Dropdown of the user's Modrinth mods. Selecting one returns a download link."""

    def __init__(self, mods: list[dict] | None = None):
        if mods:
            options = []
            for mod in mods[:25]:
                project_type = mod.get("project_type", "mod")
                emoji = "\U0001F4E6"  # package
                if project_type == "resourcepack":
                    emoji = "\U0001F3A8"
                elif project_type == "shader":
                    emoji = "\U00002728"
                elif project_type == "modpack":
                    emoji = "\U0001F4DA"
                desc = (mod.get("description") or "").strip()[:100]
                options.append(discord.SelectOption(
                    label=str(mod.get("title", "Unknown"))[:100],
                    value=str(mod.get("slug", "")) [:100],
                    description=desc or None,
                    emoji=emoji,
                ))
        else:
            options = [discord.SelectOption(label="No mods configured", value="__none__")]

        super().__init__(
            placeholder="Pick a mod to download...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="modrinth:download_select",
        )

    async def callback(self, interaction: discord.Interaction):
        from cogs.maintenance import is_under_maintenance, get_maintenance_message
        if is_under_maintenance("moddownload"):
            await interaction.response.send_message(
                f"\U0001F527 {get_maintenance_message('moddownload')}",
                ephemeral=True,
            )
            return
        slug = self.values[0]
        if slug == "__none__" or not slug:
            await interaction.response.send_message("No mods available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            project = await _fetch(session, f"{API_BASE}/project/{slug}")
            if not project:
                await interaction.followup.send(
                    f"Couldn't load `{slug}` from Modrinth. It may have been removed.",
                    ephemeral=True,
                )
                return
            versions = await _fetch(session, f"{API_BASE}/project/{project['id']}/version")

        if not versions:
            await interaction.followup.send(
                f"No versions are published for **{project.get('title')}** yet.",
                ephemeral=True,
            )
            return

        versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        latest = versions[0]
        files = latest.get("files") or []
        download_url = files[0]["url"] if files else None
        page_url = f"https://modrinth.com/{project.get('project_type', 'mod')}/{project.get('slug')}"

        color = VERSION_TYPE_COLOR.get(latest.get("version_type", "release"), 0x5865F2)
        loaders = latest.get("loaders") or project.get("loaders") or []
        loader_str = " ".join(f"{LOADER_EMOJI.get(l, '')} {l}".strip() for l in loaders) or "—"
        game_versions = latest.get("game_versions") or []
        if len(game_versions) > 6:
            game_str = f"{', '.join(game_versions[:3])} … {', '.join(game_versions[-2:])}"
        else:
            game_str = ", ".join(game_versions) or "—"

        embed = discord.Embed(
            title=f"{project.get('title')} - {latest.get('version_number')}",
            url=page_url,
            color=color,
            description=(project.get("description") or "")[:300] or None,
            timestamp=datetime.now(timezone.utc),
        )
        icon = project.get("icon_url")
        if icon:
            embed.set_thumbnail(url=icon)
        embed.add_field(name="Type", value=str(latest.get("version_type", "release")).title(), inline=True)
        embed.add_field(name="Loaders", value=loader_str, inline=True)
        embed.add_field(name="Game versions", value=game_str, inline=False)
        if download_url:
            size_kb = files[0].get("size", 0) // 1024
            embed.add_field(
                name="Download",
                value=f"[{files[0]['filename']}]({download_url}) ({size_kb} KB)",
                inline=False,
            )
        embed.add_field(name="More on Modrinth", value=f"[Project page]({page_url})", inline=False)
        embed.set_footer(text=f"Total downloads: {project.get('downloads', 0)}")

        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Failed to send download embed")


# ---------------------------------------------------------------------------
# "Filter by Minecraft version" -> loader -> matching file for every mod
# ---------------------------------------------------------------------------

_RELEASE_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
LOADER_LABELS = {
    "fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt",
    "minecraft": "Vanilla / Resource pack", "datapack": "Data pack", "iris": "Iris",
    "optifine": "OptiFine", "paper": "Paper", "spigot": "Spigot", "bukkit": "Bukkit",
}
_FILTER_CACHE: dict[tuple[str, str, str], tuple[float, dict | None]] = {}
_FILTER_CACHE_SECONDS = 600


def _filter_config() -> dict:
    return SERVER_TEMPLATE.get("download_filter", {})


def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def collect_game_versions(mods: list[dict]) -> list[str]:
    """All Minecraft versions supported by any of the mods, newest first."""
    include_snapshots = _filter_config().get("include_snapshots", False)
    versions = set()
    for mod in mods or []:
        for v in mod.get("game_versions") or mod.get("versions") or []:
            if include_snapshots or _RELEASE_VERSION.match(v):
                versions.add(v)
    ordered = sorted(versions, key=_version_key, reverse=True)
    return ordered[: int(_filter_config().get("max_versions", 25))]


def _mod_supports(mod: dict, game_version: str, loader: str | None = None) -> bool:
    if game_version not in (mod.get("game_versions") or mod.get("versions") or []):
        return False
    return loader is None or loader in (mod.get("loaders") or [])


async def _latest_matching_version(session: aiohttp.ClientSession, project_id: str,
                                   game_version: str, loader: str) -> dict | None:
    key = (project_id, game_version, loader)
    cached = _FILTER_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _FILTER_CACHE_SECONDS:
        return cached[1]
    url = (f"{API_BASE}/project/{project_id}/version"
           f"?loaders={quote(json.dumps([loader]))}&game_versions={quote(json.dumps([game_version]))}")
    versions = await _fetch(session, url) or []
    versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
    best = versions[0] if versions else None
    _FILTER_CACHE[key] = (time.monotonic(), best)
    return best


async def build_filtered_downloads_embed(mods: list[dict], game_version: str, loader: str) -> discord.Embed:
    loader_name = LOADER_LABELS.get(loader, loader.title())
    embed = discord.Embed(
        title=f"Downloads for Minecraft {game_version} \u00b7 {loader_name}",
        color=0x1BD96A,
        timestamp=datetime.now(timezone.utc),
    )
    missing = []
    found = 0
    async with aiohttp.ClientSession() as session:
        for mod in mods:
            if not _mod_supports(mod, game_version, loader):
                missing.append(mod.get("title", "?"))
                continue
            version = await _latest_matching_version(session, mod.get("id") or mod.get("project_id"), game_version, loader)
            if not version:
                missing.append(mod.get("title", "?"))
                continue
            files = version.get("files") or []
            primary = next((f for f in files if f.get("primary")), files[0] if files else None)
            if primary is None:
                missing.append(mod.get("title", "?"))
                continue
            page = f"https://modrinth.com/{mod.get('project_type', 'mod')}/{mod.get('slug')}/version/{version.get('id')}"
            size_kb = primary.get("size", 0) // 1024
            embed.add_field(
                name=f"{mod.get('title', 'Unknown')} {version.get('version_number', '')}"[:256],
                value=f"\u2B07\uFE0F [{primary['filename']}]({primary['url']}) ({size_kb} KB) \u00b7 [page]({page})"[:1024],
                inline=False,
            )
            found += 1
            if found >= 20:
                break
    if missing:
        embed.add_field(
            name=f"Not available for {game_version} {loader_name}",
            value=", ".join(missing)[:1024],
            inline=False,
        )
    if not found:
        embed.description = "None of the mods has a file for this combination yet."
    embed.set_footer(text="Newest matching version per mod \u00b7 from Modrinth")
    return embed


class LoaderButton(discord.ui.Button):
    def __init__(self, loader: str, game_version: str, mods: list[dict]):
        emoji = LOADER_EMOJI.get(loader)
        super().__init__(label=LOADER_LABELS.get(loader, loader.title()), emoji=emoji,
                         style=discord.ButtonStyle.primary)
        self.loader = loader
        self.game_version = game_version
        self.mods = mods

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await build_filtered_downloads_embed(self.mods, self.game_version, self.loader)
        await interaction.followup.send(embed=embed, ephemeral=True)


class LoaderPickView(discord.ui.View):
    def __init__(self, game_version: str, mods: list[dict], loaders: list[str]):
        super().__init__(timeout=300)
        for loader in loaders[:25]:
            self.add_item(LoaderButton(loader, game_version, mods))


class VersionFilterSelect(discord.ui.Select):
    """Persistent menu on the download panel: pick a Minecraft version first."""

    def __init__(self, mods: list[dict] | None = None):
        versions = collect_game_versions(mods or [])
        options = [discord.SelectOption(label=f"Minecraft {v}", value=v) for v in versions]
        if not options:
            options = [discord.SelectOption(label="No versions found", value="__none__")]
        super().__init__(
            placeholder="...or filter by Minecraft version",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="modrinth:version_filter",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        from cogs.maintenance import is_under_maintenance, get_maintenance_message
        if is_under_maintenance("moddownload"):
            await interaction.response.send_message(f"\U0001F527 {get_maintenance_message('moddownload')}", ephemeral=True)
            return
        game_version = self.values[0]
        if game_version == "__none__":
            await interaction.response.send_message("No versions available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        mods = await _fetch_user_projects(_config().get("username", "")) or []
        matching = [m for m in mods if _mod_supports(m, game_version)]
        loaders = sorted({l for m in matching for l in (m.get("loaders") or [])},
                         key=lambda l: list(LOADER_LABELS).index(l) if l in LOADER_LABELS else 99)
        if not loaders:
            await interaction.followup.send(f"No mod supports Minecraft {game_version} yet.", ephemeral=True)
            return
        if len(loaders) == 1:
            embed = await build_filtered_downloads_embed(mods, game_version, loaders[0])
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await interaction.followup.send(
            f"**Minecraft {game_version}** - which mod loader do you use?",
            view=LoaderPickView(game_version, mods, loaders),
            ephemeral=True,
        )


class ModDownloadView(discord.ui.View):
    def __init__(self, mods: list[dict] | None = None):
        super().__init__(timeout=None)
        self.add_item(ModDownloadSelect(mods))
        if _filter_config().get("enabled", True):
            self.add_item(VersionFilterSelect(mods))


class Modrinth(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._proj_cache: list = []      # cached project list for autocomplete
        self._proj_cache_ts: float = 0.0
        cfg = _config()
        if cfg.get("enabled") and cfg.get("username"):
            interval = int(cfg.get("poll_minutes", 15))
            self.poll_loop.change_interval(minutes=max(5, interval))
            self.poll_loop.start()
        else:
            log.info("Modrinth integration disabled or missing username")

    def cog_unload(self):
        if self.poll_loop.is_running():
            self.poll_loop.cancel()

    @tasks.loop(minutes=15)
    async def poll_loop(self):
        cfg = _config()
        username = cfg.get("username")
        if not username:
            return
        channel_name = CHANNELS.get("mod_releases", "")
        if not channel_name:
            return

        state = _load_state()
        # With no saved state at all (fresh install) we only record the current versions,
        # so the channel isn't flooded with the whole release history. Once state exists,
        # every version published after the last one we saw is announced - including
        # releases that came out while the bot was offline.
        initialized = bool(state["projects"])
        async with aiohttp.ClientSession() as session:
            projects = await _fetch(session, f"{API_BASE}/user/{username}/projects")
            if not projects:
                return

            new_versions: list[tuple[dict, dict]] = []
            for project in projects:
                pid = project.get("id")
                if not pid:
                    continue
                versions = await _fetch(session, f"{API_BASE}/project/{pid}/version")
                if not versions:
                    continue
                versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
                latest = versions[0]
                seen = state["projects"].get(pid)

                if seen is None:
                    # A project we've never seen: announce only its newest version.
                    if initialized:
                        new_versions.append((project, latest))
                else:
                    last_id = seen.get("last_version_id")
                    last_published = seen.get("last_published") or ""
                    missed = [
                        v for v in versions
                        if v.get("id") != last_id and v.get("date_published", "") > last_published
                    ]
                    # Oldest first, and cap it so a long outage doesn't spam the channel.
                    for v in reversed(missed[:MAX_CATCH_UP_PER_PROJECT]):
                        new_versions.append((project, v))

                state["projects"][pid] = {
                    "last_version_id": latest.get("id"),
                    "last_version_number": latest.get("version_number"),
                    "last_published": latest.get("date_published"),
                }

            _save_state(state)

        if not initialized:
            log.info("Modrinth: initial state recorded for %d projects", len(projects))
            return

        if not new_versions:
            return

        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel is None:
                continue
            mention = ""
            role_name = cfg.get("mention_role")
            if role_name:
                role = discord.utils.get(guild.roles, name=role_name)
                if role:
                    mention = role.mention
            for project, version in new_versions:
                try:
                    embed = _format_version_embed(project, version)
                    await channel.send(
                        content=mention or None,
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                    log.info("Posted Modrinth release: %s %s", project.get("title"), version.get("version_number"))
                except discord.Forbidden:
                    log.warning("Cannot post in #%s (missing permission)", channel.name)
                except Exception:
                    log.exception("Failed to post Modrinth release")

        # Follow-ups: feedback poll a few days later + refresh the compatibility table.
        try:
            from cogs.mod_info import schedule_release_feedback
            for project, version in new_versions:
                schedule_release_feedback(project, version)
        except Exception:
            log.exception("Failed to schedule release follow-ups")

    @poll_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="modrinth-check", description="Force a Modrinth poll right now (admin only).")
    @app_commands.default_permissions(administrator=True)
    async def modrinth_check(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.poll_loop()
        await interaction.followup.send("Modrinth poll triggered. Check the releases channel.", ephemeral=True)

    async def _cached_projects(self) -> list:
        now = time.monotonic()
        if self._proj_cache and (now - self._proj_cache_ts) < 300:
            return self._proj_cache
        username = _config().get("username")
        if not username:
            return []
        projects = await _fetch_user_projects(username) or []
        self._proj_cache = projects
        self._proj_cache_ts = now
        return projects

    async def _project_autocomplete(self, interaction: discord.Interaction, current: str):
        projects = await self._cached_projects()
        cur = (current or "").lower()
        choices = []
        for p in projects:
            title = p.get("title", "")
            if cur in title.lower():
                choices.append(app_commands.Choice(name=title[:100], value=str(p.get("slug", ""))[:100]))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="modrinth-release", description="Post the latest release of ONE chosen Modrinth project.")
    @app_commands.describe(project="Start typing to pick one of your Modrinth projects")
    @app_commands.autocomplete(project=_project_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def modrinth_release(self, interaction: discord.Interaction, project: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            proj = await _fetch(session, f"{API_BASE}/project/{project}")
            if not proj:
                await interaction.followup.send(
                    f"Project `{project}` not found. Please pick one from the suggestions.",
                    ephemeral=True,
                )
                return
            versions = await _fetch(session, f"{API_BASE}/project/{proj['id']}/version")
        if not versions:
            await interaction.followup.send(
                f"No versions are published for **{proj.get('title')}** yet.", ephemeral=True
            )
            return
        versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        latest = versions[0]
        embed = _format_version_embed(proj, latest)
        channel = discord.utils.get(interaction.guild.text_channels, name=CHANNELS.get("mod_releases", ""))
        if channel is None:
            # No releases channel — just show it to the admin.
            await interaction.followup.send(
                content="Releases channel not found (run `/update`). Showing it here instead:",
                embed=embed, ephemeral=True,
            )
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send("I can't post in the releases channel.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Posted the latest **{proj.get('title')}** release ({latest.get('version_number')}) to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="modrinth-latest", description="Show the latest version of each of your Modrinth projects.")
    async def modrinth_latest(self, interaction: discord.Interaction):
        cfg = _config()
        username = cfg.get("username")
        if not username:
            await interaction.response.send_message("Modrinth username not configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with aiohttp.ClientSession() as session:
            projects = await _fetch(session, f"{API_BASE}/user/{username}/projects")
            if not projects:
                await interaction.followup.send("Could not fetch projects from Modrinth.", ephemeral=True)
                return
            embed = discord.Embed(
                title=f"Modrinth projects of {username}",
                color=0x1bd96a,
                url=f"https://modrinth.com/user/{username}",
            )
            for project in projects[:10]:
                pid = project.get("id")
                versions = await _fetch(session, f"{API_BASE}/project/{pid}/version") or []
                versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
                if versions:
                    v = versions[0]
                    embed.add_field(
                        name=f"{project.get('title')} - {v.get('version_number')}",
                        value=f"[{v.get('version_type', '').title()}] {len(v.get('game_versions', []))} MC versions · {project.get('downloads', 0)} dl",
                        inline=False,
                    )
                else:
                    embed.add_field(name=project.get("title"), value="(no versions)", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Modrinth(bot))
