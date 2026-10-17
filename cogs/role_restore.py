"""Role restore: members who leave and come back get their old roles (and nickname) back.

When someone leaves, their roles are remembered in data/role_restore.json (for
keep_days). On rejoin the bot gives back every role that still exists and that it
is allowed to give. Roles with moderator/admin permissions are NOT given back
automatically (a hacked account shouldn't get staff rights back by rejoining) -
unless restore_staff_roles is switched on. Bot-managed roles (boosters,
integrations) are skipped.

Config: SERVER_TEMPLATE["role_restore"]. Feature switch: "role_restore".
"""

import logging
import time

import discord
from discord.ext import commands

from cogs.common import DATA_DIR, feature_on, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.role_restore")

STATE_FILE = DATA_DIR / "role_restore.json"

STAFF_PERMISSIONS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members",
                     "kick_members", "moderate_members", "manage_messages", "manage_webhooks", "mention_everyone")


def _config() -> dict:
    return SERVER_TEMPLATE.get("role_restore", {})


def is_staff_role(role: discord.Role) -> bool:
    perms = role.permissions
    return any(getattr(perms, name, False) for name in STAFF_PERMISSIONS)


def restorable(role: discord.Role, me: discord.Member) -> bool:
    if role.is_default() or role.managed or role >= me.top_role:
        return False
    if role.name in set(_config().get("never_restore", [])):
        return False
    if is_staff_role(role) and not _config().get("restore_staff_roles", False):
        return False
    return True


def prune(data: dict, now: float | None = None) -> int:
    """Forget members who left more than keep_days ago. Returns how many were removed."""
    now = time.time() if now is None else now
    keep = float(_config().get("keep_days", 180)) * 86400
    removed = 0
    for users in data.values():
        for uid in [u for u, e in users.items() if now - e.get("left_at", now) > keep]:
            del users[uid]
            removed += 1
    return removed


class RoleRestore(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or not _config().get("enabled", True):
            return
        roles = [r.id for r in member.roles if not r.is_default() and not r.managed]
        data = load_json(STATE_FILE)
        users = data.setdefault(str(member.guild.id), {})
        if not roles and not member.nick:
            if users.pop(str(member.id), None) is not None:
                save_json(STATE_FILE, data)
            return
        users[str(member.id)] = {"roles": roles, "nick": member.nick, "left_at": time.time()}
        prune(data)
        save_json(STATE_FILE, data)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not _config().get("enabled", True) or not feature_on("role_restore"):
            return
        data = load_json(STATE_FILE)
        entry = data.get(str(member.guild.id), {}).pop(str(member.id), None)
        if entry is None:
            return
        save_json(STATE_FILE, data)
        me = member.guild.me
        roles = [r for rid in entry.get("roles", []) if (r := member.guild.get_role(rid)) and restorable(r, me)]
        roles = [r for r in roles if r not in member.roles]
        given = []
        if roles:
            try:
                await member.add_roles(*roles, reason="Role restore: rejoined the server")
                given = roles
            except discord.HTTPException:
                log.warning("Could not restore roles for %s", member)
        if entry.get("nick") and _config().get("restore_nickname", True):
            try:
                await member.edit(nick=entry["nick"], reason="Role restore: rejoined the server")
            except discord.HTTPException:
                pass
        if given:
            from cogs.logging_cog import _get_log_channel, _safe_send
            embed = discord.Embed(title="Roles restored", color=0x2ECC71,
                                  description=f"{member.mention} came back and got their old roles again.")
            embed.add_field(name="Roles", value=", ".join(r.mention for r in given[:25]), inline=False)
            embed.set_footer(text=f"Left {int((time.time() - entry.get('left_at', time.time())) // 86400)} days ago")
            await _safe_send(_get_log_channel(member.guild, "member"), embed=embed)
            log.info("Restored %d roles for %s", len(given), member)


async def setup(bot):
    await bot.add_cog(RoleRestore(bot))
