"""Audit-style logging, routed into themed log channels.

Routing (all under the LOGS category, staff-only):
    message -> #message-logs   (deletes, edits)
    member  -> #member-logs    (joins, leaves, role/nick changes, bans)
    voice   -> #voice-logs     (voice joins, leaves, moves)
    server  -> #server-logs    (channel/role create+delete, role permission changes - with who did it)
    mod     -> #mod-logs       (used by the moderation cog + AutoMod alerts)

Falls back to #mod-logs when a themed channel doesn't exist yet
(run /update to create them).

on_member_join / on_member_remove / on_member_update require the
Server Members privileged intent enabled in the Discord Developer Portal.
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from cogs.common import is_selftest_name, was_deleted_by_bot
from server_template import CHANNELS

log = logging.getLogger("setup-bot.logging")

LOG_CHANNEL_KEYS = {
    "message": "message_logs",
    "member": "member_logs",
    "voice": "voice_logs",
    "server": "server_logs",
    "mod": "mod_logs",
}


def _get_log_channel(guild: discord.Guild, kind: str = "mod") -> discord.TextChannel | None:
    if guild is None:
        return None
    name = CHANNELS.get(LOG_CHANNEL_KEYS.get(kind, "mod_logs"))
    channel = discord.utils.get(guild.text_channels, name=name)
    if channel is None:
        # Fall back to mod-logs so events aren't lost before /update creates the themed channels.
        channel = discord.utils.get(guild.text_channels, name=CHANNELS["mod_logs"])
    return channel


async def _safe_send(channel: discord.TextChannel | None, *, embed: discord.Embed) -> None:
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception:
        log.exception("Failed to send log embed")


def _is_temp_voice(channel) -> bool:
    """Join-to-create channels come and go all the time - don't flood #server-logs with them."""
    try:
        from cogs.temp_voice import is_temp_channel
        return is_temp_channel(channel)
    except Exception:
        return False


