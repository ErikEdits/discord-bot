"""Link filter: removes invites to other Discord servers, 18+ content and scam links.

- Invites (discord.gg/..., discord.com/invite/..., dsc.gg/...) are removed unless
  they point to this server. The team gets a DM with "Approve" / "Reject": approved
  invites are posted again by the bot ("shared by @user, approved by @mod").
- Invites to 18+ servers (Discord's own age rating of the server, or its name /
  description: words like "nsfw", "onlyfans", emojis like 💦🍑🔞 ...) are removed
  without an approval request, and the poster is flagged as "possibly compromised"
  (such links are mostly sent by hacked accounts): a marker role + a DM with tips to
  secure the account + an entry in /modhistory. No timeout, no ban. /unflag removes it.
- Links to adult websites are always removed.
- Scam links: domains in `blocked_domains`, lookalikes of well-known sites
  (e.g. "dlscord-nitro.ru", "modrlnth.com", "steamcommunlty.com") and the
  classic "free nitro" bait with a link.
- Domains in `allowed_domains` (and their subdomains) are never touched.

Staff (Manage Messages) and bots are exempt. Every removal is logged to
#mod-logs. Config: SERVER_TEMPLATE["link_filter"]. Pause with /maintenance linkfilter.
Needs the Message Content intent.
"""

import logging
import re
import secrets
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, is_staff, load_json, mark_bot_delete, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.linkfilter")

INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|invite\.gg|discord\.me|dis\.gd)/([\w-]+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://([^\s/<>\"'?#]+)", re.IGNORECASE)

# Sites scammers like to imitate. Typos of these (same length +-2, very similar) are blocked.
# Very short names (steam, twitch) are left out on purpose: too many real sites look
# similar ("stream", "switch").
PROTECTED_BRANDS = [
    "discord", "discordapp", "steamcommunity", "steampowered", "minecraft",
    "modrinth", "curseforge", "github", "youtube", "epicgames",
]
# A domain containing one of these brands AND one of the bait words is a scam
# (e.g. "discord-gift.com", "steam-nitro-free.ru").
BAIT_BRANDS = ["discord", "dlscord", "steam", "nitro"]
BAIT_WORDS = ["gift", "nitro", "free", "promo", "claim", "airdrop", "drop", "reward", "give", "bonus"]
BAIT_TEXT = re.compile(
    r"(?i)(free\s+nitro|nitro\s+(?:for\s+)?free|gift(?:ed)?\s+nitro|nitro\s+gift|steam\s+gift|free\s+skins?|"
    r"airdrop|claim\s+your\s+(?:gift|nitro|reward))"
)
_TWO_PART_TLDS = {"co", "com", "org", "net", "gov", "ac", "edu"}
_INVITE_CACHE_SECONDS = 3600

REQUESTS_FILE = DATA_DIR / "invite_requests.json"
FLAGS_FILE = DATA_DIR / "flagged_accounts.json"
REQUEST_DAYS = 7

# -------- 18+ detection (no image AI - names, emojis and Discord's own rating) --------

# Clearly sexual / 18+ words in a server name or description.
ADULT_STRONG = re.compile(
    r"(?i)(?<![a-z])(nsfw|n\$fw|18\s*\+|\+\s*18|porn\w*|p0rn|pr0n|nudes?|nudity|naked|sex|s3x|sexy|sexting|xxx|"
    r"onlyfans|only\s*fans|fansly|hentai|rule\s*34|r34|horny|h0rny|lewd|thots?|e-?girls?\s*(?:leaks?|nsfw)|"
    r"erotic\w*|hookups?|cum|boobs?|tits|titties|pussy|dicks?|cock|milf|camgirls?|nsfw\w*|fap\w*|"
    r"leaks?\s*(?:18|nsfw|nudes?|onlyfans|of)|(?:of|onlyfans)\s*leaks?)(?![a-z])"
)
# Harmless alone, suspicious together with an adult emoji ("ava's playhouse 💦").
ADULT_WEAK = re.compile(
    r"(?i)(?<![a-z])(playhouse|leaks?|baddies|e-?girls?|girls?|spicy|kinky|daddy|mommy|dating|"
    r"femboys?|tease|teens?)(?![a-z])"
)
# Only emojis that are almost always meant sexually in a server name: 🔞 💦 🍑 🍆 👅 🫦 🥵
ADULT_EMOJIS = ("\U0001F51E", "\U0001F4A6", "\U0001F351", "\U0001F346", "\U0001F445", "\U0001FAE6", "\U0001F975")
# Registered domain names of adult sites, and words that only adult sites use in their name.
ADULT_DOMAINS = {
    "pornhub", "xvideos", "xnxx", "xhamster", "redtube", "youporn", "tube8", "spankbang", "eporner",
    "onlyfans", "fansly", "chaturbate", "stripchat", "bongacams", "livejasmin", "camsoda", "myfreecams",
    "brazzers", "realitykings", "rule34", "nhentai", "hanime", "hentaihaven", "erome", "redgifs",
    "motherless", "thothub", "fapello", "coomer", "leakedmodels", "nudostar", "simpcity", "porntrex",
    "hqporner", "beeg", "tnaflix", "pornpics", "sex", "xxx", "manyvids", "clips4sale", "fikfap",
}
ADULT_DOMAIN_WORDS = ("porn", "hentai", "xvideo", "xhamster", "onlyfans", "fansly", "nsfw", "rule34",
                      "camgirl", "nudes", "leakedmodel", "thothub")
ADULT_TLDS = (".xxx", ".porn", ".sex", ".adult", ".sexy")


def adult_text_reason(text: str) -> str | None:
    """Why a server name/description looks 18+, or None."""
    if not text:
        return None
    m = ADULT_STRONG.search(text)
    if m:
        return f"18+ word \"{m.group(0).strip()}\""
    emojis = [e for e in ADULT_EMOJIS if e in text]
    if "\U0001F51E" in emojis:
        return "18+ emoji \U0001F51E"
    weak = ADULT_WEAK.search(text)
    if emojis and weak:
        return f"\"{weak.group(0)}\" together with {''.join(emojis)}"
    if len(emojis) >= 2:
        return f"adult emojis {''.join(emojis)}"
    return None


def adult_domain(host: str) -> bool:
    host = host.lower().split(":")[0].rstrip(".")
    if host.endswith(ADULT_TLDS):
        return True
    label = _registered_label(host)
    if label in ADULT_DOMAINS:
        return True
    plain = label.replace("-", "")
    if any(w in plain for w in ADULT_DOMAIN_WORDS):
        return True
    return "sex" in re.split(r"[-\d]", label)


def _config() -> dict:
    return SERVER_TEMPLATE.get("link_filter", {})


def _host_matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _registered_label(host: str) -> str:
    """'free.discord-gift.co.uk' -> 'discord-gift'."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _TWO_PART_TLDS and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def scam_reason(host: str, message_text: str, allowed, blocked) -> str | None:
    """Return why a host is considered a scam, or None if it looks fine."""
    host = host.lower().split(":")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if _host_matches(host, allowed):
        return None
    if _host_matches(host, blocked):
        return "blocked domain"
    label = _registered_label(host)
    plain = label.replace("-", "")
    for brand in PROTECTED_BRANDS:
        if plain != brand and abs(len(plain) - len(brand)) <= 2 and SequenceMatcher(None, plain, brand).ratio() >= 0.8:
            return f"looks like a fake {brand} site"
    if any(b in plain for b in BAIT_BRANDS) and any(w in plain for w in BAIT_WORDS):
        return "fake gift/nitro site"
    if BAIT_TEXT.search(message_text or ""):
        return "free-nitro/gift bait"
    return None


class InviteDecision(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"invreq:(?P<action>ok|no):(?P<rid>[0-9a-f]{12})"):
    """Approve / Reject under an invite approval request (works after a restart)."""

    def __init__(self, action: str, rid: str):
        self.action, self.rid = action, rid
        super().__init__(discord.ui.Button(
            label="Approve" if action == "ok" else "Reject",
            emoji="✅" if action == "ok" else "❌",
            style=discord.ButtonStyle.success if action == "ok" else discord.ButtonStyle.danger,
            custom_id=f"invreq:{action}:{rid}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["rid"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("LinkFilter")
        if cog is not None:
            await cog.decide(interaction, self.rid, self.action == "ok")


class Unflag(discord.ui.DynamicItem[discord.ui.Button], template=r"unflag:(?P<guild>\d+):(?P<user>\d+)"):
    """"Remove flag" under the mod-log entry of a flagged account."""

    def __init__(self, guild_id: int, user_id: int):
        self.guild_id, self.user_id = guild_id, user_id
        super().__init__(discord.ui.Button(label="Remove flag", emoji="✅", style=discord.ButtonStyle.secondary,
                                           custom_id=f"unflag:{guild_id}:{user_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("LinkFilter")
        if cog is None or interaction.guild is None:
            return
        if not is_staff(interaction.user, _staff_roles()):
            await interaction.response.send_message("Only the team can do that.", ephemeral=True)
            return
        member = interaction.guild.get_member(self.user_id)
        done = await cog.unflag(interaction.guild, self.user_id, interaction.user)
        await interaction.response.send_message(
            f"✅ Flag removed from {member.mention if member else self.user_id}." if done
            else "That account isn't flagged (anymore).", ephemeral=True)


def _staff_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


def _request_embed(req: dict, status: str = "pending", by: str = "") -> discord.Embed:
    color = {"pending": 0x5865F2, "ok": 0x57F287, "no": 0xED4245, "expired": 0x95A5A6}[status]
    title = {"pending": "\U0001F4E8 Invite waiting for approval", "ok": "✅ Invite approved",
             "no": "❌ Invite rejected", "expired": "⌛ Invite request expired"}[status]
    embed = discord.Embed(title=title, color=color, timestamp=datetime.fromtimestamp(req["ts"], timezone.utc))
    embed.add_field(name="From", value=f"<@{req['author']}> (`{req.get('author_name', '')}`)", inline=True)
    embed.add_field(name="Channel", value=f"<#{req['channel']}>", inline=True)
    info = req.get("server") or {}
    if info:
        created = f", created <t:{int(info['created'])}:D>" if info.get("created") else ""
        embed.add_field(name="Invites to", value=f"**{info.get('name', '?')}** - {info.get('members', '?')} members"
                                                f"{created}", inline=False)
    embed.add_field(name="Message", value=(req["content"][:1000] or "*(empty)*"), inline=False)
    if by:
        embed.add_field(name="Decided by", value=by, inline=False)
    return embed


def _request_view(rid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(InviteDecision("ok", rid))
    view.add_item(InviteDecision("no", rid))
    return view


class LinkFilter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._invite_cache: dict[str, tuple[dict | None, float]] = {}  # code -> (server info, ts)
        bot.add_dynamic_items(InviteDecision, Unflag)

    # -------- Invites ---------------------------------------------------------

    async def _invite_info(self, code: str) -> dict | None:
        """Server behind an invite: {id, name, description, nsfw, members, online, created}, None if invalid."""
        cached = self._invite_cache.get(code)
        if cached and time.monotonic() - cached[1] < _INVITE_CACHE_SECONDS:
            return cached[0]
        info = None
        try:
            invite = await self.bot.fetch_invite(code, with_counts=True)
            g = invite.guild
            if g is not None:
                level = getattr(g, "nsfw_level", None)
                info = {
                    "id": g.id, "name": g.name or "", "description": getattr(g, "description", None) or "",
                    "nsfw": getattr(level, "name", str(level or "")),
                    "members": invite.approximate_member_count, "online": invite.approximate_presence_count,
                    "created": discord.utils.snowflake_time(g.id).timestamp(),
                }
        except (discord.NotFound, discord.HTTPException):
            info = None
        if len(self._invite_cache) > 500:
            self._invite_cache.clear()
        self._invite_cache[code] = (info, time.monotonic())
        return info

    async def _invite_guild_id(self, code: str) -> int | None:
        info = await self._invite_info(code)
        return info["id"] if info else None

    async def _violation(self, message: discord.Message) -> tuple[str, str, dict | None] | None:
        """(kind, reason, server info). kind: nsfw_invite, adult_link, scam, invite, dead_invite."""
        cfg = _config()
        text = message.content or ""
        found_invite = None
        if cfg.get("block_invites", True):
            for match in INVITE_RE.finditer(text):
                service, code = match.group(1).lower(), match.group(2)
                if service.startswith(("discord.gg", "discord.com", "discordapp.com")):
                    info = await self._invite_info(code)
                    if info and info["id"] == message.guild.id:
                        continue
                    if info and cfg.get("block_nsfw_invites", True):
                        why = ("Discord rates the server 18+" if info["nsfw"] in ("explicit", "age_restricted")
                               else adult_text_reason(f"{info['name']}\n{info['description']}"))
                        if why:
                            return "nsfw_invite", f"invite to an 18+ server: {why}", info
                    found_invite = found_invite or ("invite" if info else "dead_invite", info)
                else:
                    found_invite = found_invite or ("invite", None)
        allowed = [d.lower() for d in cfg.get("allowed_domains", [])]
        hosts = [h.lower() for h in URL_RE.findall(text)]
        if cfg.get("block_adult_links", True):
            for host in hosts:
                if not _host_matches(host.removeprefix("www."), allowed) and adult_domain(host):
                    return "adult_link", f"link to an adult website ({host[:80]})", None
        if cfg.get("block_scam_links", True):
            blocked = [d.lower() for d in cfg.get("blocked_domains", [])]
            for host in hosts:
                reason = scam_reason(host, text, allowed, blocked)
                if reason:
                    return "scam", f"{reason} ({host[:80]})", None
        if found_invite:
            kind, info = found_invite
            return kind, "invite to another server", info
        return None

    # -------- Checking messages ------------------------------------------------

    async def _check(self, message: discord.Message) -> None:
        if not _config().get("enabled", True):
            return
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member) or message.author.guild_permissions.manage_messages:
            return
        text = message.content or ""
        if "http" not in text.lower() and not INVITE_RE.search(text) and "www." not in text.lower():
            return
        from cogs.maintenance import is_under_maintenance
        if is_under_maintenance("linkfilter"):
            return
        found = await self._violation(message)
        if not found:
            return
        kind, reason, info = found
        mark_bot_delete(message.id)  # already logged below in #mod-logs
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            return
        log.info("Link filter removed a message from %s in #%s: %s", message.author, message.channel, reason)

        request_sent = False
        if kind == "invite" and _config().get("invite_approval", True) and "@everyone" not in text and "@here" not in text:
            request_sent = await self._ask_approval(message, info)
        flagged = False
        if kind == "nsfw_invite":
            flagged = await self.flag(message.author, reason, info)

        if request_sent:
            notice = "{user} invites to other servers need the team's OK - I've asked them. If they approve, it shows up again."
        else:
            notice = _config().get("warn_message", "{user} that link isn't allowed here and was removed.")
        try:
            await message.channel.send(notice.format(user=message.author.mention), delete_after=10,
                                       allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass
        await self._log(message, kind, reason, info, request_sent, flagged)

    async def _log(self, message, kind, reason, info, request_sent, flagged):
        try:
            from cogs.logging_cog import _get_log_channel, _safe_send
            ch = _get_log_channel(message.guild, "mod")
            if not ch:
                return
            title = {"nsfw_invite": "\U0001F51E 18+ server invite removed", "adult_link": "\U0001F51E Adult link removed",
                     "scam": "⚠ Scam link removed"}.get(kind, "Link Removed")
            color = 0xED4245 if kind in ("nsfw_invite", "adult_link", "scam") else 0xE67E22
            embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Member", value=f"{message.author.mention} ({message.author})", inline=False)
            embed.add_field(name="Channel", value=message.channel.mention, inline=True)
            embed.add_field(name="Reason", value=reason[:1024], inline=True)
            if info:
                created = f", created <t:{int(info['created'])}:D>" if info.get("created") else ""
                embed.add_field(name="Server", value=f"**{info['name'][:100]}** - {info.get('members') or '?'} members"
                                                     f"{created}", inline=False)
            embed.add_field(name="Message", value=(message.content or "")[:1000] or "*(empty)*", inline=False)
            if request_sent:
                embed.set_footer(text="The team got an approval request by DM.")
            view = None
            if flagged:
                embed.add_field(name="Account flagged", value="Marked as possibly compromised (no timeout/ban). "
                                "The member got tips to secure the account.", inline=False)
                view = discord.ui.View(timeout=None)
                view.add_item(Unflag(message.guild.id, message.author.id))
            if view is None:
                await _safe_send(ch, embed=embed)
            else:
                try:
                    await ch.send(embed=embed, view=view)
                except discord.HTTPException:
                    pass
        except Exception:
            log.exception("Failed to log link removal")

    # -------- Approval requests -------------------------------------------------

    async def _ask_approval(self, message: discord.Message, info: dict | None) -> bool:
        data = load_json(REQUESTS_FILE)
        reqs = data.setdefault("requests", {})
        now = time.time()
        for rid in [r for r, v in reqs.items() if now - v["ts"] > REQUEST_DAYS * 86400]:
            reqs.pop(rid)
        if any(v["author"] == message.author.id and v["status"] == "pending" and v["guild"] == message.guild.id
               for v in reqs.values()):
            save_json(REQUESTS_FILE, data)
            return True   # one open request per member - the team already has it
        rid = secrets.token_hex(6)
        req = {"guild": message.guild.id, "channel": message.channel.id, "author": message.author.id,
               "author_name": str(message.author), "content": message.content or "", "server": info,
               "ts": now, "status": "pending", "dms": []}
        embed, view = _request_embed(req), _request_view(rid)
        names = set(_staff_roles())
        staff = [m for m in message.guild.members if not m.bot and any(r.name in names for r in m.roles)]
        for person in staff[:25]:
            try:
                msg = await person.send(embed=embed, view=view)
                req["dms"].append([msg.channel.id, msg.id])
            except discord.HTTPException:
                pass
        if not req["dms"]:
            chat = discord.utils.get(message.guild.text_channels, name=CHANNELS.get("staff_chat"))
            if chat is None:
                return False
            try:
                msg = await chat.send(embed=embed, view=view)
                req["dms"].append([msg.channel.id, msg.id])
            except discord.HTTPException:
                return False
        reqs[rid] = req
        save_json(REQUESTS_FILE, data)
        return True

    async def decide(self, interaction: discord.Interaction, rid: str, approve: bool) -> None:
        data = load_json(REQUESTS_FILE)
        req = data.get("requests", {}).get(rid)
        if req is None:
            await interaction.response.edit_message(embed=discord.Embed(
                title="⌛ Invite request expired", color=0x95A5A6), view=None)
            return
        guild = self.bot.get_guild(req["guild"])
        member = guild.get_member(interaction.user.id) if guild else None
        if member is None or not is_staff(member, _staff_roles()):
            await interaction.response.send_message("Only the team can decide this.", ephemeral=True)
            return
        if req["status"] != "pending":
            await interaction.response.edit_message(embed=_request_embed(req, req["status"], req.get("by", "")), view=None)
            return
        await interaction.response.defer()
        req["status"] = "ok" if approve else "no"
        req["by"] = f"{member.mention} ({member})"
        channel = guild.get_channel(req["channel"]) or guild.get_thread(req["channel"])
        author = guild.get_member(req["author"])
        if approve and channel is not None:
            try:
                await channel.send(f"\U0001F4E8 Shared by <@{req['author']}> (approved by {member.mention}):\n"
                                   f"{req['content']}"[:2000], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                req["by"] += " - but posting failed (missing permissions?)"
        save_json(REQUESTS_FILE, data)
        embed = _request_embed(req, req["status"], req["by"])
        for ch_id, msg_id in req["dms"]:
            try:
                ch = self.bot.get_channel(ch_id) or await self.bot.fetch_channel(ch_id)
                await ch.get_partial_message(msg_id).edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
        if author is not None:
            try:
                await author.send(
                    f"Your invite in **{guild.name}** was {'approved and posted' if approve else 'not approved'} "
                    f"by the team.")
            except discord.HTTPException:
                pass

    # -------- Flagging possibly compromised accounts ----------------------------

    async def flag(self, member: discord.Member, reason: str, info: dict | None) -> bool:
        data = load_json(FLAGS_FILE)
        g = data.setdefault(str(member.guild.id), {})
        entry = g.setdefault(str(member.id), {"count": 0})
        first = entry["count"] == 0
        entry.update(count=entry["count"] + 1, ts=time.time(), reason=reason, name=str(member),
                     server=(info or {}).get("name", ""))
        save_json(FLAGS_FILE, data)
        try:
            from cogs.moderation import record_action
            record_action(member.guild.id, member.id, "flagged", self.bot.user, f"Possibly compromised: {reason}")
        except Exception:
            pass
        role_name = _config().get("flag_role", "⚠ Possibly compromised")
        if role_name:
            role = discord.utils.get(member.guild.roles, name=role_name)
            if role is None:
                try:
                    role = await member.guild.create_role(name=role_name, color=discord.Color.orange(),
                                                          permissions=discord.Permissions.none(),
                                                          reason="Marker for accounts that posted 18+ spam")
                except discord.HTTPException:
                    role = None
            if role is not None and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Posted an invite to an 18+ server")
                except discord.HTTPException:
                    pass
        if first:
            try:
                await member.send(
                    f"⚠ A message from your account in **{member.guild.name}** was removed: it contained an invite "
                    "to an 18+ server. That usually means **your account was hacked** or a bad app/bot has access.\n\n"
                    "**Please secure your account:**\n"
                    "1. Change your Discord password (Settings -> My Account).\n"
                    "2. Turn on two-factor authentication (2FA).\n"
                    "3. Settings -> Authorized Apps: remove everything you don't know.\n"
                    "4. Don't run unknown programs (e.g. \"game cheats\") and never scan QR codes from strangers.\n\n"
                    "Nothing else happened - you weren't banned or muted. The team was informed.")
            except discord.HTTPException:
                pass
        return True

    async def unflag(self, guild: discord.Guild, user_id: int, by) -> bool:
        data = load_json(FLAGS_FILE)
        removed = data.get(str(guild.id), {}).pop(str(user_id), None) is not None
        save_json(FLAGS_FILE, data)
        member = guild.get_member(user_id)
        role = discord.utils.get(guild.roles, name=_config().get("flag_role", "⚠ Possibly compromised") or "\0")
        if member is not None and role is not None and role in member.roles:
            removed = True
            try:
                await member.remove_roles(role, reason=f"Flag removed by {by}")
            except discord.HTTPException:
                pass
        if removed:
            try:
                from cogs.moderation import record_action
                record_action(guild.id, user_id, "unflagged", by, "Flag removed")
            except Exception:
                pass
        return removed

    @app_commands.command(name="unflag", description="Remove the 'possibly compromised' flag from a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def unflag_cmd(self, interaction: discord.Interaction, member: discord.Member):
        if not is_staff(interaction.user, _staff_roles()):
            await interaction.response.send_message("Only the team can do that.", ephemeral=True)
            return
        done = await self.unflag(interaction.guild, member.id, interaction.user)
        await interaction.response.send_message(f"✅ Flag removed from {member.mention}." if done
                                                else f"{member.mention} isn't flagged.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self._check(after)


async def setup(bot):
    await bot.add_cog(LinkFilter(bot))
