"""Support helpers for tickets and the mod-support forum.

Priority       Tickets (form answers or the first messages) with words like "crash",
               "world broken", "hacked" are marked urgent: 🔴 in the channel name, a
               note in the ticket and a ping in the staff chat.
Support times  Outside the support times (config, default Mon-Fri 13:10-22:00,
               Sat-Sun 08:00-23:00) a new ticket gets a note when you're back - or,
               if someone from the team was active in the last 20 minutes, a note
               that someone might answer anyway.
Versions       Someone writes "doesn't work" / "crash" in a ticket or a forum post
               without any versions: the bot asks once for mod, Minecraft version and
               loader with select menus and posts the answer as a short summary.
Config: SERVER_TEMPLATE["support_helpers"].
"""

import logging
import re
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from cogs.common import feature_on, is_staff
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.support_helpers")

URGENT = re.compile(
    r"(?i)(crash\w*|absturz|abst[uü]rz\w*|st[uü]rzt ab|world (?:lost|gone|broken|corrupt\w*|deleted)|"
    r"welt (?:weg|kaputt|gel[oö]scht|verloren|besch[aä]digt)|lost my world|corrupt\w*|data loss|"
    r"hacked|gehackt|compromised|startet nicht mehr|won'?t (?:start|launch|load)|can'?t (?:start|join|launch))"
)
PROBLEM = re.compile(
    r"(?i)(geht nicht|funktioniert nicht|klappt nicht|doesn'?t work|does not work|not working|isn'?t working|"
    r"won'?t work|broken|kaputt|crash\w*|absturz|st[uü]rzt|error|fehler|bug|problem|hilfe|help)"
)
HAS_VERSION = re.compile(r"(?i)(1\.\d{1,2}(?:\.\d{1,2})?|\b(?:fabric|forge|neoforge|quilt)\b)")
LOADERS = ["Fabric", "NeoForge", "Forge", "Quilt"]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ACTIVE_SECONDS = 20 * 60


def _config() -> dict:
    return SERVER_TEMPLATE.get("support_helpers", {})


def _support_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_config().get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def _span(text: str) -> tuple[dtime, dtime]:
    a, b = text.split("-")
    ha, ma = (int(x) for x in a.strip().split(":"))
    hb, mb = (int(x) for x in b.strip().split(":"))
    return dtime(ha, ma), dtime(min(hb, 23), mb if hb < 24 else 59)


def support_hours(day: int) -> tuple[dtime, dtime]:
    hours = _config().get("hours", {"weekdays": "13:10-22:00", "weekend": "08:00-23:00"})
    return _span(hours.get("weekend" if day >= 5 else "weekdays", "13:10-22:00"))


def in_support_hours(now: datetime) -> bool:
    start, end = support_hours(now.weekday())
    return start <= now.time() <= end


def next_opening(now: datetime) -> str:
    start, _ = support_hours(now.weekday())
    if now.time() < start:
        return f"today at {start:%H:%M}"
    tomorrow = (now.weekday() + 1) % 7
    return f"tomorrow at {support_hours(tomorrow)[0]:%H:%M}"


def hours_text() -> str:
    wd, we = support_hours(0), support_hours(5)
    return f"Mon-Fri {wd[0]:%H:%M}-{wd[1]:%H:%M}, Sat-Sun {we[0]:%H:%M}-{we[1]:%H:%M}"


def _is_ticket(channel) -> tuple[int | None, str | None]:
    try:
        from cogs.tickets import _is_ticket_channel, _parse_topic
    except Exception:
        return None, None
    if not _is_ticket_channel(channel):
        return None, None
    return _parse_topic(channel)


def _is_support_post(channel) -> int | None:
    """Owner id of a post in the mod-support forum."""
    parent = getattr(channel, "parent", None)
    if isinstance(channel, discord.Thread) and parent is not None and parent.name == CHANNELS.get("mod_support"):
        return channel.owner_id
    return None


