"""Utility slash commands: userinfo, serverinfo, avatar, roleinfo, membercount, ping."""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("setup-bot.utility")


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return f"{discord.utils.format_dt(dt, style='F')} ({discord.utils.format_dt(dt, style='R')})"


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Show the bot's gateway latency.")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000, 1)
        await interaction.response.send_message(f"Pong! Gateway latency: **{latency_ms} ms**", ephemeral=True)

    @app_commands.command(name="userinfo", description="Show information about a member.")
    @app_commands.describe(member="The member to look up (defaults to you)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return
        embed = discord.Embed(
            title=str(member),
            color=member.color if member.color.value else 0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Status", value=str(member.status).title() if member.status else "—", inline=True)
        embed.add_field(name="Joined server", value=_fmt_dt(member.joined_at), inline=False)
        embed.add_field(name="Account created", value=_fmt_dt(member.created_at), inline=False)
        roles = [r.mention for r in member.roles[1:]]  # skip @everyone
        if roles:
            value = " ".join(roles[:30])
            if len(roles) > 30:
                value += f" … (+{len(roles) - 30} more)"
            embed.add_field(name=f"Roles ({len(roles)})", value=value, inline=False)
        if member.premium_since:
            embed.add_field(name="Boosting since", value=_fmt_dt(member.premium_since), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="avatar", description="Show a member's avatar full-size.")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"{member}'s avatar", color=member.color if member.color.value else 0x5865F2)
        embed.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Show information about this server.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        text_count = len(guild.text_channels)
        voice_count = len(guild.voice_channels)
        category_count = len(guild.categories)
        forum_count = len(guild.forums) if hasattr(guild, "forums") else 0
        role_count = len(guild.roles) - 1
        bots = sum(1 for m in guild.members if m.bot) if guild.chunked else "?"
        humans = (guild.member_count - bots) if isinstance(bots, int) else "?"
        embed = discord.Embed(
            title=guild.name,
            description=guild.description or "—",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="Created", value=_fmt_dt(guild.created_at), inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        if isinstance(bots, int):
            embed.add_field(name="Humans / Bots", value=f"{humans} / {bots}", inline=True)
        embed.add_field(name="Boosts", value=f"Level {guild.premium_tier} ({guild.premium_subscription_count or 0})", inline=True)
        embed.add_field(name="Verification", value=str(guild.verification_level).title(), inline=True)
        embed.add_field(name="Text channels", value=str(text_count), inline=True)
        embed.add_field(name="Voice channels", value=str(voice_count), inline=True)
        embed.add_field(name="Categories", value=str(category_count), inline=True)
        if forum_count:
            embed.add_field(name="Forums", value=str(forum_count), inline=True)
        embed.add_field(name="Roles", value=str(role_count), inline=True)
        embed.add_field(name="Emojis", value=str(len(guild.emojis)), inline=True)
        features = ", ".join(sorted(guild.features)[:6]) or "—"
        embed.add_field(name="Features", value=features, inline=False)
        embed.set_footer(text=f"Guild ID: {guild.id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="roleinfo", description="Show information about a role.")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        embed = discord.Embed(
            title=role.name,
            color=role.color if role.color.value else 0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Color", value=f"#{role.color.value:06X}" if role.color.value else "default", inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(name="Managed", value="Yes (bot/integration)" if role.managed else "No", inline=True)
        embed.add_field(name="Position", value=str(role.position), inline=True)
        embed.add_field(name="Created", value=_fmt_dt(role.created_at), inline=False)
        if interaction.guild and interaction.guild.chunked:
            member_count = sum(1 for m in interaction.guild.members if role in m.roles)
            embed.add_field(name="Members with role", value=str(member_count), inline=True)
        perms = [p.replace("_", " ").title() for p, v in role.permissions if v]
        if perms:
            value = ", ".join(perms[:20])
            if len(perms) > 20:
                value += f" … (+{len(perms) - 20})"
            embed.add_field(name=f"Permissions ({len(perms)})", value=value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="membercount", description="Show the current member count.")
    async def membercount(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{guild.name}** has **{guild.member_count}** members.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Utility(bot))
