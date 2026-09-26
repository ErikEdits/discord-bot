"""Ticket system with multiple categories, persistent buttons,
per-close transcripts saved to a ticket-archive channel, and automatic
closure of inactive tickets.

Inside a ticket, staff can:
- "Claim" it (shows who takes care of it),
- add / remove people with /ticket-add and /ticket-remove,
- "Forward bug" (bug tickets): sends a summary + transcript to the webhook
  set with /settings bug-webhook.
After a ticket is closed the opener gets a DM asking for a 1-5 star rating;
the rating is posted to the ticket-archive channel.

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

from cogs.common import is_staff, post_to_webhook, webhook_url
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


def _is_staff(member) -> bool:
    return is_staff(member, _support_role_names())


def _is_ticket_channel(channel) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category is not None
        and channel.category.name == _category_name()
    )


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
        return
    except discord.NotFound:
        return  # already closed by a second click - that call asks for the rating

    from cogs.stats import record_event
    record_event(guild.id, "tickets_closed")

    creator_id, _type_key = _parse_topic(channel)
    if creator_id and _config().get("rating_enabled", True):
        await _ask_for_rating(guild, creator_id, channel.name)


async def _ask_for_rating(guild: discord.Guild, user_id: int, ticket_name: str) -> None:
    user = guild.get_member(user_id)
    if user is None:
        return
    view = discord.ui.View(timeout=None)
    for stars in range(1, 6):
        view.add_item(RateButton(guild.id, stars, ticket_name))
    embed = discord.Embed(
        title="How was your support?",
        description=f"Your ticket **#{ticket_name}** on **{guild.name}** was closed.\n"
                    "Please rate the help you got - it takes one click.",
        color=0x5865F2,
    )
    try:
        await user.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        log.info("Could not DM rating request to %s", user)


class RateButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"ticket:rate:(?P<guild>\d+):(?P<stars>[1-5]):(?P<name>[^:]{1,60})"):
    """Star button in the rating DM. Works across restarts (the data is in the custom_id)."""

    def __init__(self, guild_id: int, stars: int, ticket_name: str):
        self.guild_id = guild_id
        self.stars = stars
        self.ticket_name = ticket_name[:60]
        super().__init__(discord.ui.Button(
            label=str(stars),
            emoji="\u2B50",
            style=discord.ButtonStyle.secondary,
            custom_id=f"ticket:rate:{guild_id}:{stars}:{self.ticket_name}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), int(match["stars"]), match["name"])

    async def callback(self, interaction: discord.Interaction):
        stars_text = "\u2B50" * self.stars
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Thanks for your feedback!",
                description=f"You rated ticket **#{self.ticket_name}** with {stars_text}",
                color=0x57F287,
            ),
            view=None,
        )
        from cogs.stats import record_rating
        record_rating(self.guild_id, self.stars)
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            return
        archive = discord.utils.get(guild.text_channels, name=CHANNELS["ticket_archive"])
        if archive is None:
            return
        embed = discord.Embed(
            title=f"Ticket rated: #{self.ticket_name}",
            description=f"{stars_text} ({self.stars}/5)",
            color=0x57F287 if self.stars >= 4 else 0xF1C40F if self.stars == 3 else 0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="By", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        try:
            await archive.send(embed=embed)
        except discord.HTTPException:
            pass
        log.info("Ticket %s rated %d/5 by %s", self.ticket_name, self.stars, interaction.user)


def _existing_ticket(guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
    category = discord.utils.get(guild.categories, name=_category_name())
    if category is None:
        return None
    return discord.utils.find(
        lambda c: c.category_id == category.id and _parse_topic(c)[0] == user_id,
        guild.text_channels,
    )


class TicketFormModal(discord.ui.Modal):
    """Questions asked before a ticket opens (the "form" of a ticket type)."""

    def __init__(self, type_def: dict):
        super().__init__(title=f"{type_def['label']}"[:45])
        self.type_key = type_def["key"]
        self.inputs: list[tuple[str, discord.ui.TextInput]] = []
        for field in type_def.get("form", [])[:5]:
            text_input = discord.ui.TextInput(
                label=field["label"][:45],
                placeholder=(field.get("placeholder") or None),
                style=discord.TextStyle.paragraph if field.get("style") == "long" else discord.TextStyle.short,
                required=field.get("required", True),
                max_length=min(int(field.get("max_length", 1000)), 4000),
            )
            self.inputs.append((field["label"], text_input))
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        answers = [(label, i.value.strip()) for label, i in self.inputs if i.value and i.value.strip()]
        await _open_ticket(interaction, self.type_key, answers)


async def start_ticket(interaction: discord.Interaction, type_key: str) -> None:
    """Entry point for the ticket buttons: checks first, then the form (if any), then the ticket."""
    from cogs.maintenance import is_under_maintenance, get_maintenance_message
    if is_under_maintenance("ticket"):
        await interaction.response.send_message(f"\U0001F527 {get_maintenance_message('ticket')}", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    type_def = _ticket_type(type_key)
    if type_def is None:
        await interaction.response.send_message("Unknown ticket type.", ephemeral=True)
        return
    existing = _existing_ticket(interaction.guild, interaction.user.id)
    if existing:
        await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
        return
    if type_def.get("form"):
        await interaction.response.send_modal(TicketFormModal(type_def))
        return
    await _open_ticket(interaction, type_key)


async def _open_ticket(interaction: discord.Interaction, type_key: str,
                       answers: list[tuple[str, str]] | None = None) -> None:
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
    for label, value in answers or []:
        embed.add_field(name=label[:256], value=value[:1024], inline=False)
    embed.set_footer(text="Use the Close Ticket button or /close when finished.")
    forward = type_key in _config().get("forward_types", ["bug"])
    try:
        await ticket_channel.send(
            content=" ".join(mentions) if mentions else None,
            embed=embed,
            view=TicketControlsView(forward=forward),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.Forbidden:
        pass

    await interaction.response.send_message(
        f"Ticket created: {ticket_channel.mention}", ephemeral=True
    )
    from cogs.stats import record_event
    record_event(guild.id, "tickets_opened")


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
        await start_ticket(interaction, self.type_key)


class TicketPanelView(discord.ui.View):
    def __init__(self, types_def: list):
        super().__init__(timeout=None)
        for t in types_def:
            self.add_item(OpenTicketButton(t))


class TicketControlsView(discord.ui.View):
    """Buttons on the first message of every ticket.

    Registered once with forward=True so all three custom_ids are handled after a
    restart; tickets of types without forwarding just don't show that button.
    """

    def __init__(self, forward: bool = True):
        super().__init__(timeout=None)
        if not forward:
            self.remove_item(self.forward_bug)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                       emoji="\U0001F512", custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_close(interaction)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success,
                       emoji="\U0001F64B", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_claim(interaction)

    @discord.ui.button(label="Forward bug", style=discord.ButtonStyle.primary,
                       emoji="\U0001F4E4", custom_id="ticket:forward")
    async def forward_bug(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_ticket_channel(interaction.channel):
            await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("Only staff can forward bugs.", ephemeral=True)
            return
        if not webhook_url("bug_webhook", "BUG_WEBHOOK_URL"):
            await interaction.response.send_message(
                "No bug webhook is set. An administrator can set it with `/settings bug-webhook`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ForwardBugModal(interaction.channel))


# Old ticket messages were sent with this view; keep the name working.
CloseTicketView = TicketControlsView


async def _do_claim(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not _is_ticket_channel(channel):
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
        return
    message = interaction.message
    embed = message.embeds[0] if message and message.embeds else None
    if embed is not None:
        for field in embed.fields:
            if field.name == "Claimed by":
                await interaction.response.send_message(f"Already claimed by {field.value}.", ephemeral=True)
                return
        embed.add_field(name="Claimed by", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=embed)
        await channel.send(f"\U0001F64B {interaction.user.mention} is taking care of this ticket.")
    else:
        await interaction.response.send_message(f"\U0001F64B {interaction.user.mention} is taking care of this ticket.")
    from cogs.stats import record_claim
    record_claim(channel.guild.id, interaction.user)
    log.info("Ticket %s claimed by %s", channel.name, interaction.user)


class ForwardBugModal(discord.ui.Modal, title="Forward bug report"):
    bug_title = discord.ui.TextInput(label="Title", max_length=200)
    summary = discord.ui.TextInput(
        label="Summary (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500,
        placeholder="What's the bug, which mod/version, how to reproduce...",
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel
        self.bug_title.default = channel.name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        url = webhook_url("bug_webhook", "BUG_WEBHOOK_URL")
        creator_id, type_key = _parse_topic(self.channel)
        filename, body = await _build_transcript(self.channel)
        embed = discord.Embed(
            title=f"\U0001F41B {self.bug_title.value}"[:256],
            description=(self.summary.value or "*(no summary - see transcript)*")[:4000],
            color=0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Ticket", value=f"#{self.channel.name}", inline=True)
        embed.add_field(name="Server", value=self.channel.guild.name, inline=True)
        if creator_id:
            opener = self.channel.guild.get_member(creator_id)
            embed.add_field(name="Reported by", value=str(opener) if opener else f"User ID {creator_id}", inline=True)
        embed.add_field(name="Forwarded by", value=str(interaction.user), inline=True)
        ok = await post_to_webhook(
            url, embed=embed, file_name=filename, file_bytes=body.encode("utf-8"), username="Bug Reports",
        )
        if not ok:
            await interaction.followup.send("Sending to the bug webhook failed - check `/settings show`.", ephemeral=True)
            return
        await interaction.followup.send("Bug forwarded.", ephemeral=True)
        try:
            await self.channel.send(f"\U0001F4E4 This bug was forwarded to the developers by {interaction.user.mention}.")
        except discord.HTTPException:
            pass
        log.info("Bug ticket %s forwarded by %s", self.channel.name, interaction.user)


async def _do_close(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if not _is_ticket_channel(channel):
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    is_mod = _is_staff(interaction.user)
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
        bot.add_view(TicketControlsView(forward=True))
        bot.add_dynamic_items(RateButton)
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

    @app_commands.command(name="ticket-add", description="Add a member to this ticket (staff).")
    @app_commands.describe(member="Member who should see this ticket")
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member):
        if not _is_ticket_channel(interaction.channel):
            await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("Only staff can add members to tickets.", ephemeral=True)
            return
        try:
            await interaction.channel.set_permissions(
                member, view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True, reason=f"Added to ticket by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("I can't change this channel's permissions.", ephemeral=True)
            return
        await interaction.response.send_message(f"\u2795 {member.mention} was added to this ticket by {interaction.user.mention}.")

    @app_commands.command(name="ticket-remove", description="Remove a member from this ticket (staff).")
    @app_commands.describe(member="Member to remove from this ticket")
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not _is_ticket_channel(interaction.channel):
            await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("Only staff can remove members from tickets.", ephemeral=True)
            return
        creator_id, _ = _parse_topic(interaction.channel)
        if member.id == creator_id:
            await interaction.response.send_message("You can't remove the person who opened the ticket.", ephemeral=True)
            return
        try:
            await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't change this channel's permissions.", ephemeral=True)
            return
        await interaction.response.send_message(f"\u2796 {member.mention} was removed from this ticket.")

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