async def _actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int) -> str | None:
    """Who did it, from the audit log (needs View Audit Log). None if unknown."""
    try:
        async for entry in guild.audit_logs(limit=6, action=action):
            if getattr(entry.target, "id", None) == target_id and \
                    (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 30:
                return entry.user.mention if entry.user else None
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


def _perm_name(name: str) -> str:
    return name.replace("_", " ").title()


def _truncate(text: str, limit: int = 1024) -> str:
    if not text:
        return "*(empty)*"
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or (message.author and message.author.bot):
            return
        if was_deleted_by_bot(message.id):
            return
        ch = _get_log_channel(message.guild, "message")
        if ch is None or ch.id == message.channel.id:
            return
        embed = discord.Embed(
            title="Message Deleted",
            description=_truncate(message.content or ""),
            color=0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Author", value=message.author.mention, inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        if message.attachments:
            embed.add_field(
                name="Attachments",
                value=_truncate("\n".join(a.url for a in message.attachments)),
                inline=False,
            )
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.author.bot:
            return
        if before.content == after.content:
            return  # ignore embed/link updates that fire as edits
        ch = _get_log_channel(after.guild, "message")
        if ch is None:
            return
        embed = discord.Embed(
            title="Message Edited",
            color=0xF39C12,
            timestamp=datetime.now(timezone.utc),
            description=f"[Jump to message]({after.jump_url})",
        )
        embed.add_field(name="Author", value=after.author.mention, inline=True)
        embed.add_field(name="Channel", value=after.channel.mention, inline=True)
        embed.add_field(name="Before", value=_truncate(before.content), inline=False)
        embed.add_field(name="After", value=_truncate(after.content), inline=False)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ch = _get_log_channel(member.guild, "member")
        if ch is None:
            return
        embed = discord.Embed(title="Member Joined", color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, style="R"), inline=True)
        embed.add_field(name="Total Members", value=str(member.guild.member_count), inline=True)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        ch = _get_log_channel(member.guild, "member")
        if ch is None:
            return
        embed = discord.Embed(title="Member Left", color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, style="R"), inline=True)
        embed.add_field(name="Total Members", value=str(member.guild.member_count), inline=True)
        if member.roles[1:]:
            embed.add_field(
                name="Roles",
                value=", ".join(r.mention for r in member.roles[1:][:20]),
                inline=False,
            )
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        ch = _get_log_channel(guild, "member")
        if ch is None:
            return
        embed = discord.Embed(title="Member Banned", color=0xC0392B, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{user.mention} ({user})", inline=False)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        ch = _get_log_channel(guild, "member")
        if ch is None:
            return
        embed = discord.Embed(title="Member Unbanned", color=0x3498DB, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{user.mention} ({user})", inline=False)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        ch = _get_log_channel(after.guild, "member")
        if ch is None:
            return
        changes = []
        if before.nick != after.nick:
            changes.append(("Nickname", f"`{before.nick or '(none)'}` -> `{after.nick or '(none)'}`"))
        added = {r for r in set(after.roles) - set(before.roles) if not is_selftest_name(r.name)}
        removed = {r for r in set(before.roles) - set(after.roles) if not is_selftest_name(r.name)}
        if added:
            changes.append(("Roles Added", ", ".join(r.mention for r in added)))
        if removed:
            changes.append(("Roles Removed", ", ".join(r.mention for r in removed)))
        if not changes:
            return
        embed = discord.Embed(title="Member Updated", color=0x9B59B6, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Member", value=after.mention, inline=False)
        if added or removed:
            by = await _actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
            if by:
                embed.add_field(name="Changed by", value=by, inline=False)
        for name, value in changes:
            embed.add_field(name=name, value=_truncate(value), inline=False)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        ch = _get_log_channel(channel.guild, "server")
        if ch is None or ch.id == getattr(channel, "id", None) or _is_temp_voice(channel) or is_selftest_name(channel.name):
            return
        embed = discord.Embed(title="Channel Created", color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Name", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        ch = _get_log_channel(channel.guild, "server")
        if ch is None or _is_temp_voice(channel) or is_selftest_name(channel.name):
            return
        embed = discord.Embed(title="Channel Deleted", color=0xE74C3C, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Name", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return  # ignore mute/deafen/stream toggles, only log channel changes
        ch = _get_log_channel(member.guild, "voice")
        if ch is None:
            return
        if before.channel is None:
            title, color = "Voice Joined", 0x2ECC71
            detail = after.channel.mention
        elif after.channel is None:
            title, color = "Voice Left", 0x95A5A6
            detail = before.channel.mention
        else:
            title, color = "Voice Moved", 0x3498DB
            detail = f"{before.channel.mention} → {after.channel.mention}"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(name="Channel", value=detail, inline=False)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        ch = _get_log_channel(role.guild, "server")
        if ch is None or is_selftest_name(role.name):
            return
        embed = discord.Embed(title="Role Created", color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Role", value=role.mention, inline=True)
        by = await _actor(role.guild, discord.AuditLogAction.role_create, role.id)
        if by:
            embed.add_field(name="By", value=by, inline=True)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        ch = _get_log_channel(role.guild, "server")
        if ch is None or is_selftest_name(role.name):
            return
        embed = discord.Embed(title="Role Deleted", color=0xE74C3C, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Name", value=role.name, inline=True)
        by = await _actor(role.guild, discord.AuditLogAction.role_delete, role.id)
        if by:
            embed.add_field(name="By", value=by, inline=True)
        await _safe_send(ch, embed=embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if is_selftest_name(after.name):
            return
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` -> `{after.name}`"))
        if before.color != after.color:
            changes.append(("Color", f"`{before.color}` -> `{after.color}`"))
        if before.hoist != after.hoist:
            changes.append(("Shown separately", f"{before.hoist} -> {after.hoist}"))
        if before.mentionable != after.mentionable:
            changes.append(("Mentionable", f"{before.mentionable} -> {after.mentionable}"))
        if before.permissions != after.permissions:
            old, new = dict(before.permissions), dict(after.permissions)
            granted = [_perm_name(p) for p, v in new.items() if v and not old.get(p)]
            revoked = [_perm_name(p) for p, v in old.items() if v and not new.get(p)]
            if granted:
                changes.append(("\u2705 Permissions granted", ", ".join(granted)))
            if revoked:
                changes.append(("\u274C Permissions removed", ", ".join(revoked)))
        if not changes:
            return  # position-only changes happen a lot when roles are dragged around
        ch = _get_log_channel(after.guild, "server")
        if ch is None:
            return
        dangerous = any(n.startswith("\u2705") and ("Administrator" in v or "Manage" in v) for n, v in changes)
        embed = discord.Embed(title="Role Updated", color=0xE67E22 if dangerous else 0x3498DB,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Role", value=after.mention, inline=True)
        by = await _actor(after.guild, discord.AuditLogAction.role_update, after.id)
        embed.add_field(name="By", value=by or "unknown", inline=True)
        for name, value in changes:
            embed.add_field(name=name, value=_truncate(value), inline=False)
        await _safe_send(ch, embed=embed)


async def setup(bot):
    await bot.add_cog(Logging(bot))
