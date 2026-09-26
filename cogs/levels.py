"""Level system.

Members earn 15-25 XP per message, at most once per minute (spam doesn't pay),
only for messages with some text and not in the channels listed in
`no_xp_channels`. Level-ups are announced in CHANNELS["level_ups"] and level
roles (SERVER_TEMPLATE["levels"]["level_roles"]) are handed out automatically.

XP needed from level L to L+1: 5*L^2 + 50*L + 100 (same curve as most level bots).

Voice: members also earn `voice_xp_per_minute` XP for every minute in a voice
channel with at least one other person (not self-muted/deafened, not in the
AFK channel).

    /rank [member]        level, XP and rank
    /profile [member]     profile card image
    /leaderboard          top 10
    /xp set / /xp reset   (administrator)

XP is kept in memory and written to data/levels.json once a minute.
"""

import asyncio
import io
import logging
import random
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.levels")

LEVELS_FILE = DATA_DIR / "levels.json"


def _config() -> dict:
    return SERVER_TEMPLATE.get("levels", {})


def xp_for_next(level: int) -> int:
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    return sum(xp_for_next(lvl) for lvl in range(level))


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_next(level):
        xp -= xp_for_next(level)
        level += 1
    return level


def _progress_bar(current: int, needed: int, width: int = 14) -> str:
    filled = round(width * current / needed) if needed else width
    return "█" * filled + "░" * (width - filled)


