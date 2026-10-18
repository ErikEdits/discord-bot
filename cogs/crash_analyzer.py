"""Crash-log analyzer.

A panel in CHANNELS["crash_analyzer"] has an "Analyze my crash" button. Clicking
it opens a private channel (like a ticket) in the CRASH REPORTS category. The
user uploads latest.log / a crash report / hs_err_pid*.log there (or pastes a
mclo.gs / pastebin / gist / pastes.dev link) and the bot replies with what went wrong
and how to fix it. Several files in one message are analyzed together.

The answer names the most likely culprit mod, and checks the server owner's own
Modrinth mods in the log (made for this Minecraft version/loader? newest version?).
If the same problem comes up for `frequent_threshold` different people within
`frequent_days` days, the owner (and `notify_users`) get a DM.

Files are only read in memory and never stored. Analyzer channels are deleted
when closed or after `auto_close_hours` without activity - no transcripts, so
nothing piles up on the bot host.

Needs the Message Content intent to see the uploaded files.
Config: SERVER_TEMPLATE["crash_analyzer"]. Pause with /maintenance crash.
"""

import asyncio
import gzip
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, feature_on, is_staff, load_json, save_json
from crashlog_analyzer import analyze
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.crash")

TOPIC_PREFIX = "crash user "
TEXT_EXTENSIONS = (".log", ".txt", ".gz", ".crash", ".out")
PASTE_SOURCES = [
    (re.compile(r"https?://mclo\.gs/(\w+)"), "mclo.gs/{}", "https://api.mclo.gs/1/raw/{}"),
    (re.compile(r"https?://pastebin\.com/(?:raw/)?(\w{8})\b"), "pastebin.com/{}", "https://pastebin.com/raw/{}"),
    (re.compile(r"https?://(?:api\.)?pastes\.dev/(\w+)"), "pastes.dev/{}", "https://api.pastes.dev/{}"),
    (re.compile(r"https?://gist\.github\.com/(?:[\w\-]+/)?([0-9a-f]{20,40})"), "gist {}", "https://api.github.com/gists/{}"),
]
CRASH_STATS_FILE = DATA_DIR / "crash_stats.json"
PROJECTS_CACHE_SECONDS = 1800
MAX_DECOMPRESSED = 30 * 1024 * 1024  # guard against gzip bombs
MAX_FILES_PER_MESSAGE = 3


def _config() -> dict:
    return SERVER_TEMPLATE.get("crash_analyzer", {})


def _category_name() -> str:
    return _config().get("category_name", "CRASH REPORTS")


def _support_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


def _opener_id(channel) -> int | None:
    topic = getattr(channel, "topic", None) or ""
    if topic.startswith(TOPIC_PREFIX):
        try:
            return int(topic[len(TOPIC_PREFIX):].split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _is_analyzer_channel(channel) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category is not None
        and channel.category.name == _category_name()
        and _opener_id(channel) is not None
    )


def build_panel_embed() -> discord.Embed:
    max_mb = _config().get("max_file_mb", 8)
    return discord.Embed(
        title="\U0001F50D Crash Analyzer",
        description=(
            "Your game crashed or won't start?\n\n"
            "**1.** Click the button below - a private channel opens for you.\n"
            "**2.** Upload your log file there:\n"
            " • `.minecraft/logs/latest.log`\n"
            " • or `.minecraft/crash-reports/crash-....txt`\n"
            " • or `hs_err_pid....log` from your game folder\n"
            f"**3.** The bot tells you what's wrong and how to fix it.\n\n"
            f"Files up to {max_mb} MB are read once and not stored. You can also paste a mclo.gs link."
        ),
        color=0x5865F2,
    )


class CrashPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Analyze my crash", emoji="\U0001F50D",
                       style=discord.ButtonStyle.primary, custom_id="crash:open")
    async def open_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_analyzer(interaction)


class CrashChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Still broken? Open a ticket", emoji="\U0001F3AB",
                       style=discord.ButtonStyle.secondary, custom_id="crash:ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.tickets import start_ticket
        await start_ticket(interaction, "bug")

    @discord.ui.button(label="Close", emoji="\U0001F512",
                       style=discord.ButtonStyle.danger, custom_id="crash:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not _is_analyzer_channel(channel):
            await interaction.response.send_message("This isn't a crash-analyzer channel.", ephemeral=True)
            return
        if interaction.user.id != _opener_id(channel) and not is_staff(interaction.user, _support_roles()):
            await interaction.response.send_message("Only the opener or staff can close this.", ephemeral=True)
            return
        await interaction.response.send_message("Closing this channel in 3 seconds...")
        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"Crash analyzer closed by {interaction.user}")
        except (discord.Forbidden, discord.NotFound):
            pass


