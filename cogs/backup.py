"""/backup-config and /backup-server commands.

backup-config: exports the current server's structural state as JSON
(roles, channels, categories with overwrites and topics, settings).
Useful for inspecting what changed, or as a starting point for restoring
to another guild.
"""

import io
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("setup-bot.backup")


def _overwrites_to_dict(channel: discord.abc.GuildChannel) -> dict:
    out = {}
    for target, perms in channel.overwrites.items():
        name = "@everyone" if isinstance(target, discord.Role) and target.is_default() else getattr(target, "name", str(target))
        allow, deny = perms.pair()
        out[name] = {
            "allow": [p for p, v in allow if v],
            "deny": [p for p, v in deny if v],
        }
    return out


def _role_to_dict(role: discord.Role) -> dict:
    return {
        "name": role.name,
        "color": f"#{role.color.value:06X}",
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "position": role.position,
        "permissions": [p for p, v in role.permissions if v],
    }


def _channel_to_dict(channel: discord.abc.GuildChannel) -> dict:
    base = {
        "name": channel.name,
        "type": str(channel.type),
        "position": channel.position,
        "overwrites": _overwrites_to_dict(channel),
    }
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        base["topic"] = channel.topic
        base["nsfw"] = channel.is_nsfw()
        if isinstance(channel, discord.TextChannel):
            base["slowmode"] = channel.slowmode_delay
    if isinstance(channel, discord.VoiceChannel):
        base["bitrate"] = channel.bitrate
        base["user_limit"] = channel.user_limit
    return base


def _build_snapshot(guild: discord.Guild) -> dict:
    categories = []
    for cat in guild.categories:
        children = [_channel_to_dict(c) for c in cat.channels]
        categories.append({
            "name": cat.name,
            "position": cat.position,
            "overwrites": _overwrites_to_dict(cat),
            "channels": children,
        })
    orphan_channels = [_channel_to_dict(c) for c in guild.channels if c.category is None and not isinstance(c, discord.CategoryChannel)]
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "guild": {
            "id": guild.id,
            "name": guild.name,
            "description": guild.description,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "owner_id": guild.owner_id,
            "member_count": guild.member_count,
            "premium_tier": guild.premium_tier,
            "verification_level": str(guild.verification_level),
            "default_notifications": str(guild.default_notifications),
            "explicit_content_filter": str(guild.explicit_content_filter),
            "features": list(guild.features),
            "afk_channel": guild.afk_channel.name if guild.afk_channel else None,
            "afk_timeout": guild.afk_timeout,
            "system_channel": guild.system_channel.name if guild.system_channel else None,
            "rules_channel": guild.rules_channel.name if guild.rules_channel else None,
            "public_updates_channel": guild.public_updates_channel.name if guild.public_updates_channel else None,
        },
        "roles": [_role_to_dict(r) for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)],
        "categories": categories,
        "uncategorized_channels": orphan_channels,
    }


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="backup-config", description="Export this server's roles + channels + settings as a JSON file.")
    @app_commands.default_permissions(administrator=True)
    async def backup_config(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = _build_snapshot(guild)
        body = json.dumps(snapshot, indent=2, ensure_ascii=False)
        filename = f"backup-{guild.id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
        file = discord.File(io.BytesIO(body.encode("utf-8")), filename=filename)
        summary = (
            f"**Backup created.**\n"
            f"- Roles: {len(snapshot['roles'])}\n"
            f"- Categories: {len(snapshot['categories'])}\n"
            f"- Channels: {sum(len(c['channels']) for c in snapshot['categories']) + len(snapshot['uncategorized_channels'])}\n"
            f"- File: `{filename}` ({len(body) // 1024} KB)"
        )
        await interaction.followup.send(summary, file=file, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Backup(bot))
