"""Member counter: a locked voice channel whose name shows the member count,
e.g. "👥 Members: 342".

The channel is created by /setup and /update (ensure_counter_channel) at the
top of the configured category; its ID is remembered in data/settings.json.
Discord only allows 2 renames per channel every 10 minutes, so the name is
refreshed every 10 minutes (only if the number changed).
Config: SERVER_TEMPLATE["member_counter"].

A second locked channel shows the total mod downloads (Modrinth + CurseForge),
e.g. "📥 Downloads: 12.3k". It's created automatically next to the member
counter and refreshed every 30 minutes. Config: SERVER_TEMPLATE["download_counter"].
"""

import logging

import discord
from discord.ext import commands, tasks

from cogs.common import get_setting, set_setting
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.member_counter")

SETTING_KEY = "member_counter_channels"
DOWNLOAD_SETTING_KEY = "download_counter_channels"


def _config() -> dict:
    return SERVER_TEMPLATE.get("member_counter", {})


def _count(guild: discord.Guild) -> int:
    if not _config().get("count_bots", True) and guild.chunked:
        return sum(1 for m in guild.members if not m.bot)
    return guild.member_count or 0


def counter_name(guild: discord.Guild) -> str:
    return _config().get("name_format", "\U0001F465 Members: {count}").format(count=f"{_count(guild):,}")


def _download_config() -> dict:
    return SERVER_TEMPLATE.get("download_counter", {})


def compact(number: int) -> str:
    """12345 -> 12.3k, 1234567 -> 1.23M, 999 -> 999."""
    if number >= 1_000_000:
        text = f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    elif number >= 1_000:
        text = f"{number / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    else:
        text = str(number)
    return text


def download_counter_name(total: int) -> str:
    return _download_config().get("name_format", "\U0001F4E5 Downloads: {count}").format(count=compact(total))


def _stored_channel(guild: discord.Guild, key: str = SETTING_KEY) -> discord.VoiceChannel | None:
    channel_id = (get_setting(key) or {}).get(str(guild.id))
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.VoiceChannel) else None


async def _create_locked_voice(guild: discord.Guild, name: str, key: str, category_name: str,
                               position: int, reason: str) -> str:
    category = discord.utils.get(guild.categories, name=category_name)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
    }
    try:
        channel = await guild.create_voice_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            position=position,
            reason=reason,
        )
    except discord.HTTPException:
        log.exception("Failed to create counter channel (%s)", reason)
        return "failed"
    stored = get_setting(key) or {}
    stored[str(guild.id)] = channel.id
    set_setting(key, stored)
    log.info("Created counter channel '%s' in %s", name, guild.name)
    return "created"


async def ensure_counter_channel(guild: discord.Guild) -> str:
    """Create the counter channel if it's missing. Returns 'created', 'exists', 'disabled' or 'failed'."""
    if not _config().get("enabled", True):
        return "disabled"
    if _stored_channel(guild) is not None:
        return "exists"
    return await _create_locked_voice(guild, counter_name(guild), SETTING_KEY, _config().get("category", ""),
                                      0, "Member counter")


async def ensure_download_channel(guild: discord.Guild, total: int) -> discord.VoiceChannel | None:
    if _stored_channel(guild, DOWNLOAD_SETTING_KEY) is None:
        category = _download_config().get("category") or _config().get("category", "")
        await _create_locked_voice(guild, download_counter_name(total), DOWNLOAD_SETTING_KEY, category,
                                   1, "Download counter")
    return _stored_channel(guild, DOWNLOAD_SETTING_KEY)


async def total_downloads() -> int | None:
    """All downloads of all projects (Modrinth + CurseForge). None if the API can't be reached."""
    from cogs.mod_stats import _dl, _fetch_projects
    try:
        projects = await _fetch_projects()
    except Exception:
        log.exception("Download counter: fetching projects failed")
        return None
    if not projects:
        return None
    total = sum(_dl(p) for p in projects)
    note_downloads(total)
    return total


def note_downloads(total: int, today: str | None = None) -> None:
    """Remember the first total of each day, so the panel can show "downloads today"."""
    from cogs.stats import today_key
    today = today or today_key()
    daily = get_setting("downloads_daily") or {}
    if daily.get("date") != today:
        daily = {"date": today, "start": daily.get("last", total), "last": total}
    else:
        daily["last"] = total
    set_setting("downloads_daily", daily)


class MemberCounter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if _config().get("enabled", True):
            self.update_loop.start()
        if _download_config().get("enabled", True):
            self.download_loop.start()

    def cog_unload(self):
        for loop in (self.update_loop, self.download_loop):
            if loop.is_running():
                loop.cancel()

    @tasks.loop(minutes=10)
    async def update_loop(self):
        for guild in self.bot.guilds:
            channel = _stored_channel(guild)
            if channel is None:
                continue
            name = counter_name(guild)
            if channel.name == name:
                continue
            try:
                await channel.edit(name=name, reason="Member count changed")
            except discord.HTTPException:
                log.warning("Could not rename member counter in %s", guild.name)

    @tasks.loop(minutes=30)
    async def download_loop(self):
        from cogs.common import feature_on
        if not feature_on("download_counter"):
            return
        total = await total_downloads()
        if total is None:
            return
        name = download_counter_name(total)
        for guild in self.bot.guilds:
            channel = await ensure_download_channel(guild, total)
            if channel is None or channel.name == name:
                continue
            try:
                await channel.edit(name=name, reason="Download count changed")
            except discord.HTTPException:
                log.warning("Could not rename download counter in %s", guild.name)

    @update_loop.before_loop
    @download_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MemberCounter(bot))
