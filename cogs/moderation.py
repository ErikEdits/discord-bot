"""Moderation slash commands: timeout, ban, unban, kick, purge, warn, warnings."""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from server_template import CHANNELS

log = logging.getLogger("setup-bot.moderation")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
WARNINGS_FILE = DATA_DIR / "warnings.json"

DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d|w)?$", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, None: 1}


def parse_duration(text: str):
    if not text:
        return None
    m = DURATION_RE.match(text.strip())
    if not m:
        return None
    n, unit = m.group(1), m.group(2)
    try:
        return timedelta(seconds=int(n) * UNIT_SECONDS[unit.lower() if unit else None])
    except (KeyError, ValueError):
        return None


def load_warnings() -> dict:
    if not WARNINGS_FILE.exists():
        return {}
    try:
        return json.loads(WARNINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read warnings file")
        return {}


def save_warnings(data: dict) -> None:
    WARNINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def send_to_mod_logs(guild: discord.Guild, embed: discord.Embed) -> None:
    channel = discord.utils.get(guild.text_channels, name=CHANNELS["mod_logs"])
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass


def mod_action_embed(title: str, color: int, member, moderator, reason: str, extra: dict | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
    embed.add_field(name="Moderator", value=moderator.mention, inline=True)
    if extra:
        for k, v in extra.items():
            embed.add_field(name=k, value=v, inline=True)
    embed.add_field(name="Reason", value=reason or "*(none)*", inline=False)
    return embed


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="timeout", description="Time out a member (max 28 days).")
    @app_commands.describe(member="Member to time out", duration="e.g. 10s, 5m, 1h, 2d, 1w", reason="Reason shown in the audit log")
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("You need the Moderate Members permission.", ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta.total_seconds() < 1:
            await interaction.response.send_message("Invalid duration. Use forms like 10s, 5m, 1h, 2d, 1w.", ephemeral=True)
            return
        if delta > timedelta(days=28):
            await interaction.response.send_message("Max timeout is 28 days.", ephemeral=True)
            return
        try:
            await member.timeout(delta, reason=f"By {interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't time out that member (role hierarchy or missing perms).", ephemeral=True)
            return
        except Exception:
            log.exception("Timeout failed")
            await interaction.response.send_message("Timeout failed - see logs.", ephemeral=True)
            return
        await interaction.response.send_message(f"Timed out {member.mention} for `{duration}`.\nReason: {reason}", ephemeral=True)
        embed = mod_action_embed("Member Timed Out", 0xF1C40F, member, interaction.user, reason, {"Duration": duration})
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="untimeout", description="Remove an active timeout from a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Manual removal"):
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("You need the Moderate Members permission.", ephemeral=True)
            return
        try:
            await member.timeout(None, reason=f"By {interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't modify that member.", ephemeral=True)
            return
        await interaction.response.send_message(f"Removed timeout from {member.mention}.", ephemeral=True)
        embed = mod_action_embed("Timeout Removed", 0x3498DB, member, interaction.user, reason)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("You need Kick Members permission.", ephemeral=True)
            return
        try:
            await member.kick(reason=f"By {interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't kick that member.", ephemeral=True)
            return
        await interaction.response.send_message(f"Kicked {member}.\nReason: {reason}", ephemeral=True)
        embed = mod_action_embed("Member Kicked", 0xE67E22, member, interaction.user, reason)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="ban", description="Ban a member.")
    @app_commands.describe(member="Member to ban", reason="Audit log reason", delete_message_days="Days of recent messages to delete (0-7)")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_message_days: int = 0):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("You need Ban Members permission.", ephemeral=True)
            return
        delete_message_days = max(0, min(7, delete_message_days))
        try:
            await member.ban(reason=f"By {interaction.user}: {reason}", delete_message_days=delete_message_days)
        except discord.Forbidden:
            await interaction.response.send_message("I can't ban that member.", ephemeral=True)
            return
        await interaction.response.send_message(f"Banned {member}.\nReason: {reason}", ephemeral=True)
        embed = mod_action_embed("Member Banned", 0xC0392B, member, interaction.user, reason, {"Messages deleted (days)": str(delete_message_days)})
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "Manual unban"):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("You need Ban Members permission.", ephemeral=True)
            return
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("Invalid user ID.", ephemeral=True)
            return
        user = discord.Object(id=uid)
        try:
            await interaction.guild.unban(user, reason=f"By {interaction.user}: {reason}")
        except discord.NotFound:
            await interaction.response.send_message("That user isn't banned.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("I can't unban that user.", ephemeral=True)
            return
        await interaction.response.send_message(f"Unbanned user ID {uid}.", ephemeral=True)
        embed = discord.Embed(title="Member Unbanned", color=0x3498DB, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User ID", value=str(uid), inline=False)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="purge", description="Delete the last N messages in this channel (1-100).")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100], member: discord.Member | None = None):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("You need Manage Messages permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        check = (lambda m: m.author.id == member.id) if member else None
        try:
            deleted = await interaction.channel.purge(limit=count, check=check)
        except discord.Forbidden:
            await interaction.followup.send("I can't delete messages in this channel.", ephemeral=True)
            return
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
        embed = discord.Embed(title="Messages Purged", color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
        embed.add_field(name="Count", value=str(len(deleted)), inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        if member:
            embed.add_field(name="Filtered by member", value=member.mention, inline=False)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="warn", description="Warn a member. Stored persistently.")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("You need Moderate Members permission.", ephemeral=True)
            return
        data = load_warnings()
        g, u = str(interaction.guild.id), str(member.id)
        data.setdefault(g, {}).setdefault(u, []).append({
            "id": uuid.uuid4().hex[:8],
            "reason": reason,
            "moderator_id": interaction.user.id,
            "moderator_name": str(interaction.user),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        save_warnings(data)
        count = len(data[g][u])
        await interaction.response.send_message(f"Warned {member.mention}. They now have {count} warning(s).", ephemeral=True)
        embed = mod_action_embed("Member Warned", 0xF39C12, member, interaction.user, reason, {"Total warnings": str(count)})
        await send_to_mod_logs(interaction.guild, embed)
        try:
            await member.send(f"You were warned in **{interaction.guild.name}**.\nReason: {reason}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @app_commands.command(name="warnings", description="List a member's warnings.")
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("You need Moderate Members permission.", ephemeral=True)
            return
        data = load_warnings()
        warns = data.get(str(interaction.guild.id), {}).get(str(member.id), [])
        if not warns:
            await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Warnings for {member}", color=0xF39C12)
        for w in warns[-15:]:
            ts = w.get("timestamp", "")
            mod = w.get("moderator_name", "Unknown")
            embed.add_field(
                name=f"#{w.get('id', '?')} - {ts[:19]}",
                value=f"By **{mod}**\n{w.get('reason', 'No reason')}",
                inline=False,
            )
        embed.set_footer(text=f"Total: {len(warns)} warning(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("You need Moderate Members permission.", ephemeral=True)
            return
        data = load_warnings()
        g, u = str(interaction.guild.id), str(member.id)
        removed = len(data.get(g, {}).get(u, []))
        if removed and g in data and u in data[g]:
            del data[g][u]
            save_warnings(data)
        await interaction.response.send_message(f"Cleared {removed} warning(s) for {member.mention}.", ephemeral=True)
        if removed:
            embed = mod_action_embed("Warnings Cleared", 0x3498DB, member, interaction.user, f"Cleared {removed} warning(s)")
            await send_to_mod_logs(interaction.guild, embed)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
