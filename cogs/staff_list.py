"""Staff list in CHANNELS["staff_team"].

One embed listing the members of each staff role (SERVER_TEMPLATE["staff_list"]
["roles"], highest first). Everyone is listed only under their highest staff
role. The embed is updated automatically when staff roles change (checked once
a minute) and the message ID is kept in data/settings.json.
"""

import logging

import discord
from discord.ext import commands, tasks

from cogs.common import get_setting, set_setting
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.staff_list")

SETTING_KEY = "staff_list_messages"


def _config() -> dict:
    return SERVER_TEMPLATE.get("staff_list", {})


def build_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title="\U0001F46E The team", color=0x5865F2,
                          description="Questions? Open a ticket - the team is happy to help.")
    listed: set[int] = set()
    for role_name in _config().get("roles", []):
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            continue
        members = [m for m in role.members if not m.bot and m.id not in listed]
        listed.update(m.id for m in members)
        value = "\n".join(m.mention for m in sorted(members, key=lambda m: m.display_name.lower())) or "*nobody yet*"
        embed.add_field(name=f"{role.name} ({len(members)})", value=value[:1024], inline=True)
    embed.set_footer(text="Updated automatically")
    return embed


class StaffList(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dirty = True
        if _config().get("enabled", True):
            self.update_loop.start()

    def cog_unload(self):
        if self.update_loop.is_running():
            self.update_loop.cancel()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        names = set(_config().get("roles", []))
        changed = {r.name for r in set(before.roles) ^ set(after.roles)}
        if changed & names or (before.display_name != after.display_name and {r.name for r in after.roles} & names):
            self.dirty = True

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if {r.name for r in member.roles} & set(_config().get("roles", [])):
            self.dirty = True

    async def refresh(self) -> None:
        stored = get_setting(SETTING_KEY) or {}
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=CHANNELS.get("staff_team", ""))
            if channel is None:
                continue
            embed = build_embed(guild)
            message = None
            if stored.get(str(guild.id)):
                try:
                    message = await channel.fetch_message(stored[str(guild.id)])
                except (discord.NotFound, discord.Forbidden):
                    message = None
            try:
                if message:
                    await message.edit(embed=embed)
                else:
                    message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    stored[str(guild.id)] = message.id
            except discord.HTTPException:
                log.warning("Could not update the staff list in %s", guild.name)
        set_setting(SETTING_KEY, stored)

    @tasks.loop(seconds=60)
    async def update_loop(self):
        if self.dirty:
            self.dirty = False
            await self.refresh()

    @update_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(StaffList(bot))
