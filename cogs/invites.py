"""Invite tracking: remembers who invited whom.

When someone joins, the bot compares invite use counts to find the invite
(and its creator) that was used. Everything is visible to administrators only:

    /invites leaderboard      who brought in the most members (still here / left)
    /invites user @member     who invited them and how many they invited

Needs the Manage Server permission to read invites. State: data/invites.json.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.invites")

STATE_FILE = DATA_DIR / "invites.json"
VANITY = "vanity"


def _enabled() -> bool:
    return SERVER_TEMPLATE.get("invites", {}).get("enabled", True)


class Invites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(STATE_FILE)
        self.cache: dict[int, dict[str, tuple[int, int | None]]] = {}  # guild -> code -> (uses, inviter id)

    def _g(self, guild_id: int) -> dict:
        return self.data.setdefault(str(guild_id), {"inviters": {}, "members": {}})

    def _save(self) -> None:
        save_json(STATE_FILE, self.data)

    async def _snapshot(self, guild: discord.Guild) -> dict[str, tuple[int, int | None]] | None:
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None
        snap = {i.code: (i.uses or 0, i.inviter.id if i.inviter else None) for i in invites}
        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
                if vanity:
                    snap[VANITY] = (vanity.uses or 0, None)
            except (discord.Forbidden, discord.HTTPException):
                pass
        return snap

    @commands.Cog.listener()
    async def on_ready(self):
        if not _enabled():
            return
        for guild in self.bot.guilds:
            snap = await self._snapshot(guild)
            if snap is None:
                log.warning("Invite tracking needs the Manage Server permission in %s", guild.name)
            else:
                self.cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            self.cache.setdefault(invite.guild.id, {})[invite.code] = (invite.uses or 0, invite.inviter.id if invite.inviter else None)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        # Keep deleted invites in the cache: a single-use invite is deleted right when it's used.
        pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not _enabled() or member.bot:
            return
        guild = member.guild
        before = self.cache.get(guild.id, {})
        after = await self._snapshot(guild)
        if after is None:
            return
        used_code, inviter_id = None, None
        for code, (uses, inviter) in after.items():
            if uses > before.get(code, (0, None))[0]:
                used_code, inviter_id = code, inviter
                break
        if used_code is None:
            # An invite that vanished since the last snapshot was probably a single-use one.
            gone = [c for c in before if c not in after]
            if len(gone) == 1:
                used_code, inviter_id = gone[0], before[gone[0]][1]
        self.cache[guild.id] = after

        g = self._g(guild.id)
        g["members"][str(member.id)] = {"inviter": inviter_id, "code": used_code, "ts": time.time()}
        if inviter_id:
            inviter = guild.get_member(inviter_id)
            entry = g["inviters"].setdefault(str(inviter_id), {"name": str(inviter or inviter_id), "joins": 0, "left": 0})
            entry["joins"] += 1
            if inviter:
                entry["name"] = str(inviter)
        self._save()
        log.info("%s joined via %s (inviter %s)", member, used_code or "unknown invite", inviter_id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not _enabled():
            return
        g = self._g(member.guild.id)
        info = g["members"].get(str(member.id))
        if info and info.get("inviter"):
            entry = g["inviters"].get(str(info["inviter"]))
            if entry:
                entry["left"] += 1
                self._save()

    group = app_commands.Group(
        name="invites",
        description="Invite tracking (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @group.command(name="leaderboard", description="Who invited the most members.")
    async def leaderboard(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        inviters = self._g(interaction.guild_id)["inviters"]
        ranked = sorted(inviters.items(), key=lambda kv: kv[1]["joins"] - kv[1]["left"], reverse=True)[:15]
        if not ranked:
            await interaction.response.send_message("No tracked invites yet.", ephemeral=True)
            return
        lines = [
            f"`#{i}` <@{uid}> - **{e['joins'] - e['left']}** still here ({e['joins']} joined, {e['left']} left)"
            for i, (uid, e) in enumerate(ranked, 1)
        ]
        embed = discord.Embed(title="Invite leaderboard", description="\n".join(lines), color=0x5865F2)
        embed.set_footer(text="Only visible to administrators")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="user", description="Who invited a member, and how many they invited.")
    async def user(self, interaction: discord.Interaction, member: discord.User):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        g = self._g(interaction.guild_id)
        info = g["members"].get(str(member.id))
        stats = g["inviters"].get(str(member.id))
        lines = []
        if info:
            who = f"<@{info['inviter']}>" if info.get("inviter") else "unknown"
            lines.append(f"**Invited by:** {who} (code `{info.get('code') or '?'}`, <t:{int(info['ts'])}:R>)")
        else:
            lines.append("**Invited by:** not tracked (joined before tracking started)")
        if stats:
            lines.append(f"**Invited:** {stats['joins']} member(s), {stats['joins'] - stats['left']} still here")
        else:
            lines.append("**Invited:** nobody yet")
        await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Invites(bot))
