"""CurseForge: downloads and new versions of the mods (needs a free API key).

    /curseforge key      set or remove the CurseForge API key (administrator, modal)
    /curseforge status   which mods were found on CurseForge, downloads, last check
    /curseforge sync     check CurseForge now

The bot finds the mods by itself: for every Modrinth project it searches CurseForge
for the same slug / name by the same author (SERVER_TEMPLATE["curseforge"]["author"],
default: the Modrinth username). Once one is found it remembers the author id and
from then on lists all mods of that author - so new mods, and mods that are only on
CurseForge, show up automatically.

What it's used for:
- download statistics and milestones count Modrinth + CurseForge (cogs/mod_stats.py,
  cogs/mod_info.py) and /mod shows both numbers and a CurseForge link
- new files on CurseForge: after release_grace_minutes the bot checks whether the same
  version is on Modrinth (then the Modrinth announcement already covered it); if not,
  it's announced in the mod releases channel

Key: data/settings.json (never in backups). State: data/curseforge.json.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, get_setting, is_admin, load_json, save_json, set_setting
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.curseforge")

API = "https://api.curseforge.com/v1"
MINECRAFT = 432
KEY_SETTING = "curseforge_api_key"
STATE_FILE = DATA_DIR / "curseforge.json"
USER_AGENT = "ErikEdits-Discord-Bot (github.com/ErikEdits/discord-bot)"
RELEASE_TYPES = {1: "Release", 2: "Beta", 3: "Alpha"}
LOADERS = {"fabric", "forge", "neoforge", "quilt", "liteloader", "rift"}
MAX_ANNOUNCE_PER_CHECK = 5
# Upload tokens from curseforge.com -> Settings -> My API Tokens look like a UUID. They
# can upload files but can't read data; the bot needs a key from console.curseforge.com.
UPLOAD_TOKEN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
WRONG_KEY_TEXT = (
    "That looks like an **upload token** (curseforge.com → Settings → My API Tokens) - nothing saved. "
    "It can only upload files, so keep it private.\n"
    "The bot needs an **API key** from **console.curseforge.com**: sign in there, create an organization if "
    "asked, open **API Keys** and copy the key. It starts with `$2a$10$`."
)


def _config() -> dict:
    return SERVER_TEMPLATE.get("curseforge", {})


def _author() -> str:
    return (_config().get("author") or SERVER_TEMPLATE.get("modrinth", {}).get("username") or "").lower()


def load_state() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("author_id", None)
    data.setdefault("mods", {})          # cf id -> {name, slug, url, logo, modrinth_id, downloads, seen_files}
    data.setdefault("pending", [])       # new files waiting for the Modrinth check
    data.setdefault("last_sync", None)
    data.setdefault("last_discovery", None)
    data.setdefault("last_error", None)
    return data


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# -------- used by mod_stats / mod_info ---------------------------------------

def cf_for_modrinth(modrinth_id: str | None) -> dict | None:
    """The CurseForge entry of a Modrinth project (downloads, url), or None."""
    if not modrinth_id:
        return None
    for cf_id, mod in load_state()["mods"].items():
        if mod.get("modrinth_id") == modrinth_id:
            return dict(mod, id=cf_id)
    return None


def extend_projects(projects: list[dict]) -> list[dict]:
    """Modrinth projects + "cf_downloads"/"cf_url", plus pseudo projects for mods only on CurseForge."""
    mods = load_state()["mods"]
    by_mr = {m.get("modrinth_id"): m for m in mods.values() if m.get("modrinth_id")}
    out = []
    for p in projects:
        p = dict(p)
        cf = by_mr.get(p.get("id"))
        if cf:
            p["cf_downloads"], p["cf_url"] = int(cf.get("downloads", 0)), cf.get("url")
        out.append(p)
    for cf_id, m in mods.items():
        if not m.get("modrinth_id"):
            out.append({"id": f"cf:{cf_id}", "title": m.get("name"), "slug": m.get("slug"), "downloads": 0,
                        "followers": 0, "cf_downloads": int(m.get("downloads", 0)), "cf_url": m.get("url"),
                        "icon_url": m.get("logo"), "cf_only": True})
    return out


def total_downloads(project: dict) -> int:
    return int(project.get("downloads", 0)) + int(project.get("cf_downloads", 0))


def same_release(cf_file: dict, modrinth_versions: list[dict]) -> bool:
    """Is this CurseForge file also on Modrinth (same version number or file name)?"""
    names = f"{cf_file.get('displayName', '')} {cf_file.get('fileName', '')}".lower()
    file_name = (cf_file.get("fileName") or "").lower()
    for v in modrinth_versions or []:
        number = (v.get("version_number") or "").lower()
        if number and re.search(rf"(?<![\d.]){re.escape(number)}(?!\d|\.\d)", names):
            return True
        if file_name and any((f.get("filename") or "").lower() == file_name for f in v.get("files") or []):
            return True
    return False


def file_url(mod: dict, file: dict) -> str:
    return f"{mod.get('url', '').rstrip('/')}/files/{file.get('id')}"


def release_embed(mod: dict, file: dict) -> discord.Embed:
    versions = [g for g in file.get("gameVersions") or [] if re.match(r"^\d+\.\d+", g)]
    loaders = [g for g in file.get("gameVersions") or [] if g.lower() in LOADERS]
    kind = RELEASE_TYPES.get(file.get("releaseType"), "Release")
    embed = discord.Embed(
        title=f"{mod.get('name')} {file.get('displayName') or file.get('fileName')}"[:256],
        url=file_url(mod, file),
        description=f"A new **{kind.lower()}** is out on **CurseForge**!",
        color=0xF16436,
        timestamp=datetime.now(timezone.utc),
    )
    if versions:
        embed.add_field(name="Minecraft", value=", ".join(versions[:12]), inline=True)
    if loaders:
        embed.add_field(name="Loader", value=", ".join(loaders), inline=True)
    embed.add_field(name="Download", value=f"[CurseForge]({file_url(mod, file)})", inline=False)
    if mod.get("logo"):
        embed.set_thumbnail(url=mod["logo"])
    embed.set_footer(text="CurseForge")
    return embed


class KeyModal(discord.ui.Modal, title="CurseForge API key"):
    key = discord.ui.TextInput(label="API key from console.curseforge.com", required=False, max_length=200,
                               placeholder="$2a$10$... (empty = remove)")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        value = self.key.value.strip()
        if not value:
            set_setting(KEY_SETTING, None)
            await interaction.response.send_message("CurseForge key removed.", ephemeral=True)
            return
        if UPLOAD_TOKEN_RE.match(value):
            await interaction.response.send_message(WRONG_KEY_TEXT, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        status, _ = await self.cog.request("GET", f"/games/{MINECRAFT}", key=value)
        if status in (401, 403):
            hint = "" if value.startswith("$2a$") else "\n\n" + WRONG_KEY_TEXT
            await interaction.followup.send("CurseForge says this key is invalid - nothing saved." + hint,
                                            ephemeral=True)
            return
        set_setting(KEY_SETTING, value)
        log.info("CurseForge key set by %s", interaction.user)
        result = await self.cog.sync(force_discovery=True)
        note = "" if status == 200 else f"\n(Couldn't check the key right now: HTTP {status})"
        await interaction.followup.send(f"✅ Key saved. {result}{note}", ephemeral=True)


class CurseForge(commands.Cog):
    group = app_commands.Group(name="curseforge", description="CurseForge downloads and releases (administrator).",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.sync_loop.change_interval(minutes=max(10, int(_config().get("poll_minutes", 30))))
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()

    # -------- HTTP ------------------------------------------------------------

    async def request(self, method: str, path: str, *, params=None, json_body=None, key: str | None = None):
        """(status, json). Network trouble and CurseForge errors only log a warning."""
        key = key or get_setting(KEY_SETTING)
        if not key:
            return 0, None
        headers = {"x-api-key": key, "Accept": "application/json", "User-Agent": USER_AGENT}
        for attempt in (1, 2):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.request(method, API + path, params=params, json=json_body, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 200:
                            return 200, await r.json(content_type=None)
                        if r.status != 429 and r.status < 500:
                            return r.status, None
                        problem = f"HTTP {r.status}"
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                problem = type(exc).__name__
            if attempt == 1:
                await asyncio.sleep(2)
        log.warning("CurseForge not reachable right now (%s): %s", problem, path)
        return -1, None

    async def search(self, **params) -> list[dict]:
        out, index = [], 0
        while index < 500:
            status, data = await self.request("GET", "/mods/search",
                                              params={"gameId": MINECRAFT, "pageSize": 50, "index": index, **params})
            if status != 200 or not data:
                break
            page = data.get("data") or []
            out += page
            total = (data.get("pagination") or {}).get("totalCount", 0)
            index += len(page)
            if not page or index >= total:
                break
        return out

    # -------- sync ---------------------------------------------------------------

    def _by_author(self, mod: dict, author_id: int | None) -> bool:
        authors = mod.get("authors") or []
        if author_id is not None and any(a.get("id") == author_id for a in authors):
            return True
        return any((a.get("name") or "").lower() == _author() for a in authors)

    async def discover(self, state: dict, projects: list[dict]) -> None:
        """Find the author's mods on CurseForge and link them to the Modrinth projects."""
        found: dict[int, dict] = {}
        if state["author_id"]:
            for m in await self.search(authorId=state["author_id"]):
                if self._by_author(m, state["author_id"]):
                    found[m["id"]] = m
        if not found:
            for p in projects:
                hits = await self.search(slug=p.get("slug")) if p.get("slug") else []
                hits = [h for h in hits if self._by_author(h, state["author_id"])]
                if not hits:
                    hits = [h for h in await self.search(searchFilter=p.get("title", ""), classId=6)
                            if _norm(h.get("name")) == _norm(p.get("title")) and self._by_author(h, state["author_id"])]
                for h in hits[:1]:
                    found[h["id"]] = h
                    if not state["author_id"]:
                        author = next((a for a in h.get("authors") or [] if (a.get("name") or "").lower() == _author()),
                                      None)
                        state["author_id"] = author.get("id") if author else None
            if state["author_id"]:  # now that we know the author, also pick up CurseForge-only mods
                for m in await self.search(authorId=state["author_id"]):
                    if self._by_author(m, state["author_id"]):
                        found.setdefault(m["id"], m)
        by_slug = {_norm(p.get("slug")): p for p in projects}
        by_name = {_norm(p.get("title")): p for p in projects}
        for cf_id, m in found.items():
            match = by_slug.get(_norm(m.get("slug"))) or by_name.get(_norm(m.get("name")))
            entry = state["mods"].setdefault(str(cf_id), {})
            new = "seen_files" not in entry
            self._update_entry(entry, m)
            entry["modrinth_id"] = match.get("id") if match else None
            if new:
                entry["seen_files"] = [f["id"] for f in m.get("latestFiles") or []]  # no announcements for old files
                log.info("CurseForge: found %s%s", m.get("name"), f" (= Modrinth {match.get('title')})" if match else "")
        state["last_discovery"] = time.time()

    @staticmethod
    def _update_entry(entry: dict, m: dict) -> None:
        entry.update({
            "name": m.get("name"), "slug": m.get("slug"),
            "url": (m.get("links") or {}).get("websiteUrl") or f"https://www.curseforge.com/minecraft/mc-mods/{m.get('slug')}",
            "logo": (m.get("logo") or {}).get("thumbnailUrl"),
            "downloads": int(m.get("downloadCount") or 0),
        })

    async def refresh(self, state: dict) -> list[tuple[dict, dict]]:
        """Update downloads and collect files we haven't seen yet: [(mod entry, file)]."""
        ids = [int(i) for i in state["mods"]]
        if not ids:
            return []
        status, data = await self.request("POST", "/mods", json_body={"modIds": ids, "filterPcOnly": True})
        if status != 200 or not data:
            return []
        new_files = []
        for m in data.get("data") or []:
            entry = state["mods"].get(str(m.get("id")))
            if entry is None:
                continue
            self._update_entry(entry, m)
            seen = set(entry.get("seen_files") or [])
            for f in m.get("latestFiles") or []:
                if f.get("id") not in seen:
                    seen.add(f["id"])
                    new_files.append((dict(entry, id=str(m["id"])), f))
            entry["seen_files"] = sorted(seen)[-200:]
        return new_files

    async def _modrinth_versions(self, project_id: str) -> list[dict]:
        from cogs.modrinth import API_BASE, _fetch
        async with aiohttp.ClientSession() as session:
            return await _fetch(session, f"{API_BASE}/project/{project_id}/version") or []

    async def sync(self, force_discovery: bool = False) -> str:
        if not get_setting(KEY_SETTING):
            return "No CurseForge key set (`/curseforge key`)."
        async with self.lock:
            state = load_state()
            username = SERVER_TEMPLATE.get("modrinth", {}).get("username", "")
            from cogs.modrinth import _fetch_user_projects
            projects = await _fetch_user_projects(username) if username else []
            hours = float(_config().get("discover_hours", 24))
            if force_discovery or not state["mods"] or time.time() - (state["last_discovery"] or 0) > hours * 3600:
                await self.discover(state, projects or [])
            new_files = await self.refresh(state)
            grace = float(_config().get("release_grace_minutes", 45)) * 60
            now = time.time()
            for entry, f in new_files:
                state["pending"].append({"mod": entry["id"], "file": f, "seen": now})
            due = [p for p in state["pending"] if now - p["seen"] >= grace]
            state["pending"] = [p for p in state["pending"] if now - p["seen"] < grace]
            state["last_sync"] = now
            save_json(STATE_FILE, state)
        announced = await self._announce_due(due, state)
        total = sum(int(m.get("downloads", 0)) for m in state["mods"].values())
        return (f"{len(state['mods'])} mod(s) on CurseForge, {total:,} downloads"
                + (f", {announced} new version(s) announced" if announced else "") + ".")

    async def _announce_due(self, due: list[dict], state: dict) -> int:
        to_post = []
        newest_per_mod: dict[str, dict] = {}
        for p in due:  # several files of one upload (one per loader) -> only the newest
            current = newest_per_mod.get(p["mod"])
            if current is None or p["file"].get("id", 0) > current["file"].get("id", 0):
                newest_per_mod[p["mod"]] = p
        for p in newest_per_mod.values():
            mod = state["mods"].get(p["mod"])
            if mod is None:
                continue
            if mod.get("modrinth_id"):
                versions = await self._modrinth_versions(mod["modrinth_id"])
                if same_release(p["file"], versions):
                    log.info("CurseForge file %s of %s is also on Modrinth - already announced there",
                             p["file"].get("displayName"), mod.get("name"))
                    continue
            to_post.append((mod, p["file"]))
        if not to_post:
            return 0
        cfg = SERVER_TEMPLATE.get("modrinth", {})
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=CHANNELS.get("mod_releases", ""))
            if channel is None:
                continue
            role = discord.utils.get(guild.roles, name=cfg.get("mention_role") or "")
            for mod, f in to_post[:MAX_ANNOUNCE_PER_CHECK]:
                try:
                    await channel.send(content=role.mention if role else None, embed=release_embed(mod, f),
                                       allowed_mentions=discord.AllowedMentions(roles=True))
                    log.info("Posted CurseForge release: %s %s", mod.get("name"), f.get("displayName"))
                except discord.HTTPException:
                    log.warning("Cannot post the CurseForge release in #%s", channel.name)
        return len(to_post[:MAX_ANNOUNCE_PER_CHECK])

    @tasks.loop(minutes=30)
    async def sync_loop(self):
        if not _config().get("enabled", True) or not get_setting(KEY_SETTING):
            return
        try:
            await self.sync()
        except Exception:
            log.exception("CurseForge sync failed")

    @sync_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # -------- commands -------------------------------------------------------------

    @group.command(name="key", description="Set or remove the CurseForge API key (free at console.curseforge.com).")
    async def key(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.send_modal(KeyModal(self))

    @group.command(name="status", description="Mods found on CurseForge, downloads, last check.")
    async def status(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        state = load_state()
        embed = discord.Embed(title="CurseForge", color=0xF16436)
        if not get_setting(KEY_SETTING):
            embed.description = ("No API key yet. Get one for free at **console.curseforge.com** (API keys) and "
                                 "enter it with `/curseforge key`.")
        lines = []
        for m in sorted(state["mods"].values(), key=lambda m: -int(m.get("downloads", 0))):
            link = "linked to Modrinth" if m.get("modrinth_id") else "only on CurseForge"
            lines.append(f"[{m.get('name')}]({m.get('url')}) – ⬇️ {int(m.get('downloads', 0)):,} · {link}")
        embed.add_field(name=f"Mods ({len(lines)})", value="\n".join(lines)[:1024] or "none found yet", inline=False)
        embed.add_field(name="Last check", value=f"<t:{int(state['last_sync'])}:R>" if state["last_sync"] else "never")
        embed.add_field(name="Author", value=f"{_author() or '?'}" + (f" (id {state['author_id']})"
                                                                         if state["author_id"] else ""))
        if state["pending"]:
            embed.add_field(name="New files waiting", value=", ".join(
                f"{state['mods'].get(p['mod'], {}).get('name')} {p['file'].get('displayName')}"
                for p in state["pending"])[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="sync", description="Check CurseForge now (also looks for new mods).")
    async def sync_command(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.sync(force_discovery=True), ephemeral=True)


async def setup(bot):
    await bot.add_cog(CurseForge(bot))
