"""Send each new member a welcome DM with the rules link and a quick onboarding pointer.

Config in SERVER_TEMPLATE['welcome_dm']:
    enabled  bool
    message  str   - supports {user}, {guild}, {rules}, {tickets}, {roles} placeholders
"""

import logging

import discord
from discord.ext import commands

from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.welcome_dm")


def _channel_mention(guild: discord.Guild, key: str) -> str:
    name = CHANNELS.get(key)
    if not name:
        return f"#{key}"
    ch = discord.utils.get(guild.text_channels, name=name)
    return ch.mention if ch else f"#{name}"


class WelcomeDM(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        config = SERVER_TEMPLATE.get("welcome_dm", {})
        if not config.get("enabled"):
            return
        guild = member.guild
        message_template = config.get("message", "Welcome to **{guild}**, {user}!")
        try:
            text = message_template.format(
                user=member.mention,
                guild=guild.name,
                rules=_channel_mention(guild, "rules"),
                tickets=_channel_mention(guild, "tickets"),
                roles=_channel_mention(guild, "roles"),
                general=_channel_mention(guild, "general"),
                introductions=_channel_mention(guild, "introductions"),
            )
        except (KeyError, IndexError):
            text = f"Welcome to **{guild.name}**, {member.mention}!"

        embed = discord.Embed(
            title=f"Welcome to {guild.name}!",
            description=text,
            color=0x57F287,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="You can reply here if you have questions about joining the server.")

        try:
            await member.send(embed=embed)
            log.info("Sent welcome DM to %s", member)
        except discord.Forbidden:
            log.info("Could not DM %s (DMs disabled)", member)
        except discord.HTTPException:
            log.exception("Welcome DM failed for %s", member)


async def setup(bot):
    await bot.add_cog(WelcomeDM(bot))
