"""Member counter: a locked voice channel whose name shows the member count,
e.g. "👥 Members: 342".

The channel is created by /setup and /update (ensure_counter_channel) at the
top of the configured category; its ID is remembered in data/settings.json.
Discord only allows 2 renames per channel every 10 minutes, so the name is
refreshed every 10 minutes (only if the number changed).
Config: SERVER_TEMPLATE["member_counter"].
"""

import logging

import discord
from discord.ext import commands, tasks

from cogs.common import get_setting, set_setting
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.member_counter")

SETTING_KEY = "member_counter_channels"


def _config() -> dict:
    return SERVER_TEMPLATE.get("member_counter", {})


def _count(guild: discord.Guild) -> int:
    if not _config().get("count_bots", True) and guild.chunked:
        return sum(1 for m in guild.members if not m.bot)
    return guild.member_count or 0


def counter_name(guild: discord.Guild) -> str:
    return _config().get("name_format", "\U0001F465 Members: {count}").format(count=f"{_count(guild):,}")


def _stored_channel(guild: discord.Guild) -> discord.VoiceChannel | None:
    channel_id = (get_setting(SETTING_KEY) or {}).get(str(guild.id))
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.VoiceChannel) else None


async def ensure_counter_channel(guild: discord.Guild) -> str:
    """Create the counter channel if it's missing. Returns 'created', 'exists', 'disabled' or 'failed'."""
    if not _config().get("enabled", True):
        return "disabled"
    if _stored_channel(guild) is not None:
        return "exists"
    category = discord.utils.get(guild.categories, name=_config().get("category", ""))
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
    }
    try:
        channel = await guild.create_voice_channel(
            name=counter_name(guild),
            category=category,
            overwrites=overwrites,
            position=0,
            reason="Member counter",
        )
    except discord.HTTPException:
        log.exception("Failed to create member counter channel")
        return "failed"
    stored = get_setting(SETTING_KEY) or {}
    stored[str(guild.id)] = channel.id
    set_setting(SETTING_KEY, stored)
    log.info("Created member counter channel in %s", guild.name)
    return "created"


class MemberCounter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if _config().get("enabled", True):
            self.update_loop.start()

    def cog_unload(self):
        if self.update_loop.is_running():
            self.update_loop.cancel()

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

    @update_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MemberCounter(bot))