async def _ensure_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    category = discord.utils.get(guild.categories, name=_category_name())
    if category is not None:
        return category
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                              read_message_history=True, embed_links=True),
    }
    for name in _support_roles():
        role = discord.utils.get(guild.roles, name=name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True)
    try:
        return await guild.create_category(name=_category_name(), overwrites=overwrites, reason="Crash analyzer")
    except discord.Forbidden:
        return None


async def _open_analyzer(interaction: discord.Interaction) -> None:
    from cogs.maintenance import get_maintenance_message, is_under_maintenance
    if is_under_maintenance("crash"):
        await interaction.response.send_message(f"\U0001F527 {get_maintenance_message('crash')}", ephemeral=True)
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    category = await _ensure_category(guild)
    if category is None:
        await interaction.response.send_message("I can't create the crash-report category. Check my permissions.",
                                                ephemeral=True)
        return
    existing = discord.utils.find(
        lambda c: c.category_id == category.id and _opener_id(c) == interaction.user.id,
        guild.text_channels,
    )
    if existing:
        await interaction.response.send_message(f"You already have an open analyzer channel: {existing.mention}",
                                                ephemeral=True)
        return

    safe_name = "".join(c for c in interaction.user.name.lower() if c.isalnum() or c == "-")[:20] or "user"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                      read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              manage_channels=True, embed_links=True, attach_files=True),
    }
    for name in _support_roles():
        role = discord.utils.get(guild.roles, name=name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True)
    try:
        channel = await guild.create_text_channel(
            name=f"crash-{safe_name}",
            category=category,
            overwrites=overwrites,
            topic=f"{TOPIC_PREFIX}{interaction.user.id}",
            reason=f"Crash analyzer opened by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to create the channel.", ephemeral=True)
        return

    max_mb = _config().get("max_file_mb", 8)
    embed = discord.Embed(
        title="Upload your log here",
        description=(
            f"Hi {interaction.user.mention}! Drag & drop one of these files into this channel:\n\n"
            "• `.minecraft/logs/latest.log` (best choice)\n"
            "• `.minecraft/crash-reports/crash-<date>.txt`\n"
            "• `hs_err_pid<number>.log` (if the game closed without a crash report)\n\n"
            f"Supported: `.log`, `.txt`, `.gz` up to {max_mb} MB - or paste a mclo.gs link.\n"
            f"This channel is deleted automatically after {_config().get('auto_close_hours', 6)}h without activity."
        ),
        color=0x5865F2,
    )
    try:
        await channel.send(embed=embed, view=CrashChannelView())
    except discord.Forbidden:
        pass
    await interaction.response.send_message(f"Your analyzer channel: {channel.mention}", ephemeral=True)
    log.info("Opened crash analyzer channel for %s", interaction.user)


def _decode(data: bytes, filename: str) -> str | None:
    if filename.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                data = gz.read(MAX_DECOMPRESSED + 1)
        except OSError:
            return None
        if len(data) > MAX_DECOMPRESSED:
            data = data[:MAX_DECOMPRESSED]
    if b"\x00" in data[:4096]:
        return None  # binary file
    return data.decode("utf-8", errors="replace")


