"""Scheduled announcements.

    /schedule create channel when [ping]   -> a form for title + text opens
    /schedule list
    /schedule edit id                      -> change time, title or text
    /schedule delete id

Also manageable in the web panel (Scheduled page). `when` accepts
"2026-10-01 18:00", "01.10.2026 18:00", "01.10. 18:00", "18:00" (next time
it's 18:00) or a relative time like "2h" / "in 3d". Times are in the timezone
from SERVER_TEMPLATE["scheduled_announcements"]. Administrator only.
Stored in data/scheduled.json; sent posts are removed from the file.
"""

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, parse_duration, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.scheduler")

SCHEDULE_FILE = DATA_DIR / "scheduled.json"
MAX_AHEAD_DAYS = 365
PING_CHOICES = [
    app_commands.Choice(name="No ping", value="none"),
    app_commands.Choice(name="@everyone", value="everyone"),
    app_commands.Choice(name="@Announcements role", value="role:Announcements"),
]


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(SERVER_TEMPLATE.get("scheduled_announcements", {}).get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def load_items() -> list[dict]:
    return load_json(SCHEDULE_FILE, default={"items": []}).get("items", [])


def save_items(items: list[dict]) -> None:
    save_json(SCHEDULE_FILE, {"items": items})


def parse_when(text: str, now: datetime | None = None) -> float | None:
    """Turn user input into a UTC timestamp in the future, or None."""
    text = (text or "").strip()
    zone = tz()
    now = now or datetime.now(zone)
    rel = text[3:] if text.lower().startswith("in ") else text
    delta = parse_duration(rel)
    if delta is not None and not re.fullmatch(r"\d+", rel.strip()):
        return (now + delta).timestamp()

    formats = ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%Y-%m-%dT%H:%M"]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=zone).timestamp()  # validate_time rejects the past
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?\s+(\d{1,2}):(\d{2})", text)
    if m:
        day, month, hour, minute = map(int, m.groups())
        try:
            dt = now.replace(month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if dt <= now:
            dt = dt.replace(year=dt.year + 1)
        return dt.timestamp()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = map(int, m.groups())
        if hour > 23 or minute > 59:
            return None
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt.timestamp()
    return None


def validate_time(ts: float | None) -> str | None:
    if ts is None:
        return "I couldn't read that time. Use e.g. `2026-10-01 18:00`, `01.10. 18:00`, `18:00` or `2h`."
    if ts <= time.time():
        return "That time is in the past."
    if ts > time.time() + MAX_AHEAD_DAYS * 86400:
        return f"You can schedule at most {MAX_AHEAD_DAYS} days ahead."
    return None


def local_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz()).strftime("%Y-%m-%d %H:%M")


def add_item(guild_id: int, channel_id: int, title: str, message: str, send_at: float,
             ping: str, created_by: str) -> dict:
    item = {
        "id": uuid.uuid4().hex[:6],
        "guild_id": guild_id,
        "channel_id": channel_id,
        "title": title.strip()[:256],
        "message": message.replace("\r", "").strip()[:4000],
        "send_at": send_at,
        "ping": ping,
        "created_by": created_by,
        "created_at": time.time(),
    }
    items = load_items()
    items.append(item)
    save_items(items)
    return item


def update_item(item_id: str, **changes) -> dict | None:
    items = load_items()
    for item in items:
        if item["id"] == item_id:
            item.update({k: v for k, v in changes.items() if v is not None})
            save_items(items)
            return item
    return None


def delete_item(item_id: str) -> bool:
    items = load_items()
    keep = [i for i in items if i["id"] != item_id]
    if len(keep) == len(items):
        return False
    save_items(keep)
    return True


def build_embed(item: dict) -> discord.Embed:
    embed = discord.Embed(title=item["title"], description=item["message"], color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Announcement")
    return embed


async def send_item(bot, item: dict) -> bool:
    channel = bot.get_channel(item["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        log.warning("Scheduled post %s: channel %s not found", item["id"], item["channel_id"])
        return False
    content, mentions = None, discord.AllowedMentions.none()
    ping = item.get("ping", "none")
    if ping == "everyone":
        content, mentions = "@everyone", discord.AllowedMentions(everyone=True)
    elif ping.startswith("role:"):
        role = discord.utils.get(channel.guild.roles, name=ping[5:])
        if role:
            content, mentions = role.mention, discord.AllowedMentions(roles=[role])
    try:
        await channel.send(content=content, embed=build_embed(item), allowed_mentions=mentions)
        log.info("Sent scheduled announcement %s in #%s", item["id"], channel.name)
        return True
    except discord.HTTPException:
        log.exception("Failed to send scheduled announcement %s", item["id"])
        return False


class ScheduleModal(discord.ui.Modal):
    def __init__(self, *, channel: discord.TextChannel | None = None, send_at: float | None = None,
                 ping: str = "none", item: dict | None = None):
        super().__init__(title="Edit scheduled announcement" if item else "Schedule an announcement")
        self.channel = channel
        self.send_at = send_at
        self.ping = ping
        self.item = item
        self.title_input = discord.ui.TextInput(label="Title", max_length=256,
                                                default=item["title"] if item else None)
        self.message_input = discord.ui.TextInput(label="Text (Discord markdown works)", style=discord.TextStyle.paragraph,
                                                  max_length=4000, default=item["message"] if item else None)
        self.add_item(self.title_input)
        self.add_item(self.message_input)
        if item:
            self.when_input = discord.ui.TextInput(label="When (e.g. 2026-10-01 18:00)", max_length=40,
                                                   default=local_str(item["send_at"]))
            self.add_item(self.when_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.item:
            ts = parse_when(self.when_input.value)
            error = validate_time(ts)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            item = update_item(self.item["id"], title=self.title_input.value.strip()[:256],
                               message=self.message_input.value.strip()[:4000], send_at=ts)
            if item is None:
                await interaction.response.send_message("That announcement was already sent or deleted.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Updated `{item['id']}` - it will be posted <t:{int(ts)}:R> (<t:{int(ts)}:f>).", ephemeral=True
            )
            return
        item = add_item(interaction.guild_id, self.channel.id, self.title_input.value, self.message_input.value,
                        self.send_at, self.ping, str(interaction.user))
        await interaction.response.send_message(
            f"\U0001F4C5 Scheduled `{item['id']}` for {self.channel.mention} <t:{int(self.send_at)}:R> "
            f"(<t:{int(self.send_at)}:f>).\nPreview:",
            embed=build_embed(item), ephemeral=True,
        )
        log.info("Announcement %s scheduled by %s", item["id"], interaction.user)


class Scheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.send_loop.start()

    def cog_unload(self):
        if self.send_loop.is_running():
            self.send_loop.cancel()

    group = app_commands.Group(
        name="schedule",
        description="Scheduled announcements (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @tasks.loop(seconds=20)
    async def send_loop(self):
        now = time.time()
        due = [i for i in load_items() if i["send_at"] <= now]
        for item in due:
            await send_item(self.bot, item)
            delete_item(item["id"])

    @send_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _id_autocomplete(self, interaction: discord.Interaction, current: str):
        items = sorted((i for i in load_items() if i.get("guild_id") == interaction.guild_id), key=lambda i: i["send_at"])
        return [
            app_commands.Choice(name=f"{i['id']} - {local_str(i['send_at'])} - {i['title']}"[:100], value=i["id"])
            for i in items if current.lower() in (i["id"] + i["title"]).lower()
        ][:25]

    @group.command(name="create", description="Schedule an announcement (a form opens for the text).")
    @app_commands.describe(channel="Where to post it", when="e.g. 2026-10-01 18:00, 01.10. 18:00, 18:00 or 2h",
                           ping="Who to ping")
    @app_commands.choices(ping=PING_CHOICES)
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel, when: str,
                     ping: app_commands.Choice[str] | None = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        ts = parse_when(when)
        error = validate_time(ts)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(f"I can't post in {channel.mention}.", ephemeral=True)
            return
        await interaction.response.send_modal(ScheduleModal(channel=channel, send_at=ts,
                                                            ping=ping.value if ping else "none"))

    @group.command(name="list", description="Show upcoming scheduled announcements.")
    async def list_items(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        items = sorted((i for i in load_items() if i.get("guild_id") == interaction.guild_id), key=lambda i: i["send_at"])
        if not items:
            await interaction.response.send_message("Nothing scheduled.", ephemeral=True)
            return
        embed = discord.Embed(title="Scheduled announcements", color=0x57F287)
        for i in items[:20]:
            ping = {"none": "", "everyone": " · @everyone"}.get(i.get("ping", "none"), f" · @{i['ping'][5:]}")
            embed.add_field(
                name=f"`{i['id']}` {i['title']}"[:256],
                value=f"<#{i['channel_id']}> · <t:{int(i['send_at'])}:f> (<t:{int(i['send_at'])}:R>){ping}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="edit", description="Change the time, title or text of a scheduled announcement.")
    @app_commands.describe(announcement_id="The ID from /schedule list")
    @app_commands.autocomplete(announcement_id=_id_autocomplete)
    async def edit(self, interaction: discord.Interaction, announcement_id: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        item = next((i for i in load_items() if i["id"] == announcement_id.strip()), None)
        if item is None:
            await interaction.response.send_message("No scheduled announcement with that ID.", ephemeral=True)
            return
        await interaction.response.send_modal(ScheduleModal(item=item))

    @group.command(name="delete", description="Delete a scheduled announcement.")
    @app_commands.describe(announcement_id="The ID from /schedule list")
    @app_commands.autocomplete(announcement_id=_id_autocomplete)
    async def delete(self, interaction: discord.Interaction, announcement_id: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if delete_item(announcement_id.strip()):
            await interaction.response.send_message("Deleted.", ephemeral=True)
        else:
            await interaction.response.send_message("No scheduled announcement with that ID.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Scheduler(bot))
