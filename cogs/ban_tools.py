"""Ban tools: evidence, ban appeals and alt-account detection.

Evidence    When someone is banned (any way - /ban, /tempban, Discord's menu), their
            last 50 messages are posted as a file in #mod-logs. The bot keeps the
            latest messages of active members in memory for that (never on disk),
            so it works even when the ban deletes their messages.
Appeals     /ban and /tempban DM the member before the ban, with an "Appeal" button:
            a form (what happened / why unban), which goes to the staff chat with
            Unban / Reject. The member can check the status with a button in the DM
            (approved: they get an invite link). One appeal at a time, a new one
            `appeal_cooldown_days` after a rejection.
Alts        When someone joins who looks like a banned user (same profile picture, or
            a very similar name), #mod-logs gets a warning with Ban / Kick /
            Timeout 1 day / Ignore buttons.
Config: SERVER_TEMPLATE["ban_tools"].
"""

import asyncio
import io
import logging
import re
import secrets
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import aiohttp
import discord
from discord.ext import commands
from PIL import Image

from cogs.common import DATA_DIR, feature_on, is_staff, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.ban_tools")

PROFILES_FILE = DATA_DIR / "ban_profiles.json"
APPEALS_FILE = DATA_DIR / "appeals.json"
KEEP_MESSAGES = 50
MAX_TRACKED_USERS = 3000
AVATAR_DISTANCE = 6        # dHash bits that may differ for "the same picture"
NAME_SIMILARITY = 0.88


def _config() -> dict:
    return SERVER_TEMPLATE.get("ban_tools", {})


def _staff_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


async def _mod_log(guild: discord.Guild, **kwargs) -> discord.Message | None:
    ch = discord.utils.get(guild.text_channels, name=CHANNELS["mod_logs"])
    if ch is None:
        return None
    try:
        return await ch.send(**kwargs)
    except discord.HTTPException:
        return None


# -------- Picture + name similarity ------------------------------------------