def _level_roles() -> list[tuple[int, str]]:
    roles = _config().get("level_roles", {}) or {}
    return sorted(((int(k), v) for k, v in roles.items()), key=lambda x: x[0])


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data: dict = load_json(LEVELS_FILE)
        self.dirty = False
        self.cooldowns: dict[tuple[int, int], float] = {}
        self.flush_loop.start()
        if int(_config().get("voice_xp_per_minute", 0)) > 0:
            self.voice_loop.start()

    def cog_unload(self):
        if self.flush_loop.is_running():
            self.flush_loop.cancel()
        if self.voice_loop.is_running():
            self.voice_loop.cancel()
        self._flush()

    def _flush(self) -> None:
        if self.dirty:
            save_json(LEVELS_FILE, self.data)
            self.dirty = False

    def reload(self) -> None:
        """Re-read the file (used after a backup restore)."""
        self.data = load_json(LEVELS_FILE)
        self.dirty = False

    @tasks.loop(seconds=60)
    async def flush_loop(self):
        self._flush()

    def _entry(self, guild_id: int, user_id: int) -> dict:
        return self.data.setdefault(str(guild_id), {}).setdefault(str(user_id), {"xp": 0, "messages": 0})

    def _ranked(self, guild_id: int) -> list[tuple[str, dict]]:
        users = self.data.get(str(guild_id), {})
        return sorted(users.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)

    async def _apply_level_roles(self, member: discord.Member, level: int) -> None:
        earned = [(lvl, name) for lvl, name in _level_roles() if lvl <= level]
        guild = member.guild
        if not earned:
            keep_names = set()
        elif _config().get("stack_roles"):
            keep_names = {name for _, name in earned}
        else:
            keep_names = {earned[-1][1]}
        all_names = {name for _, name in _level_roles()}
        to_add = [r for r in guild.roles if r.name in keep_names and r not in member.roles]
        to_remove = [r for r in member.roles if r.name in all_names and r.name not in keep_names]
        try:
            if to_add:
                await member.add_roles(*to_add, reason=f"Reached level {level}")
            if to_remove:
                await member.remove_roles(*to_remove, reason=f"Reached level {level}")
        except discord.Forbidden:
            log.warning("Can't manage level roles for %s (role hierarchy)", member)

    async def _announce(self, member: discord.Member, level: int) -> None:
        channel = discord.utils.get(member.guild.text_channels, name=CHANNELS.get("level_ups", ""))
        if channel is None:
            return
        role_line = ""
        for lvl, name in _level_roles():
            if lvl == level:
                role = discord.utils.get(member.guild.roles, name=name)
                role_line = f"\nNew role: {role.mention if role else name}"
        embed = discord.Embed(
            description=f"\U0001F389 {member.mention} reached **Level {level}**!{role_line}",
            color=0xF1C40F,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        cfg = _config()
        if not cfg.get("enabled", True):
            return
        if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
            return
        if message.channel.name in set(cfg.get("no_xp_channels", [])):
            return
        if len((message.content or "").strip()) < int(cfg.get("min_message_length", 3)) and not message.attachments:
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.cooldowns.get(key, 0) < float(cfg.get("cooldown_seconds", 60)):
            return
        self.cooldowns[key] = now
        if len(self.cooldowns) > 5000:
            cutoff = now - float(cfg.get("cooldown_seconds", 60))
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if v > cutoff}

        entry = self._entry(message.guild.id, message.author.id)
        entry["messages"] = entry.get("messages", 0) + 1
        await self.add_xp(message.author, random.randint(int(cfg.get("xp_min", 15)), int(cfg.get("xp_max", 25))))

    async def add_xp(self, member: discord.Member, amount: int) -> None:
        entry = self._entry(member.guild.id, member.id)
        old_level = level_from_xp(entry["xp"])
        entry["xp"] += amount
        entry["name"] = str(member)
        self.dirty = True
        new_level = level_from_xp(entry["xp"])
        if new_level > old_level:
            log.info("%s reached level %d", member, new_level)
            await self._apply_level_roles(member, new_level)
            await self._announce(member, new_level)

    @tasks.loop(seconds=60)
    async def voice_loop(self):
        if not _config().get("enabled", True):
            return
        per_minute = int(_config().get("voice_xp_per_minute", 0))
        hub = CHANNELS.get("create_vc")
        for guild in self.bot.guilds:
            for channel in list(guild.voice_channels) + list(guild.stage_channels):
                if channel == guild.afk_channel or channel.name == hub:
                    continue
                humans = [m for m in channel.members if not m.bot]
                if len(humans) < 2:
                    continue
                for member in humans:
                    vs = member.voice
                    if vs is None or vs.self_mute or vs.self_deaf or vs.mute or vs.deaf:
                        continue
                    entry = self._entry(guild.id, member.id)
                    entry["voice_minutes"] = entry.get("voice_minutes", 0) + 1
                    await self.add_xp(member, per_minute)

    @voice_loop.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

    # -------- Commands ----------------------------------------------------

    @app_commands.command(name="rank", description="Show your level (or someone else's).")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        entry = self.data.get(str(interaction.guild_id), {}).get(str(member.id))
        if not entry:
            await interaction.response.send_message(f"{member.mention} has no XP yet.", ephemeral=True)
            return
        xp = entry.get("xp", 0)
        level = level_from_xp(xp)
        into = xp - total_xp_for_level(level)
        needed = xp_for_next(level)
        position = next((i for i, (uid, _) in enumerate(self._ranked(interaction.guild_id), 1) if uid == str(member.id)), None)
        embed = discord.Embed(title=f"Rank of {member.display_name}", color=member.color if member.color.value else 0xF1C40F)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Rank", value=f"**#{position}**" if position else "-", inline=True)
        embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
        embed.add_field(name=f"Progress to level {level + 1}",
                        value=f"`{_progress_bar(into, needed)}` {into:,} / {needed:,} XP", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="profile", description="Show a profile card (yours or someone else's).")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        from cogs.profile_card import render_profile
        member = member or interaction.user
        await interaction.response.defer(thinking=True)
        entry = self.data.get(str(interaction.guild_id), {}).get(str(member.id), {})
        xp = entry.get("xp", 0)
        level = level_from_xp(xp)
        position = next((i for i, (uid, _) in enumerate(self._ranked(interaction.guild_id), 1)
                         if uid == str(member.id)), None) if entry else None
        badges = [(r.name, r.color.value) for r in reversed(member.roles[1:]) if r.hoist or r.name.startswith("Level ")][:3]
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except (discord.HTTPException, ValueError):
            avatar = None
        joined = member.joined_at.strftime("%d %b %Y") if member.joined_at else "-"
        accent = member.color.value if member.color.value else 0x5865F2
        png = await asyncio.to_thread(
            render_profile, avatar, member.display_name, member.name, accent, level,
            xp - total_xp_for_level(level), xp_for_next(level), position, xp,
            entry.get("messages", 0), entry.get("voice_minutes", 0), joined, badges,
        )
        await interaction.followup.send(file=discord.File(io.BytesIO(png), filename="profile.png"))

    @app_commands.command(name="leaderboard", description="Show the top 10 members by XP.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        ranked = self._ranked(interaction.guild_id)[:10]
        if not ranked:
            await interaction.response.send_message("Nobody has XP yet.", ephemeral=True)
            return
        medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
        lines = []
        for i, (uid, entry) in enumerate(ranked):
            prefix = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{prefix} <@{uid}> - Level **{level_from_xp(entry.get('xp', 0))}** ({entry.get('xp', 0):,} XP)")
        embed = discord.Embed(title="\U0001F3C6 Leaderboard", description="\n".join(lines), color=0xF1C40F,
                              timestamp=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    xp_group = app_commands.Group(
        name="xp",
        description="Manage member XP (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @xp_group.command(name="set", description="Set a member's level.")
    @app_commands.describe(member="Member", level="New level (0-500)")
    async def xp_set(self, interaction: discord.Interaction, member: discord.Member,
                     level: app_commands.Range[int, 0, 500]):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        entry = self._entry(interaction.guild_id, member.id)
        entry["xp"] = total_xp_for_level(level)
        entry["name"] = str(member)
        self.dirty = True
        self._flush()
        await self._apply_level_roles(member, level)
        await interaction.response.send_message(f"{member.mention} is now level **{level}**.", ephemeral=True)

    @xp_group.command(name="reset", description="Reset a member's XP to 0.")
    async def xp_reset(self, interaction: discord.Interaction, member: discord.Member):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        self.data.get(str(interaction.guild_id), {}).pop(str(member.id), None)
        self.dirty = True
        self._flush()
        names = {name for _, name in _level_roles()}
        roles = [r for r in member.roles if r.name in names]
        if roles:
            try:
                await member.remove_roles(*roles, reason="XP reset")
            except discord.Forbidden:
                pass
        await interaction.response.send_message(f"XP of {member.mention} was reset.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Levels(bot))
