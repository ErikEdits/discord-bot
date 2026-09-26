"""Link filter: removes invites to other Discord servers and scam links.

- Invites (discord.gg/..., discord.com/invite/..., dsc.gg/...) are removed unless
  they point to this server.
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
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

import discord
from discord.ext import commands

from cogs.common import mark_bot_delete
from server_template import SERVER_TEMPLATE

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


class LinkFilter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._invite_cache: dict[str, tuple[int | None, float]] = {}  # code -> (guild_id, ts)

    async def _invite_guild_id(self, code: str) -> int | None:
        cached = self._invite_cache.get(code)
        if cached and time.monotonic() - cached[1] < _INVITE_CACHE_SECONDS:
            return cached[0]
        guild_id = None
        try:
            invite = await self.bot.fetch_invite(code, with_counts=False)
            guild_id = invite.guild.id if invite.guild else None
        except (discord.NotFound, discord.HTTPException):
            guild_id = None
        if len(self._invite_cache) > 500:
            self._invite_cache.clear()
        self._invite_cache[code] = (guild_id, time.monotonic())
        return guild_id

    async def _violation(self, message: discord.Message) -> str | None:
        cfg = _config()
        text = message.content or ""
        if cfg.get("block_invites", True):
            for match in INVITE_RE.finditer(text):
                service, code = match.group(1).lower(), match.group(2)
                if service.startswith(("discord.gg", "discord.com", "discordapp.com")):
                    if await self._invite_guild_id(code) == message.guild.id:
                        continue
                return "invite to another server"
        if cfg.get("block_scam_links", True):
            allowed = [d.lower() for d in cfg.get("allowed_domains", [])]
            blocked = [d.lower() for d in cfg.get("blocked_domains", [])]
            for host in URL_RE.findall(text):
                reason = scam_reason(host, text, allowed, blocked)
                if reason:
                    return f"{reason} ({host.lower()[:80]})"
        return None

    async def _check(self, message: discord.Message) -> None:
        if not _config().get("enabled", True):
            return
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member) or message.author.guild_permissions.manage_messages:
            return
        text = message.content or ""
        if "http" not in text.lower() and not INVITE_RE.search(text):
            return
        from cogs.maintenance import is_under_maintenance
        if is_under_maintenance("linkfilter"):
            return
        reason = await self._violation(message)
        if not reason:
            return
        mark_bot_delete(message.id)  # already logged below in #mod-logs
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            return
        log.info("Link filter removed a message from %s in #%s: %s", message.author, message.channel, reason)
        warn = _config().get("warn_message", "{user} that link isn't allowed here and was removed.")
        try:
            await message.channel.send(warn.format(user=message.author.mention), delete_after=10)
        except discord.HTTPException:
            pass
        try:
            from cogs.logging_cog import _get_log_channel, _safe_send
            ch = _get_log_channel(message.guild, "mod")
            if ch:
                embed = discord.Embed(title="Link Removed", color=0xE67E22, timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Member", value=f"{message.author.mention} ({message.author})", inline=False)
                embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                embed.add_field(name="Reason", value=reason[:1024], inline=True)
                embed.add_field(name="Message", value=text[:1000] or "*(empty)*", inline=False)
                await _safe_send(ch, embed=embed)
        except Exception:
            log.exception("Failed to log link removal")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self._check(after)


async def setup(bot):
    await bot.add_cog(LinkFilter(bot))
