"""Ticket system with multiple categories, persistent buttons,
per-close transcripts saved to a ticket-archive channel, and automatic
closure of inactive tickets.

Configuration lives in SERVER_TEMPLATE['tickets']. Ticket types each
become a separate button on the panel posted by /ticket-panel.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.tickets")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
TICKETS_FILE = DATA_DIR / "tickets.json"

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


def _config() -> dict:
    return SERVER_TEMPLATE.get("tickets", {})


def _category_name() -> str:
    return _config().get("category_name", "TICKETS")


def _support_role_names() -> list:
    return _config().get("support_role_names", ["Moderator", "Admin", "Owner"])


def _ticket_type(key: str) -> dict | None:
    for t in _config().get("types", []):
        if t["key"] == key:
            return t
    return None


def _load_state() -> dict:
    if not TICKETS_FILE.exists():
        return {}
    try:
        return json.loads(TICKETS_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read tickets state")
        return {}


def _save_state(data: dict) -> None:
    TICKETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _next_ticket_number(guild_id: int) -> int:
    state = _load_state()
    key = str(guild_id)
    state[key] = state.get(key, 0) + 1
    _save_state(state)
    return state[key]


def _parse_topic(channel: discord.TextChannel) -> tuple[int | None, str | None]:
    """Pull creator_id and ticket type_key out of the channel topic."""
    if not channel.topic:
        return None, None
    creator_id, type_key = None, None
    for part in channel.topic.split("|"):
        part = part.strip()
        if part.startswith("user "):
            try:
                creator_id = int(part.split(" ", 1)[1])
            except (ValueError, IndexError):
                pass
        elif part.startswith("type "):
            type_key = part.split(" ", 1)[1] if len(part.split(" ", 1)) > 1 else None
    return creator_id, type_key


def _build_topic(creator_id: int, type_key: str) -> str:
    return f"user {creator_id} | type {type_key}"


async def _ensure_ticket_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    category = discord.utils.get(guild.categories, name=_category_name())
    if category is not None:
        return category
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for name in _support_role_names():
        role = discord.utils.get(guild.roles, name=name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_messages=True,
            )
    try:
        return await guild.create_category(
            name=_category_name(),
            overwrites=overwrites,
            reason="Ticket system",
        )
    except discord.Forbidden:
        return None


async def _build_transcript(channel: discord.TextChannel) -> tuple[str, str]:
    """Collect a plain-text transcript. Returns (filename, body)."""
    lines = [
        f"Ticket transcript: #{channel.name}",
        f"Channel ID: {channel.id}",
        f"Created: {discord.utils.format_dt(channel.created_at)}",
        f"Closed: {datetime.now(timezone.utc).isoformat()}",
        "-" * 60,
    ]
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            author = f"{msg.author} ({msg.author.id})"
            lines.append(f"[{ts}] {author}: {msg.content}")
            for attach in msg.attachments:
                lines.append(f"    -> attachment: {attach.url}")
            for embed in msg.embeds:
                if embed.title or embed.description:
                    title = embed.title or ""
                    desc = (embed.description or "")[:300]
                    lines.append(f"    -> embed: {title} | {desc}")
    except discord.Forbidden:
        lines.append("(missing permission to read message history)")
    except Exception as e:
        lines.append(f"(error fetching history: {e})")
    return f"{channel.name}.txt", "\n".join(lines)


async def _archive_and_delete(channel: discord.TextChannel, closer: discord.abc.User) -> None:
    guild = channel.guild
    filename, body = await _build_transcript(channel)
    archive = discord.utils.get(guild.text_channels, name=CHANNELS["ticket_archive"])
    if archive:
        creator_id, type_key = _parse_topic(channel)
        embed = discord.Embed(
            title=f"Closed: #{channel.name}",
            color=0x95A5A6,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Type", value=type_key or "unknown", inline=True)
        embed.add_field(name="Closed by", value=closer.mention if hasattr(closer, "mention") else str(closer), inline=True)
        if creator_id:
            embed.add_field(name="Opener", value=f"<@{creator_id}>", inline=True)
        file = discord.File(BytesIO(body.encode("utf-8")), filename=filename)
        try:
            await archive.send(embed=embed, file=file)
        except discord.Forbidden:
            log.warning("Cannot post to %s", CHANNELS["ticket_archive"])
        except Exception:
            log.exception("Failed to post transcript")

    log_ch = discord.utils.get(guild.text_channels, name=CHANNELS["mod_logs"])
    if log_ch:
        embed = discord.Embed(title="Ticket Closed", color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Closed by", value=getattr(closer, "mention", str(closer)), inline=True)
        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.Forbidden:
        log.warning("Cannot delete ticket channel: %s", channel.name)
    except discord.NotFound:
        pass


async def _open_ticket(interaction: discord.Interaction, type_key: str) -> None:
    from cogs.maintenance import is_under_maintenance, get_maintenance_message
    if is_under_maintenance("ticket"):
        await interaction.response.send_message(
            f"\U0001F527 {get_maintenance_message('ticket')}",
            ephemeral=True,
        )
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    type_def = _ticket_type(type_key)
    if type_def is None:
        await interaction.response.send_message("Unknown ticket type.", ephemeral=True)
        return
    category = await _ensure_ticket_category(guild)
    if category is None:
        await interaction.response.send_message(
            "I can't create the TICKETS category. Check my permissions.", ephemeral=True
        )
        return

    existing = discord.utils.find(
        lambda c: c.category_id == category.id and _parse_topic(c)[0] == interaction.user.id,
        guild.text_channels,
    )
    if existing:
        await interaction.response.send_message(
            f"You already have an open ticket: {existing.mention}", ephemeral=True
        )
        return

    number = _next_ticket_number(guild.id)
    safe_name = "".join(c for c in interaction.user.name.lower() if c.isalnum() or c == "-")[:18] or "user"
    channel_name = f"{type_key}-{number:04d}-{safe_name}"[:90]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True, embed_links=True, attach_files=True,
        ),
    }
    for name in _support_role_names():
        role = discord.utils.get(guild.roles, name=name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True, attach_files=True, embed_links=True,
            )

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=_build_topic(interaction.user.id, type_key),
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to create a ticket channel.", ephemeral=True
        )
        return

    mentions = []
    for name in ("Moderator", "Admin"):
        role = discord.utils.get(guild.roles, name=name)
        if role:
            mentions.append(role.mention)

    color = int(type_def.get("color", "0x5865F2"), 16)
    intro = type_def.get("intro", "").format(user=interaction.user.mention)
    embed = discord.Embed(
        title=f"{type_def.get('emoji', '')} {type_def['label']} #{number:04d}".strip(),
        description=intro,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Use the Close Ticket button or /close when finished.")
    try:
        await ticket_channel.send(
            content=" ".join(mentions) if mentions else None,
            embed=embed,
            view=CloseTicketView(),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.Forbidden:
        pass

    await interaction.response.send_message(
        f"Ticket created: {ticket_channel.mention}", ephemeral=True
    )


class OpenTicketButton(discord.ui.Button):
    def __init__(self, type_def: dict):
        super().__init__(
            label=type_def["label"],
            emoji=type_def.get("emoji"),
            style=BUTTON_STYLES.get(type_def.get("style", "primary"), discord.ButtonStyle.primary),
            custom_id=f"ticket:open:{type_def['key']}",
        )
        self.type_key = type_def["key"]

    async def callback(self, interaction: discord.Interaction):
        await _open_ticket(interaction, self.type_key)


class TicketPanelView(discord.ui.View):
    def __init__(self, types_def: list):
        super().__init__(timeout=None)
        for t in types_def:
            self.add_item(OpenTicketButton(t))


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                       emoji="\U0001F512", custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_close(interaction)


async def _do_close(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or channel.category is None or channel.category.name != _category_name():
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    is_mod = interaction.user.guild_permissions.manage_channels
    creator_id, _ = _parse_topic(channel)
    is_creator = creator_id == interaction.user.id
    if not (is_mod or is_creator):
        await interaction.response.send_message(
            "Only staff or the ticket opener can close this ticket.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Closing ticket in 5 seconds (by {interaction.user.mention}). Transcript will be saved."
    )
    await asyncio.sleep(5)
    await _archive_and_delete(channel, interaction.user)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        types_def = _config().get("types", [])
        if types_def:
            bot.add_view(TicketPanelView(types_def))
        bot.add_view(CloseTicketView())
        low_power = os.getenv("LOW_POWER", "").lower() in ("1", "true", "yes")
        if low_power:
            log.info("LOW_POWER: skipping ticket auto-close background loop")
        elif _config().get("auto_close_hours"):
            self.auto_close_loop.change_interval(minutes=_config().get("auto_close_check_minutes", 30))
            self.auto_close_loop.start()

    def cog_unload(self):
        if self.auto_close_loop.is_running():
            self.auto_close_loop.cancel()

    @app_commands.command(name="ticket-panel", description="Post the multi-button ticket panel in this channel.")
    @app_commands.default_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        types_def = _config().get("types", [])
        if not types_def:
            await interaction.response.send_message("No ticket types configured.", ephemeral=True)
            return
        lines = []
        for t in types_def:
            lines.append(f"{t.get('emoji', '')} **{t['label']}** - {t.get('intro', '').splitlines()[0][:100]}")
        embed = discord.Embed(
            title="Open a Ticket",
            description="Pick the type that fits your issue. Each ticket is a private channel between you and the staff.\n\n" + "\n".join(lines),
            color=0x5865F2,
        )
        await interaction.channel.send(embed=embed, view=TicketPanelView(types_def))
        await interaction.response.send_message("Ticket panel posted.", ephemeral=True)

    @app_commands.command(name="close", description="Close the current ticket channel.")
    async def close(self, interaction: discord.Interaction):
        await _do_close(interaction)

    @tasks.loop(minutes=30)
    async def auto_close_loop(self):
        hours = _config().get("auto_close_hours", 0)
        if not hours:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        for guild in self.bot.guilds:
            category = discord.utils.get(guild.categories, name=_category_name())
            if category is None:
                continue
            for channel in list(category.text_channels):
                try:
                    last_msg = None
                    async for msg in channel.history(limit=1):
                        last_msg = msg
                    last_activity = last_msg.created_at if last_msg else channel.created_at
                    if last_activity < cutoff:
                        log.info("Auto-closing stale ticket: %s (last activity %s)", channel.name, last_activity)
                        try:
                            await channel.send(
                                embed=discord.Embed(
                                    title="Auto-closing inactive ticket",
                                    description=f"No activity for {hours}h. Closing and archiving.",
                                    color=0xE67E22,
                                )
                            )
                        except discord.Forbidden:
                            pass
                        await _archive_and_delete(channel, self.bot.user)
                except Exception:
                    log.exception("Auto-close failed for %s", channel.name)

    @auto_close_loop.before_loop
    async def _before_auto_close(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Tickets(bot))
