"""Temporary roles: give a role for a while, the bot takes it away again.

    /temprole add @member @role 7d     give it for 7 days (1h, 12h, 2w ... up to a year)
    /temprole remove @member @role     take it away now
    /temprole list                     all running temporary roles

Stored in data/temp_roles.json, checked every minute - also after a restart.
Needs Manage Roles, and the role must be below the bot's highest role.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, format_duration, load_json, parse_duration, save_json

log = logging.getLogger("setup-bot.temp_roles")

STATE_FILE = DATA_DIR / "temp_roles.json"
MAX_SECONDS = 365 * 86400


class TempRoles(commands.Cog):
    group = app_commands.Group(name="temprole", description="Give a role for a limited time.",
                               default_permissions=discord.Permissions(manage_roles=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @staticmethod
    def _load() -> dict:
        data = load_json(STATE_FILE)
        data.setdefault("roles", [])   # [{guild, user, role, until, by}]
        return data

    def _can_manage(self, interaction: discord.Interaction, role: discord.Role) -> str | None:
        me = interaction.guild.me
        if not interaction.user.guild_permissions.manage_roles:
            return "You need the Manage Roles permission."
        if role.is_default() or role.managed:
            return "That role can't be given."
        if role >= me.top_role:
            return "That role is above the bot's highest role - move the bot's role up first."
        if role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
            return "That role is above your own highest role."
        return None

    @group.command(name="add", description="Give someone a role for a limited time.")
    @app_commands.describe(duration="e.g. 1h, 12h, 7d, 2w")
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role, duration: str):
        problem = self._can_manage(interaction, role)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta.total_seconds() < 60 or delta.total_seconds() > MAX_SECONDS:
            await interaction.response.send_message("Use a duration like 1h, 12h, 7d or 2w (1 minute to 1 year).",
                                                    ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"Temporary role ({format_duration(delta)}) by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't give the role - missing permissions?", ephemeral=True)
            return
        until = time.time() + delta.total_seconds()
        data = self._load()
        data["roles"] = [r for r in data["roles"] if not (r["guild"] == interaction.guild.id and r["user"] == member.id
                                                          and r["role"] == role.id)]
        data["roles"].append({"guild": interaction.guild.id, "user": member.id, "role": role.id, "until": until,
                              "by": str(interaction.user)})
        save_json(STATE_FILE, data)
        await interaction.response.send_message(
            f"{member.mention} has {role.mention} until <t:{int(until)}:f> (<t:{int(until)}:R>).",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="remove", description="Take a temporary role away now.")
    async def remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        problem = self._can_manage(interaction, role)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        data = self._load()
        before = len(data["roles"])
        data["roles"] = [r for r in data["roles"] if not (r["guild"] == interaction.guild.id and r["user"] == member.id
                                                          and r["role"] == role.id)]
        save_json(STATE_FILE, data)
        try:
            await member.remove_roles(role, reason=f"Temporary role removed by {interaction.user}")
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            f"Removed {role.mention} from {member.mention}." if before != len(data["roles"])
            else f"{member.mention} had no temporary {role.mention} - removed it anyway.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="list", description="Show all running temporary roles.")
    async def list_roles(self, interaction: discord.Interaction):
        rows = [r for r in self._load()["roles"] if r["guild"] == interaction.guild.id]
        rows.sort(key=lambda r: r["until"])
        lines = [f"<@{r['user']}> - <@&{r['role']}> - ends <t:{int(r['until'])}:R>" for r in rows[:30]]
        await interaction.response.send_message("\n".join(lines) or "No temporary roles right now.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=1)
    async def loop(self):
        data = self._load()
        now = time.time()
        due = [r for r in data["roles"] if r["until"] <= now]
        if not due:
            return
        keep = []
        for r in data["roles"]:
            if r["until"] > now:
                keep.append(r)
                continue
            guild = self.bot.get_guild(r["guild"])
            if guild is None:
                continue
            member, role = guild.get_member(r["user"]), guild.get_role(r["role"])
            if member is not None and role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Temporary role ended")
                    log.info("Temporary role %s of %s ended", role.name, member)
                except discord.HTTPException:
                    keep.append(dict(r, until=now + 600))   # try again in 10 minutes
                    continue
        data["roles"] = keep
        save_json(STATE_FILE, data)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(TempRoles(bot))
