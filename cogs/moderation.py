"""Moderation slash commands: timeout, ban, tempban, unban, kick, purge, warn,
warnings, modhistory.

Every action done through the bot is recorded in data/modlog.json (max. 50 per
member) so /modhistory can show warnings, timeouts, kicks and bans together.
Temporary bans live in data/tempbans.json and are lifted automatically, also
after a restart.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import format_duration, has_perm, load_json, parse_duration, save_json
from server_template import CHANNELS

log = logging.getLogger("setup-bot.moderation")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
WARNINGS_FILE = DATA_DIR / "warnings.json"
MODLOG_FILE = DATA_DIR / "modlog.json"
TEMPBANS_FILE = DATA_DIR / "tempbans.json"
MAX_ACTIONS_PER_USER = 50

ACTION_ICONS = {
    "warn": "\u26A0\uFE0F", "timeout": "\u23F3", "untimeout": "\u2705", "kick": "\U0001F462",
    "ban": "\U0001F528", "tempban": "\u231B", "unban": "\U0001F513", "clearwarnings": "\U0001F9F9",
}


def record_action(guild_id: int, user_id: int, action: str, moderator, reason: str, **extra) -> None:
    """Store a moderation action for /modhistory."""
    data = load_json(MODLOG_FILE)
    entries = data.setdefault(str(guild_id), {}).setdefault(str(user_id), [])
    entry = {
        "action": action,
        "reason": reason,
        "moderator_id": getattr(moderator, "id", None),
        "moderator_name": str(moderator),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    entry.update(extra)
    entries.append(entry)
    del entries[:-MAX_ACTIONS_PER_USER]
    save_json(MODLOG_FILE, data)


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


def _remove_tempban(guild_id: int, user_id: int) -> None:
    data = load_json(TEMPBANS_FILE)
    if data.get(str(guild_id), {}).pop(str(user_id), None) is not None:
        save_json(TEMPBANS_FILE, data)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tempban_loop.start()

    def cog_unload(self):
        if self.tempban_loop.is_running():
            self.tempban_loop.cancel()

    # -------- Temp bans ---------------------------------------------------

    @app_commands.command(name="tempban", description="Ban a member for a limited time - they're unbanned automatically.")
    @app_commands.describe(member="Member to ban", duration="e.g. 1h, 12h, 7d, 2w", reason="Reason (shown to them and in the audit log)",
                           delete_message_days="Days of recent messages to delete (0-7)")
    @app_commands.default_permissions(ban_members=True)
    async def tempban(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: str = "No reason provided", delete_message_days: app_commands.Range[int, 0, 7] = 0):
        if not has_perm(interaction.user, "ban_members"):
            await interaction.response.send_message("You need Ban Members permission.", ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta < timedelta(minutes=1):
            await interaction.response.send_message("Invalid duration. Use forms like 1h, 12h, 7d, 2w (at least 1 minute).", ephemeral=True)
            return
        if delta > timedelta(days=365):
            await interaction.response.send_message("Max temp-ban is 365 days. Use /ban for longer.", ephemeral=True)
            return
        until = time.time() + delta.total_seconds()
        pretty = format_duration(delta)
        # DM first - once banned we may no longer share a server with them.
        try:
            await member.send(
                f"You were banned from **{interaction.guild.name}** for **{pretty}** "
                f"(until <t:{int(until)}:f>).\nReason: {reason}"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await member.ban(reason=f"Tempban {pretty} by {interaction.user}: {reason}",
                             delete_message_days=delete_message_days)
        except discord.Forbidden:
            await interaction.response.send_message("I can't ban that member.", ephemeral=True)
            return
        data = load_json(TEMPBANS_FILE)
        data.setdefault(str(interaction.guild.id), {})[str(member.id)] = {
            "until": until,
            "reason": reason,
            "user_name": str(member),
            "moderator_name": str(interaction.user),
        }
        save_json(TEMPBANS_FILE, data)
        record_action(interaction.guild.id, member.id, "tempban", interaction.user, reason, duration=pretty, until=until)
        await interaction.response.send_message(
            f"Banned {member} for **{pretty}** (unban <t:{int(until)}:R>).\nReason: {reason}", ephemeral=True
        )
        embed = mod_action_embed("Member Temp-Banned", 0xC0392B, member, interaction.user, reason,
                                 {"Duration": pretty, "Unban": f"<t:{int(until)}:R>"})
        await send_to_mod_logs(interaction.guild, embed)

    @tasks.loop(seconds=60)
    async def tempban_loop(self):
        data = load_json(TEMPBANS_FILE)
        now = time.time()
        changed = False
        for guild_id, bans in data.items():
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            for user_id, info in list(bans.items()):
                if info.get("until", 0) > now:
                    continue
                try:
                    await guild.unban(discord.Object(id=int(user_id)), reason="Temp-ban expired")
                    log.info("Temp-ban expired, unbanned %s in %s", info.get("user_name", user_id), guild.name)
                    embed = discord.Embed(title="Temp-Ban Expired", color=0x3498DB, timestamp=datetime.now(timezone.utc))
                    embed.add_field(name="User", value=f"<@{user_id}> ({info.get('user_name', user_id)})", inline=False)
                    embed.add_field(name="Original reason", value=info.get("reason") or "*(none)*", inline=False)
                    await send_to_mod_logs(guild, embed)
                    record_action(guild.id, int(user_id), "unban", self.bot.user, "Temp-ban expired")
                except discord.NotFound:
                    pass  # already unbanned by hand
                except discord.Forbidden:
                    log.warning("Missing permission to lift temp-ban for %s", user_id)
                    continue  # keep it and retry later
                except Exception:
                    log.exception("Failed to lift temp-ban for %s", user_id)
                    continue
                del bans[user_id]
                changed = True
        if changed:
            save_json(TEMPBANS_FILE, data)

    @tempban_loop.before_loop
    async def _before_tempban(self):
        await self.bot.wait_until_ready()

    # -------- History -----------------------------------------------------

    @app_commands.command(name="modhistory", description="Show all warnings, timeouts, kicks and bans of a user.")
    @app_commands.describe(user="The user (also works for users who already left or are banned)")
    @app_commands.default_permissions(moderate_members=True)
    async def modhistory(self, interaction: discord.Interaction, user: discord.User):
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("You need Moderate Members permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        g, u = str(guild.id), str(user.id)

        entries = []
        for w in load_warnings().get(g, {}).get(u, []):
            entries.append({"action": "warn", "reason": w.get("reason", ""), "moderator_name": w.get("moderator_name", "?"),
                            "timestamp": w.get("timestamp", ""), "id": w.get("id")})
        entries.extend(load_json(MODLOG_FILE).get(g, {}).get(u, []))
        entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

        counts = {}
        for e in entries:
            counts[e["action"]] = counts.get(e["action"], 0) + 1

        status = []
        member = guild.get_member(user.id)
        if member is None:
            status.append("Not on the server")
        elif member.is_timed_out():
            status.append(f"Timed out until <t:{int(member.timed_out_until.timestamp())}:f>")
        else:
            status.append("On the server")
        tempban = load_json(TEMPBANS_FILE).get(g, {}).get(u)
        if tempban:
            status.append(f"Temp-banned until <t:{int(tempban['until'])}:f>")
        elif member is None:
            try:
                await guild.fetch_ban(user)
                status.append("Banned")
            except (discord.NotFound, discord.Forbidden):
                pass

        embed = discord.Embed(title=f"Moderation history: {user}", color=0xF39C12, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Status", value="\n".join(status), inline=False)
        summary = " \u00b7 ".join(
            f"{ACTION_ICONS.get(k, '')} {k}: **{v}**" for k, v in sorted(counts.items())
        ) or "No entries - clean record."
        embed.add_field(name="Summary", value=summary[:1024], inline=False)
        for e in entries[:15]:
            ts = e.get("timestamp", "")
            try:
                when = f"<t:{int(datetime.fromisoformat(ts).timestamp())}:d>"
            except ValueError:
                when = ts[:10]
            extra = f" ({e['duration']})" if e.get("duration") else ""
            embed.add_field(
                name=f"{ACTION_ICONS.get(e['action'], '')} {e['action']}{extra} - {when}"[:256],
                value=f"By **{e.get('moderator_name', '?')}**: {e.get('reason') or '*(no reason)*'}"[:1024],
                inline=False,
            )
        if len(entries) > 15:
            embed.set_footer(text=f"Showing the latest 15 of {len(entries)} entries")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="timeout", description="Time out a member (max 28 days).")
    @app_commands.describe(member="Member to time out", duration="e.g. 10m, 1h30m, 2d, 1w", reason="Reason shown in the audit log")
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("You need the Moderate Members permission.", ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta.total_seconds() < 1:
            await interaction.response.send_message("Invalid duration. Use forms like 10m, 1h30m, 2d, 1w.", ephemeral=True)
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
        record_action(interaction.guild.id, member.id, "timeout", interaction.user, reason, duration=format_duration(delta))
        embed = mod_action_embed("Member Timed Out", 0xF1C40F, member, interaction.user, reason, {"Duration": duration})
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="untimeout", description="Remove an active timeout from a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Manual removal"):
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("You need the Moderate Members permission.", ephemeral=True)
            return
        try:
            await member.timeout(None, reason=f"By {interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't modify that member.", ephemeral=True)
            return
        await interaction.response.send_message(f"Removed timeout from {member.mention}.", ephemeral=True)
        record_action(interaction.guild.id, member.id, "untimeout", interaction.user, reason)
        embed = mod_action_embed("Timeout Removed", 0x3498DB, member, interaction.user, reason)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not has_perm(interaction.user, "kick_members"):
            await interaction.response.send_message("You need Kick Members permission.", ephemeral=True)
            return
        try:
            await member.kick(reason=f"By {interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't kick that member.", ephemeral=True)
            return
        await interaction.response.send_message(f"Kicked {member}.\nReason: {reason}", ephemeral=True)
        record_action(interaction.guild.id, member.id, "kick", interaction.user, reason)
        embed = mod_action_embed("Member Kicked", 0xE67E22, member, interaction.user, reason)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="ban", description="Ban a member.")
    @app_commands.describe(member="Member to ban", reason="Audit log reason", delete_message_days="Days of recent messages to delete (0-7)")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_message_days: int = 0):
        if not has_perm(interaction.user, "ban_members"):
            await interaction.response.send_message("You need Ban Members permission.", ephemeral=True)
            return
        delete_message_days = max(0, min(7, delete_message_days))
        try:
            await member.ban(reason=f"By {interaction.user}: {reason}", delete_message_days=delete_message_days)
        except discord.Forbidden:
            await interaction.response.send_message("I can't ban that member.", ephemeral=True)
            return
        await interaction.response.send_message(f"Banned {member}.\nReason: {reason}", ephemeral=True)
        record_action(interaction.guild.id, member.id, "ban", interaction.user, reason)
        _remove_tempban(interaction.guild.id, member.id)  # a permanent ban replaces a temporary one
        embed = mod_action_embed("Member Banned", 0xC0392B, member, interaction.user, reason, {"Messages deleted (days)": str(delete_message_days)})
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "Manual unban"):
        if not has_perm(interaction.user, "ban_members"):
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
        record_action(interaction.guild.id, uid, "unban", interaction.user, reason)
        _remove_tempban(interaction.guild.id, uid)
        embed = discord.Embed(title="Member Unbanned", color=0x3498DB, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User ID", value=str(uid), inline=False)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        await send_to_mod_logs(interaction.guild, embed)

    @app_commands.command(name="purge", description="Delete the last N messages in this channel (1-100).")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100], member: discord.Member | None = None):
        if not has_perm(interaction.user, "manage_messages"):
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
        if not has_perm(interaction.user, "moderate_members"):
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
        if not has_perm(interaction.user, "moderate_members"):
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
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("You need Moderate Members permission.", ephemeral=True)
            return
        data = load_warnings()
        g, u = str(interaction.guild.id), str(member.id)
        removed = len(data.get(g, {}).get(u, []))
        if removed and g in data and u in data[g]:
            del data[g][u]
            save_warnings(data)
            record_action(interaction.guild.id, member.id, "clearwarnings", interaction.user, f"Cleared {removed} warning(s)")
        await interaction.response.send_message(f"Cleared {removed} warning(s) for {member.mention}.", ephemeral=True)
        if removed:
            embed = mod_action_embed("Warnings Cleared", 0x3498DB, member, interaction.user, f"Cleared {removed} warning(s)")
            await send_to_mod_logs(interaction.guild, embed)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