def build_result_embed(source: str, result: dict, own_mods: list[str] | None = None,
                       own_title: str = "Mods from this server") -> discord.Embed:
    findings = result["findings"]
    info = result["info"]
    top = max((f.severity for f in findings), default=0)
    color = 0xE74C3C if top >= 3 else 0xE67E22 if top == 2 else 0x95A5A6
    embed = discord.Embed(title=f"\U0001F50D Crash analysis - {source}"[:256], color=color)

    setup_bits = []
    if info.get("minecraft"):
        setup_bits.append(f"Minecraft **{info['minecraft']}**")
    if info.get("loader"):
        setup_bits.append(f"{info['loader']} **{info.get('loader_version', '?')}**")
    if info.get("java"):
        setup_bits.append(f"Java **{info['java']}**")
    if info.get("mod_count"):
        setup_bits.append(f"**{info['mod_count']}** mods")
    setup_line = " · ".join(setup_bits) or "not detected"
    embed.description = f"**Detected:** {setup_line}\n**File type:** {info.get('kind', 'log')}"
    if info.get("description"):
        embed.description += f"\n**Crash report says:** {info['description'][:200]}"

    if findings:
        for f in findings[:5]:
            icon = "\U0001F534" if f.severity >= 3 else "\U0001F7E0" if f.severity == 2 else "\U0001F7E1"
            value = f"{f.detail}\n**Fix:** {f.fix}"
            embed.add_field(name=f"{icon} {f.title}"[:256], value=value[:1024], inline=False)
    elif result["looks_like_crash"]:
        embed.add_field(
            name="No known cause found",
            value="I couldn't match this error to a known problem. Check the error below, "
                  "or open a ticket so staff can take a look.",
            inline=False,
        )
    else:
        embed.add_field(
            name="No errors found",
            value="This log doesn't contain a crash. If the game crashed, upload the file from "
                  "`.minecraft/crash-reports/` or the `latest.log` right after the crash.",
            inline=False,
        )

    if result.get("root_error"):
        embed.add_field(name="Error", value=f"```{result['root_error'][:300]}```", inline=False)
    if result.get("suspects"):
        icons = {"very likely": "\U0001F534", "likely": "\U0001F7E0", "possible": "\U0001F7E1"}
        lines = [f"{icons.get(s['likelihood'], '')} **{s['mod']}** - {s['likelihood']} ({s['why']}) - "
                 f"[Modrinth](https://modrinth.com/mods?q={quote(s['mod'])})" for s in result["suspects"]]
        embed.add_field(name="Most likely culprit", value="\n".join(lines)[:1024], inline=False)
    elif result.get("stack_mods"):
        embed.add_field(
            name="Mods in the stack trace",
            value=", ".join(f"`{m}`" for m in result["stack_mods"]) + "\n(often, but not always, the culprit)",
            inline=False,
        )
    if own_mods:
        embed.add_field(name=f"\U0001F9E9 {own_title}", value="\n".join(own_mods)[:1024], inline=False)
    embed.set_footer(text="Automatic analysis - it can be wrong. Still stuck? Open a ticket.")
    return embed


async def _fetch_paste(url: str) -> str | None:
    """Raw text of a paste link (mclo.gs, pastebin, pastes.dev, GitHub gist)."""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "ErikEdits-Discord-Bot crash analyzer"}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                data = await r.content.read(MAX_DECOMPRESSED)
            if "api.github.com/gists/" not in url:
                return data.decode("utf-8", errors="replace")
            parts = []
            for f in (json.loads(data).get("files") or {}).values():
                content = f.get("content") or ""
                if f.get("truncated") and f.get("raw_url"):
                    async with session.get(f["raw_url"], timeout=aiohttp.ClientTimeout(total=15)) as r:
                        content = (await r.content.read(MAX_DECOMPRESSED)).decode("utf-8", errors="replace")
                parts.append(f"===== {f.get('filename', 'file')} =====\n{content}")
            return "\n\n".join(parts) or None
    except Exception:
        log.warning("Couldn't load paste %s", url)
        return None