# -------- Version question --------------------------------------------------------

class VersionView(discord.ui.View):
    def __init__(self, owner_id: int, mods: list[dict]):
        super().__init__(timeout=1800)
        self.owner_id = owner_id
        self.choice = {"mod": None, "mc": None, "loader": None}
        mod_options = [discord.SelectOption(label=m.get("title", m.get("slug", "?"))[:100], value=m.get("slug", "?")[:100])
                       for m in mods[:24]] + [discord.SelectOption(label="Another mod", value="other")]
        versions = sorted({v for m in mods for v in (m.get("game_versions") or [])},
                          key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")], reverse=True)
        mc_options = [discord.SelectOption(label=v, value=v) for v in versions[:24] if re.match(r"^1\.\d+(\.\d+)?$", v)]
        mc_options = (mc_options or [discord.SelectOption(label=v, value=v) for v in
                                     ("1.21.1", "1.21", "1.20.6", "1.20.4", "1.20.1", "1.19.2", "1.18.2")])[:24]
        mc_options.append(discord.SelectOption(label="Other", value="other"))
        self.add_item(self._select("mod", "Which mod?", mod_options))
        self.add_item(self._select("mc", "Minecraft version?", mc_options))
        self.add_item(self._select("loader", "Mod loader?", [discord.SelectOption(label=x, value=x) for x in LOADERS]))

    def _select(self, key: str, placeholder: str, options) -> discord.ui.Select:
        select = discord.ui.Select(placeholder=placeholder, options=options, custom_id=f"verq:{key}")

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id and not is_staff(interaction.user, _support_roles()):
                await interaction.response.send_message("Only the person who asked can answer this.", ephemeral=True)
                return
            self.choice[key] = select.values[0]
            if all(self.choice.values()):
                self.stop()
                mod = self.choice["mod"]
                summary = (f"\U0001F4CB **Setup:** {'another mod' if mod == 'other' else mod} - Minecraft "
                           f"{'(other)' if self.choice['mc'] == 'other' else self.choice['mc']} - {self.choice['loader']}")
                await interaction.response.edit_message(content=summary, embed=None, view=None)
            else:
                await interaction.response.defer()
        select.callback = callback
        return select


class SupportHelpers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.staff_seen: dict[int, float] = {}        # staff member id -> last message time
        self.asked: set[int] = set()                  # channels where the version question was asked
        self.urgent: set[int] = set()
        self.checked_messages: dict[int, int] = {}    # channel -> opener messages looked at

    # -------- Helpers -----------------------------------------------------------

    def staff_active(self, guild: discord.Guild) -> list[discord.Member]:
        now = time.time()
        return [m for uid, ts in self.staff_seen.items() if now - ts < ACTIVE_SECONDS
                and (m := guild.get_member(uid)) is not None]

    async def _own_mods(self) -> list[dict]:
        cog = self.bot.get_cog("Modrinth")
        try:
            if cog is not None and hasattr(cog, "_cached_projects"):
                return await cog._cached_projects() or []
            from cogs.modrinth import _fetch_user_projects
            return await _fetch_user_projects(SERVER_TEMPLATE.get("modrinth", {}).get("username", "")) or []
        except Exception:
            return []

    # -------- Called by cogs/tickets.py when a ticket was opened ------------------

    async def ticket_opened(self, channel: discord.TextChannel, opener: discord.Member, type_key: str,
                            answers: list[tuple[str, str]] | None) -> None:
        text = "\n".join(v for _, v in answers or [])
        if _config().get("priority", True) and feature_on("urgent_tickets") and URGENT.search(text):
            await self.mark_urgent(channel, opener, URGENT.search(text).group(0))
        if _config().get("support_times", True) and feature_on("support_hours_note"):
            now = datetime.now(_tz())
            if not in_support_hours(now):
                active = self.staff_active(channel.guild)
                if active:
                    note = (f"\U0001F319 It's outside our support times ({hours_text()}), but "
                            f"{', '.join(m.display_name for m in active[:3])} from the team "
                            f"{'is' if len(active) == 1 else 'are'} around right now - you might get an answer soon.")
                else:
                    note = (f"\U0001F319 It's outside our support times right now ({hours_text()}). "
                            f"We'll answer {next_opening(now)} at the latest - no need to ping anyone.")
                try:
                    await channel.send(note)
                except discord.HTTPException:
                    pass
        if text and _config().get("version_question", True) and feature_on("version_question") \
                and type_key in ("bug", "help"):
            self.checked_messages[channel.id] = 0
            if HAS_VERSION.search(text):
                self.asked.add(channel.id)   # the form already has the versions

    async def mark_urgent(self, channel: discord.TextChannel, opener, word: str) -> None:
        if channel.id in self.urgent:
            return
        self.urgent.add(channel.id)
        if not channel.name.startswith("\U0001F534"):
            try:
                await channel.edit(name=f"\U0001F534-{channel.name}"[:100], reason="Urgent ticket")
            except discord.HTTPException:
                pass
        try:
            await channel.send(f"\U0001F534 **Marked as urgent** (\"{word}\") - the team has been told.")
        except discord.HTTPException:
            pass
        chat = discord.utils.get(channel.guild.text_channels, name=CHANNELS.get("staff_chat"))
        roles = [r for n in _support_roles() if (r := discord.utils.get(channel.guild.roles, name=n))]
        if chat is not None:
            try:
                await chat.send(f"\U0001F6A8 {' '.join(r.mention for r in roles[:2])} Urgent ticket from "
                                f"{getattr(opener, 'mention', 'someone')}: {channel.mention} (\"{word}\")",
                                allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException:
                pass
        from cogs.push import safe_notify
        safe_notify("urgent", "\U0001F534 Urgent ticket", f"#{channel.name} - \"{word}\"", "/tickets")
        log.info("Ticket %s marked urgent (%s)", channel.name, word)

    # -------- Messages ------------------------------------------------------------

    async def ask_versions(self, channel, owner_id: int) -> None:
        self.asked.add(channel.id)
        mods = await self._own_mods()
        embed = discord.Embed(
            title="\U0001F527 Quick question",
            description="To help you faster, please pick your **mod**, **Minecraft version** and **mod loader** "
                        "below (or just write them).",
            color=0x5865F2)
        try:
            await channel.send(content=f"<@{owner_id}>", embed=embed, view=VersionView(owner_id, mods),
                               allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if is_staff(message.author, _support_roles()):
            self.staff_seen[message.author.id] = time.time()
            return
        opener, type_key = _is_ticket(message.channel)
        forum_owner = None if opener else _is_support_post(message.channel)
        owner = opener or forum_owner
        if owner is None or message.author.id != owner:
            return
        seen = self.checked_messages.get(message.channel.id, 0)
        if seen >= 5:   # only the first few messages are looked at
            return
        self.checked_messages[message.channel.id] = seen + 1
        text = message.content or ""
        if opener and _config().get("priority", True) and feature_on("urgent_tickets") and (m := URGENT.search(text)):
            await self.mark_urgent(message.channel, message.author, m.group(0))
        if not _config().get("version_question", True) or not feature_on("version_question") \
                or message.channel.id in self.asked:
            return
        if opener and type_key not in ("bug", "help"):
            return
        if HAS_VERSION.search(text):
            self.asked.add(message.channel.id)
            return
        if PROBLEM.search(text):
            await self.ask_versions(message.channel, owner)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if _is_support_post(thread) is None or not _config().get("version_question", True) \
                or not feature_on("version_question"):
            return
        self.checked_messages.setdefault(thread.id, 0)


async def setup(bot):
    await bot.add_cog(SupportHelpers(bot))