def dhash(data: bytes) -> int | None:
    """64-bit difference hash: the same picture (also resized/recompressed) gives (almost) the same number."""
    try:
        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
    except Exception:
        return None
    px = list(img.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def norm_name(name: str | None) -> str:
    """'xX_Cool.Guy_123Xx' -> 'coolguy'."""
    n = re.sub(r"[^a-z]", "", (name or "").lower())
    n = re.sub(r"^x+|x+$", "", n) if len(n) > 4 else n
    return n


def name_score(a_names, b_names) -> float:
    best = 0.0
    for a in a_names:
        for b in b_names:
            a2, b2 = norm_name(a), norm_name(b)
            if len(a2) < 3 or len(b2) < 3:
                continue
            best = max(best, 1.0 if a2 == b2 else SequenceMatcher(None, a2, b2).ratio())
    return best


async def avatar_hash(user: discord.abc.User) -> int | None:
    if user.avatar is None:   # default avatars are shared by many people - useless for matching
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(user.avatar.replace(size=64, format="png").url,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                data = await r.read()
    except Exception:
        return None
    return await asyncio.to_thread(dhash, data)


# -------- Buttons ---------------------------------------------------------------

class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"appeal:(?P<what>open|status):(?P<guild>\d+)"):
    """In the ban DM: open the appeal form / check its status. Works after a restart."""

    def __init__(self, what: str, guild_id: int):
        self.what, self.guild_id = what, guild_id
        super().__init__(discord.ui.Button(
            label="Appeal the ban" if what == "open" else "Check appeal status",
            emoji="\U0001F4DD" if what == "open" else "\U0001F50D",
            style=discord.ButtonStyle.primary if what == "open" else discord.ButtonStyle.secondary,
            custom_id=f"appeal:{what}:{guild_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["what"], int(match["guild"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("BanTools")
        if cog is None:
            await interaction.response.send_message("Appeals are turned off right now.", ephemeral=True)
        elif self.what == "open":
            await cog.open_appeal(interaction, self.guild_id)
        else:
            await cog.appeal_status(interaction, self.guild_id)


class AppealDecision(discord.ui.DynamicItem[discord.ui.Button], template=r"appealdec:(?P<action>ok|no):(?P<aid>[0-9a-f]{10})"):
    def __init__(self, action: str, aid: str):
        self.action, self.aid = action, aid
        super().__init__(discord.ui.Button(
            label="Unban" if action == "ok" else "Reject", emoji="✅" if action == "ok" else "❌",
            style=discord.ButtonStyle.success if action == "ok" else discord.ButtonStyle.danger,
            custom_id=f"appealdec:{action}:{aid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["aid"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("BanTools")
        if cog is not None:
            await cog.decide_appeal(interaction, self.aid, self.action == "ok")


class AltAction(discord.ui.DynamicItem[discord.ui.Button], template=r"alt:(?P<action>ban|kick|mute|ok):(?P<user>\d+)"):
    LOOK = {"ban": ("Ban", "\U0001F528", discord.ButtonStyle.danger),
            "kick": ("Kick", "\U0001F462", discord.ButtonStyle.secondary),
            "mute": ("Timeout 1 day", "⏳", discord.ButtonStyle.secondary),
            "ok": ("Ignore", "✅", discord.ButtonStyle.success)}

    def __init__(self, action: str, user_id: int):
        self.action, self.user_id = action, user_id
        label, emoji, style = self.LOOK[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"alt:{action}:{user_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("BanTools")
        if cog is not None:
            await cog.alt_action(interaction, self.action, self.user_id)


class AppealModal(discord.ui.Modal, title="Ban appeal"):
    happened = discord.ui.TextInput(label="What happened?", style=discord.TextStyle.paragraph, max_length=1000)
    why = discord.ui.TextInput(label="Why should we unban you?", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog: "BanTools", guild_id: int):
        super().__init__()
        self.cog, self.guild_id = cog, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit_appeal(interaction, self.guild_id, str(self.happened.value), str(self.why.value))


def appeal_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AppealButton("open", guild_id))
    view.add_item(AppealButton("status", guild_id))
    return view


async def send_ban_notice(guild: discord.Guild, member: discord.abc.User, reason: str, until: float | None = None) -> None:
    """DM before a ban (afterwards we no longer share a server): reason + appeal buttons."""
    text = (f"You were banned from **{guild.name}**" + (f" until <t:{int(until)}:f>" if until else "") + ".\n"
            f"**Reason:** {reason}")
    embed = discord.Embed(title="\U0001F528 You were banned", description=text, color=0xC0392B)
    view = None
    if _config().get("appeals", True):
        embed.add_field(name="Think this was a mistake?",
                        value="Click **Appeal the ban** and tell the team what happened. "
                              "Check the answer here later with **Check appeal status**.", inline=False)
        view = appeal_view(guild.id)
    try:
        await member.send(embed=embed, view=view) if view else await member.send(embed=embed)
    except discord.HTTPException:
        pass


class BanTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recent: OrderedDict[int, deque] = OrderedDict()   # user id -> latest messages (memory only)
        bot.add_dynamic_items(AppealButton, AppealDecision, AltAction)
        self._seeded = False

    # -------- Evidence ------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        q = self.recent.get(message.author.id)
        if q is None:
            q = self.recent[message.author.id] = deque(maxlen=KEEP_MESSAGES)
            if len(self.recent) > MAX_TRACKED_USERS:
                self.recent.popitem(last=False)
        else:
            self.recent.move_to_end(message.author.id)
        q.append((message.created_at, message.guild.id, getattr(message.channel, "name", "?"),
                  message.content or "", [a.url for a in message.attachments]))

    async def _scan_history(self, guild: discord.Guild, user_id: int) -> list:
        """Fallback after a restart: look through the busiest channels."""
        found = []
        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).read_message_history][:12]
        for ch in channels:
            try:
                async for m in ch.history(limit=150):
                    if m.author.id == user_id:
                        found.append((m.created_at, guild.id, ch.name, m.content or "", [a.url for a in m.attachments]))
            except discord.HTTPException:
                continue
        found.sort(key=lambda x: x[0])
        return found[-KEEP_MESSAGES:]

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self._remember_banned(guild, user)
        if not _config().get("evidence", True):
            return
        msgs = [m for m in self.recent.get(user.id, []) if m[1] == guild.id]
        source = "recent messages the bot saw"
        if not msgs:
            msgs = await self._scan_history(guild, user.id)
            source = "found in the busiest channels"
        if not msgs:
            return
        lines = [f"Messages of {user} ({user.id}) - banned {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
        for ts, _g, ch, content, atts in msgs:
            lines.append(f"[{ts:%Y-%m-%d %H:%M:%S}] #{ch}: {content}" + "".join(f"\n    [attachment] {a}" for a in atts))
        embed = discord.Embed(title="\U0001F9FE Evidence: messages of a banned user", color=0x95A5A6,
                              description=f"{user.mention} (`{user}`) - last {len(msgs)} messages ({source}).",
                              timestamp=datetime.now(timezone.utc))
        await _mod_log(guild, embed=embed,
                       file=discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"messages-{user.id}.txt"))
        self.recent.pop(user.id, None)

    # -------- Banned profiles + alt detection --------------------------------

    async def _remember_banned(self, guild: discord.Guild, user: discord.abc.User, reason: str = "") -> None:
        data = load_json(PROFILES_FILE)
        g = data.setdefault(str(guild.id), {})
        g[str(user.id)] = {"name": user.name, "global_name": getattr(user, "global_name", None),
                           "avatar": await avatar_hash(user), "ts": time.time(), "reason": reason}
        save_json(PROFILES_FILE, data)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        data = load_json(PROFILES_FILE)
        if data.get(str(guild.id), {}).pop(str(user.id), None) is not None:
            save_json(PROFILES_FILE, data)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._seeded or not _config().get("alt_detection", True):
            return
        self._seeded = True
        data = load_json(PROFILES_FILE)
        for guild in self.bot.guilds:
            if data.get(str(guild.id)):
                continue
            try:
                count = 0
                async for entry in guild.bans(limit=300):
                    await self._remember_banned(guild, entry.user, entry.reason or "")
                    count += 1
                log.info("Remembered %d banned users of %s for alt detection", count, guild.name)
            except discord.HTTPException:
                log.warning("Can't read the ban list of %s (missing Ban Members permission?)", guild.name)

    def find_alt(self, guild_id: int, names: list[str], avatar: int | None) -> tuple[str, dict, list[str]] | None:
        """(banned user id, profile, reasons) of the best match, or None."""
        best = None
        for uid, p in load_json(PROFILES_FILE).get(str(guild_id), {}).items():
            reasons, score = [], 0.0
            if avatar is not None and p.get("avatar") is not None and hamming(avatar, p["avatar"]) <= AVATAR_DISTANCE:
                reasons.append("same profile picture")
                score += 2
            ns = name_score(names, [p.get("name"), p.get("global_name")])
            if ns >= NAME_SIMILARITY:
                reasons.append("same name" if ns == 1.0 else f"very similar name ({ns:.0%})")
                score += ns
            if reasons and (best is None or score > best[0]):
                best = (score, uid, p, reasons)
        return (best[1], best[2], best[3]) if best else None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not _config().get("alt_detection", True) or not feature_on("alt_detection"):
            return
        avatar = await avatar_hash(member)
        hit = self.find_alt(member.guild.id, [member.name, member.global_name, member.display_name], avatar)
        if hit is None:
            return
        uid, profile, reasons = hit
        age = datetime.now(timezone.utc) - member.created_at
        embed = discord.Embed(title="\U0001F575 Possible alt account of a banned user", color=0xE67E22,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Joined", value=f"{member.mention} (`{member}`, {member.id})", inline=False)
        embed.add_field(name="Looks like", value=f"<@{uid}> (`{profile.get('name')}`) - banned "
                                                 f"<t:{int(profile.get('ts', 0))}:R>"
                                                 + (f"\nBan reason: {profile['reason'][:200]}" if profile.get("reason") else ""),
                        inline=False)
        embed.add_field(name="Why", value=", ".join(reasons), inline=True)
        embed.add_field(name="Account age", value=f"{age.days} days" + (" ⚠" if age.days < 30 else ""), inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        view = discord.ui.View(timeout=None)
        for action in ("ban", "kick", "mute", "ok"):
            view.add_item(AltAction(action, member.id))
        await _mod_log(member.guild, embed=embed, view=view)
        from cogs.push import safe_notify
        safe_notify("alt", "\U0001F575 Possible alt account", f"{member} looks like banned {profile.get('name')}",
                    f"/members?q={member.id}")
        log.info("Possible alt: %s looks like banned %s (%s)", member, profile.get("name"), ", ".join(reasons))

    async def alt_action(self, interaction: discord.Interaction, action: str, user_id: int):
        guild = interaction.guild
        if guild is None or not is_staff(interaction.user, _staff_roles()):
            await interaction.response.send_message("Only the team can do that.", ephemeral=True)
            return
        from cogs.moderation import mod_action_embed, record_action, send_to_mod_logs
        member = guild.get_member(user_id)
        reason = "Alt account of a banned user"
        note = ""
        try:
            if action == "ok":
                note = f"✅ Ignored by {interaction.user.mention}"
            elif member is None:
                await interaction.response.send_message("They already left the server.", ephemeral=True)
                return
            elif action == "ban":
                await send_ban_notice(guild, member, reason)
                await member.ban(reason=f"{reason} (by {interaction.user})")
                record_action(guild.id, member.id, "ban", interaction.user, reason)
                note = f"\U0001F528 Banned by {interaction.user.mention}"
            elif action == "kick":
                await member.kick(reason=f"{reason} (by {interaction.user})")
                record_action(guild.id, member.id, "kick", interaction.user, reason)
                note = f"\U0001F462 Kicked by {interaction.user.mention}"
            else:
                await member.timeout(timedelta(days=1), reason=f"{reason} (by {interaction.user})")
                record_action(guild.id, member.id, "timeout", interaction.user, reason, duration="1d")
                await send_to_mod_logs(guild, mod_action_embed("Member Timed Out", 0xE67E22, member, interaction.user,
                                                               reason, {"Duration": "1 day"}))
                note = f"⏳ Timed out for 1 day by {interaction.user.mention}"
        except discord.HTTPException:
            await interaction.response.send_message("That didn't work - missing permissions?", ephemeral=True)
            return
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed()
        embed.add_field(name="Handled", value=note, inline=False)
        embed.color = 0x95A5A6
        await interaction.response.edit_message(embed=embed, view=None)

    # -------- Appeals ---------------------------------------------------------

    def _appeals(self) -> dict:
        data = load_json(APPEALS_FILE)
        data.setdefault("appeals", {})
        return data

    def _latest(self, data: dict, guild_id: int, user_id: int) -> tuple[str, dict] | None:
        mine = [(aid, a) for aid, a in data["appeals"].items() if a["guild"] == guild_id and a["user"] == user_id]
        return max(mine, key=lambda x: x[1]["ts"]) if mine else None

    async def open_appeal(self, interaction: discord.Interaction, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await interaction.response.send_message("I can't find that server anymore.", ephemeral=True)
            return
        latest = self._latest(self._appeals(), guild_id, interaction.user.id)
        cooldown = float(_config().get("appeal_cooldown_days", 30)) * 86400
        if latest and latest[1]["status"] == "pending":
            await interaction.response.send_message("Your appeal is waiting for the team - check the status later.",
                                                    ephemeral=True)
            return
        if latest and latest[1]["status"] == "rejected" and time.time() - latest[1].get("decided", 0) < cooldown:
            when = int(latest[1]["decided"] + cooldown)
            await interaction.response.send_message(f"Your last appeal was rejected. You can appeal again <t:{when}:R>.",
                                                    ephemeral=True)
            return
        try:
            await guild.fetch_ban(interaction.user)
        except discord.NotFound:
            await interaction.response.send_message("You aren't banned from that server (anymore).", ephemeral=True)
            return
        except discord.HTTPException:
            pass
        await interaction.response.send_modal(AppealModal(self, guild_id))

    async def submit_appeal(self, interaction: discord.Interaction, guild_id: int, happened: str, why: str):
        guild = self.bot.get_guild(guild_id)
        chat = discord.utils.get(guild.text_channels, name=CHANNELS.get("staff_chat")) if guild else None
        if chat is None:
            await interaction.response.send_message("The appeal couldn't be delivered - please try again later.",
                                                    ephemeral=True)
            return
        data = self._appeals()
        aid = secrets.token_hex(5)
        data["appeals"][aid] = {"guild": guild_id, "user": interaction.user.id, "name": str(interaction.user),
                                "ts": time.time(), "status": "pending", "happened": happened, "why": why}
        embed = discord.Embed(title="\U0001F4DD Ban appeal", color=0x5865F2, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="From", value=f"<@{interaction.user.id}> (`{interaction.user}`, {interaction.user.id})",
                        inline=False)
        try:
            ban = await guild.fetch_ban(interaction.user)
            embed.add_field(name="Ban reason", value=(ban.reason or "-")[:1024], inline=False)
        except discord.HTTPException:
            pass
        embed.add_field(name="What happened?", value=happened[:1024], inline=False)
        embed.add_field(name="Why unban?", value=why[:1024], inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(AppealDecision("ok", aid))
        view.add_item(AppealDecision("no", aid))
        try:
            await chat.send(embed=embed, view=view)
        except discord.HTTPException:
            await interaction.response.send_message("The appeal couldn't be delivered - please try again later.",
                                                    ephemeral=True)
            return
        save_json(APPEALS_FILE, data)
        from cogs.push import safe_notify
        safe_notify("appeal", "\U0001F4DD Ban appeal", f"{interaction.user}: {why[:200]}", "/")
        await interaction.response.send_message("✅ Your appeal was sent to the team. Check back here with "
                                                "**Check appeal status**.", ephemeral=True)
        log.info("Ban appeal from %s for %s", interaction.user, guild.name)

    async def _invite(self, guild: discord.Guild) -> str | None:
        channel = (discord.utils.get(guild.text_channels, name=CHANNELS.get("rules"))
                   or discord.utils.get(guild.text_channels, name=CHANNELS.get("welcome")) or guild.text_channels[0])
        try:
            inv = await channel.create_invite(max_uses=1, max_age=7 * 86400, unique=True, reason="Ban appeal approved")
            return inv.url
        except discord.HTTPException:
            return None

    async def decide_appeal(self, interaction: discord.Interaction, aid: str, approve: bool):
        data = self._appeals()
        appeal = data["appeals"].get(aid)
        guild = interaction.guild
        if appeal is None or guild is None:
            await interaction.response.send_message("That appeal doesn't exist anymore.", ephemeral=True)
            return
        perms = interaction.user.guild_permissions
        if not (perms.ban_members or perms.administrator):
            await interaction.response.send_message("You need the Ban Members permission.", ephemeral=True)
            return
        if appeal["status"] != "pending":
            await interaction.response.send_message(f"Already {appeal['status']}.", ephemeral=True)
            return
        from cogs.moderation import record_action
        if approve:
            try:
                await guild.unban(discord.Object(appeal["user"]), reason=f"Appeal approved by {interaction.user}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                await interaction.response.send_message("Unbanning failed - missing permissions?", ephemeral=True)
                return
            appeal["invite"] = await self._invite(guild)
            record_action(guild.id, appeal["user"], "unban", interaction.user, "Ban appeal approved")
        appeal.update(status="approved" if approve else "rejected", decided=time.time(), by=str(interaction.user))
        save_json(APPEALS_FILE, data)
        embed = interaction.message.embeds[0]
        embed.color = 0x57F287 if approve else 0xED4245
        embed.add_field(name="Decision", value=f"{'Unbanned' if approve else 'Rejected'} by {interaction.user.mention}",
                        inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        user = self.bot.get_user(appeal["user"])
        if user is not None:
            try:   # works only if we still share a server - otherwise they see it with "Check appeal status"
                await user.send(self._status_text(guild, appeal))
            except discord.HTTPException:
                pass

    @staticmethod
    def _status_text(guild: discord.Guild, appeal: dict) -> str:
        if appeal["status"] == "pending":
            return f"Your appeal for **{guild.name}** is still waiting for the team."
        if appeal["status"] == "approved":
            invite = f" Join again: {appeal['invite']}" if appeal.get("invite") else ""
            return f"✅ Your appeal for **{guild.name}** was approved - you're unbanned.{invite}"
        return f"❌ Your appeal for **{guild.name}** was rejected."

    async def appeal_status(self, interaction: discord.Interaction, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        latest = self._latest(self._appeals(), guild_id, interaction.user.id)
        if guild is None or latest is None:
            await interaction.response.send_message("You haven't sent an appeal yet - click **Appeal the ban**.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(self._status_text(guild, latest[1]), ephemeral=True)


async def setup(bot):
    await bot.add_cog(BanTools(bot))