def _version_matches(installed: str, latest: str) -> bool:
    a, b = installed.lower().lstrip("v"), latest.lower().lstrip("v")
    return a == b or b in a or a in b


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class CrashAnalyzer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._projects: tuple[float, list] = (0.0, [])
        bot.add_view(CrashPanelView())
        bot.add_view(CrashChannelView())
        if _config().get("auto_close_hours"):
            self.cleanup_loop.start()

    def cog_unload(self):
        if self.cleanup_loop.is_running():
            self.cleanup_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not feature_on("crash_analyzer") or not _is_analyzer_channel(message.channel):
            return
        opener = _opener_id(message.channel)
        if message.author.id != opener and not is_staff(message.author, _support_roles()):
            return

        max_bytes = int(_config().get("max_file_mb", 8)) * 1024 * 1024
        jobs: list[tuple[str, str]] = []  # (source name, text)
        skipped: list[str] = []
        for attachment in message.attachments[:MAX_FILES_PER_MESSAGE]:
            name = attachment.filename
            if not name.lower().endswith(TEXT_EXTENSIONS):
                skipped.append(f"`{name}` (not a log file)")
                continue
            if attachment.size > max_bytes:
                skipped.append(f"`{name}` (bigger than {max_bytes // (1024 * 1024)} MB)")
                continue
            try:
                data = await attachment.read()
            except (discord.HTTPException, discord.NotFound):
                skipped.append(f"`{name}` (download failed)")
                continue
            text = _decode(data, name)
            if text is None:
                skipped.append(f"`{name}` (couldn't read it as text)")
                continue
            jobs.append((name, text))

        links = 0
        for pattern, label, url in PASTE_SOURCES:
            for paste_id in pattern.findall(message.content or ""):
                if links >= 3:
                    break
                links += 1
                text = await _fetch_paste(url.format(paste_id))
                if text:
                    jobs.append((label.format(paste_id), text))
                else:
                    skipped.append(f"{label.format(paste_id)} (couldn't load it)")

        if not jobs and not skipped:
            if not self.bot.intents.message_content:
                log.warning("Crash analyzer can't see uploads: Message Content intent is off")
            return

        async with message.channel.typing():
            if jobs:
                # Several files (crash report + latest.log) are analyzed together.
                source = " + ".join(name for name, _ in jobs)
                text = jobs[0][1] if len(jobs) == 1 else "\n\n".join(f"===== {n} =====\n{t}" for n, t in jobs)
                # Regex work on a multi-MB log can take a moment - keep the event loop free.
                result = await asyncio.to_thread(analyze, text)
                own_lines, own_keys = await self.own_mod_check(result)
                embed = build_result_embed(source, result, own_lines, self._own_title())
                try:
                    await message.reply(embed=embed, view=CrashChannelView(), mention_author=False)
                except discord.HTTPException:
                    log.exception("Failed to send crash analysis")
                log.info("Analyzed %s for %s: %d finding(s)", source, message.author, len(result["findings"]))
                try:
                    from cogs.stats import record_event
                    record_event(message.guild.id, "crash_analyses")
                except Exception:
                    pass
                await self.track(message.guild, message.author, result, own_keys)
        if skipped:
            try:
                await message.reply("Skipped: " + ", ".join(skipped), mention_author=False)
            except discord.HTTPException:
                pass

    # -------- The owner's own mods ------------------------------------------

    @staticmethod
    def _own_title() -> str:
        name = SERVER_TEMPLATE.get("modrinth", {}).get("username") or ""
        return f"{name}'s mods" if name else "Mods from this server"

    async def _own_projects(self) -> list:
        username = SERVER_TEMPLATE.get("modrinth", {}).get("username")
        if not username:
            return []
        if time.monotonic() - self._projects[0] < PROJECTS_CACHE_SECONDS and self._projects[1]:
            return self._projects[1]
        from cogs.modrinth import _fetch_user_projects
        projects = await _fetch_user_projects(username) or []
        if projects:
            self._projects = (time.monotonic(), projects)
        return projects or self._projects[1]

    async def own_mod_check(self, result: dict) -> tuple[list[str], list[str]]:
        """Lines for the embed + keys of problems (old version / wrong MC version) for tracking."""
        installed = result.get("mods") or {}
        if not installed:
            return [], []
        try:
            projects = await self._own_projects()
        except Exception:
            log.exception("Couldn't load the own Modrinth projects")
            return [], []
        by_norm = {_norm(mod_id): (mod_id, ver) for mod_id, ver in installed.items()}
        mc = result["info"].get("minecraft")
        loader = (result["info"].get("loader") or "").lower()
        lines, keys = [], []
        from cogs.modrinth import _latest_matching_version
        async with aiohttp.ClientSession() as session:
            for p in projects:
                names = {_norm(p.get("slug") or ""), _norm(p.get("title") or "")} - {""}
                hit = next((by_norm[n] for n in names if n in by_norm), None)
                if hit is None:
                    continue
                title, slug, ver = p.get("title") or hit[0], p.get("slug") or p.get("id"), hit[1]
                url = f"https://modrinth.com/mod/{slug}"
                if mc and p.get("game_versions") and mc not in p["game_versions"]:
                    lines.append(f"\u274C **{title}** `{ver}` isn't made for Minecraft {mc} - [versions]({url}/versions)")
                    keys.append(f"own:{slug}:mc")
                    continue
                if loader and p.get("loaders") and loader not in p["loaders"]:
                    lines.append(f"\u274C **{title}** has no version for {result['info'].get('loader')} - [versions]({url}/versions)")
                    keys.append(f"own:{slug}:loader")
                    continue
                latest = None
                if mc and loader:
                    try:
                        latest = await _latest_matching_version(session, p["id"], mc, loader)
                    except Exception:
                        latest = None
                if latest and not _version_matches(ver, latest.get("version_number", "")):
                    lines.append(f"\u2B06 **{title}**: installed `{ver}`, newest for {mc}: "
                                 f"`{latest['version_number']}` - [update]({url}/version/{latest.get('id', '')})")
                    keys.append(f"own:{slug}:old")
                else:
                    lines.append(f"\u2705 **{title}** `{ver}`" + (" is up to date" if latest else ""))
        return lines, keys

    # -------- Problems many people have -------------------------------------

    async def track(self, guild: discord.Guild, user: discord.abc.User, result: dict, own_keys: list[str]) -> None:
        days = float(_config().get("frequent_days", 7))
        threshold = int(_config().get("frequent_threshold", 3))
        if threshold <= 0:
            return
        now = time.time()
        error = (result.get("root_error") or "")[:150]
        problems = {}   # key -> (label, fix)
        for f in result["findings"]:
            if f.severity >= 2:
                problems[f.key or f.title] = (f.title + (f" ({f.key.split(':', 1)[1]})" if ":" in f.key else ""), f.fix)
        for sus in result.get("suspects") or []:
            if sus["likelihood"] == "very likely":
                problems[f"mod:{_norm(sus['mod'])}"] = (f"Crashes caused by **{sus['mod']}**",
                                                        f"Check the newest version of {sus['mod']} and known issues.")
        for key in own_keys:
            slug, kind = key.split(":")[1:3]
            label = {"old": "people use an old version", "mc": "used on a Minecraft version it doesn't support",
                     "loader": "used on a loader it doesn't support"}[kind]
            problems[key] = (f"**{slug}**: {label}", "Maybe mention it in the mod description / announcements.")
        if not problems:
            return
        data = load_json(CRASH_STATS_FILE)
        sigs = data.setdefault("problems", {})
        notify = []
        for key, (label, fix) in problems.items():
            entry = sigs.setdefault(key, {"events": [], "notified": 0})
            entry.update(label=label, fix=fix)
            entry["events"] = [e for e in entry["events"] if now - e[0] < days * 86400] + [[now, user.id, error]]
            people = {e[1] for e in entry["events"]}
            if len(people) >= threshold and now - entry.get("notified", 0) > days * 86400:
                entry["notified"] = now
                notify.append((key, entry, len(people)))
        for key in [k for k, e in sigs.items() if not e["events"] or now - e["events"][-1][0] > days * 86400]:
            sigs.pop(key)
        save_json(CRASH_STATS_FILE, data)
        for key, entry, count in notify:
            await self._notify_frequent(guild, entry, count, days)

    async def _notify_frequent(self, guild: discord.Guild, entry: dict, count: int, days: float) -> None:
        embed = discord.Embed(
            title="\U0001F501 Crash problem seen often",
            description=f"{entry['label']}\n\n**{count} different people** had this in the last {days:g} days.",
            color=0xE67E22,
        )
        embed.add_field(name="Suggested fix", value=entry["fix"][:1024], inline=False)
        examples = [f"<@{uid}> <t:{int(ts)}:R>" + (f" - `{err[:90]}`" if err else "")
                    for ts, uid, err in entry["events"][-5:]]
        embed.add_field(name="Latest cases", value="\n".join(examples)[:1024], inline=False)
        embed.set_footer(text=f"Crash analyzer - {guild.name}")
        recipients = {guild.owner} if guild.owner else set()
        wanted = {n.lower() for n in _config().get("notify_users", [])}
        recipients |= {m for m in guild.members if m.name.lower() in wanted}
        for person in recipients:
            try:
                await person.send(embed=embed)
            except discord.HTTPException:
                pass
        log.info("Frequent crash problem reported: %s (%d people)", entry["label"], count)

    @tasks.loop(minutes=30)
    async def cleanup_loop(self):
        hours = _config().get("auto_close_hours", 0)
        if not hours:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        for guild in self.bot.guilds:
            category = discord.utils.get(guild.categories, name=_category_name())
            if category is None:
                continue
            for channel in list(category.text_channels):
                if _opener_id(channel) is None:
                    continue
                try:
                    last = None
                    async for msg in channel.history(limit=1):
                        last = msg
                    last_activity = last.created_at if last else channel.created_at
                    if last_activity < cutoff:
                        await channel.delete(reason=f"Crash analyzer idle for {hours}h")
                        log.info("Deleted idle crash analyzer channel %s", channel.name)
                except (discord.Forbidden, discord.NotFound):
                    pass
                except Exception:
                    log.exception("Crash analyzer cleanup failed for %s", channel.name)

    @cleanup_loop.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CrashAnalyzer(bot))
