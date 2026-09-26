"""Crash-log analyzer.

A panel in CHANNELS["crash_analyzer"] has an "Analyze my crash" button. Clicking
it opens a private channel (like a ticket) in the CRASH REPORTS category. The
user uploads latest.log / a crash report / hs_err_pid*.log there (or pastes a
mclo.gs link) and the bot replies with what went wrong and how to fix it.

Files are only read in memory and never stored. Analyzer channels are deleted
when closed or after `auto_close_hours` without activity - no transcripts, so
nothing piles up on the bot host.

Needs the Message Content intent to see the uploaded files.
Config: SERVER_TEMPLATE["crash_analyzer"]. Pause with /maintenance crash.
"""

import asyncio
import gzip
import io
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from cogs.common import is_staff
from crashlog_analyzer import analyze
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.crash")

TOPIC_PREFIX = "crash user "
TEXT_EXTENSIONS = (".log", ".txt", ".gz", ".crash", ".out")
MCLOGS_RE = re.compile(r"https?://mclo\.gs/(\w+)")
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
        from cogs.tickets import _open_ticket
        await _open_ticket(interaction, "bug")

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


def build_result_embed(source: str, result: dict) -> discord.Embed:
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
    if result.get("stack_mods"):
        embed.add_field(
            name="Mods in the stack trace",
            value=", ".join(f"`{m}`" for m in result["stack_mods"]) + "\n(often, but not always, the culprit)",
            inline=False,
        )
    embed.set_footer(text="Automatic analysis - it can be wrong. Still stuck? Open a ticket.")
    return embed


async def _fetch_mclogs(log_id: str) -> str | None:
    url = f"https://api.mclo.gs/1/raw/{log_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                data = await r.content.read(MAX_DECOMPRESSED)
                return data.decode("utf-8", errors="replace")
    except Exception:
        log.exception("mclo.gs fetch failed")
        return None


class CrashAnalyzer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
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
        if not _is_analyzer_channel(message.channel):
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

        for log_id in MCLOGS_RE.findall(message.content or "")[:2]:
            text = await _fetch_mclogs(log_id)
            if text:
                jobs.append((f"mclo.gs/{log_id}", text))
            else:
                skipped.append(f"mclo.gs/{log_id} (couldn't load it)")

        if not jobs and not skipped:
            if not self.bot.intents.message_content:
                log.warning("Crash analyzer can't see uploads: Message Content intent is off")
            return

        async with message.channel.typing():
            for source, text in jobs:
                # Regex work on a multi-MB log can take a moment - keep the event loop free.
                result = await asyncio.to_thread(analyze, text)
                embed = build_result_embed(source, result)
                try:
                    await message.reply(embed=embed, view=CrashChannelView(), mention_author=False)
                except discord.HTTPException:
                    log.exception("Failed to send crash analysis")
                log.info("Analyzed %s for %s: %d finding(s)", source, message.author, len(result["findings"]))
        if skipped:
            try:
                await message.reply("Skipped: " + ", ".join(skipped), mention_author=False)
            except discord.HTTPException:
                pass

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
