"""Beta tester program.

Applications are only possible while an administrator has opened them:

    /beta open duration:7d [slots:10]   open applications for a time window
    /beta close                         close them early
    /beta status                        window + numbers
    /beta remove member                 take the Beta Tester role away
    /beta panel                         (re)post the panel in the beta-program channel

The panel in CHANNELS["beta_program"] shows whether applications are open and
has an "Apply" button that opens a short form. Applications land in
CHANNELS["beta_applications"] (staff) with Accept / Reject buttons - only
members with Administrator can use them. Accepted applicants get the
"Beta Tester" role (access to the beta-testing channel) and a DM.
The window closes automatically at its end time or when all slots are filled.
State: data/beta.json.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, format_duration, is_admin, load_json, parse_duration, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.beta")

BETA_FILE = DATA_DIR / "beta.json"


def _config() -> dict:
    return SERVER_TEMPLATE.get("beta", {})


def _role_name() -> str:
    return _config().get("role", "Beta Tester")


def _load() -> dict:
    data = load_json(BETA_FILE)
    data.setdefault("window", {"open": False})
    data.setdefault("applications", {})
    return data


def _window_open(data: dict) -> bool:
    w = data["window"]
    return bool(w.get("open")) and w.get("until", 0) > time.time()


def _accepted_in_window(data: dict) -> int:
    wid = data["window"].get("id")
    return sum(1 for a in data["applications"].values() if a.get("window_id") == wid and a.get("status") == "accepted")


def build_panel_embed() -> discord.Embed:
    data = _load()
    if _window_open(data):
        w = data["window"]
        slots = w.get("slots")
        slot_line = ""
        if slots:
            slot_line = f"\n**Free spots:** {max(0, slots - _accepted_in_window(data))} of {slots}"
        embed = discord.Embed(
            title="\U0001F9EA Beta Program - applications OPEN",
            description=(
                "Want to test new mod versions before everyone else and help find bugs?\n\n"
                f"**Applications close:** <t:{int(w['until'])}:R> (<t:{int(w['until'])}:f>)"
                f"{slot_line}\n\n"
                "Click **Apply** and fill out the short form. You'll get a DM with the result."
            ),
            color=0x1ABC9C,
        )
    else:
        embed = discord.Embed(
            title="\U0001F9EA Beta Program - applications closed",
            description=(
                "Beta testers try new mod versions before release and report bugs.\n\n"
                "Applications are currently **closed**. Watch the announcements - "
                "when a new round opens, the Apply button here works again."
            ),
            color=0x95A5A6,
        )
    embed.set_footer(text="Beta testers get access to a private testing channel.")
    return embed


class BetaPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply", emoji="\U0001F9EA", style=discord.ButtonStyle.success, custom_id="beta:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = _load()
        if not _window_open(data):
            await interaction.response.send_message("Applications are closed right now.", ephemeral=True)
            return
        member = interaction.user
        if any(r.name == _role_name() for r in getattr(member, "roles", [])):
            await interaction.response.send_message("You're already a beta tester!", ephemeral=True)
            return
        wid = data["window"].get("id")
        for a in data["applications"].values():
            if a.get("user_id") == member.id and (a.get("status") == "pending" or a.get("window_id") == wid):
                await interaction.response.send_message(
                    "You already applied in this round. You'll get a DM with the result.", ephemeral=True
                )
                return
        await interaction.response.send_modal(BetaApplicationModal())


class BetaApplicationModal(discord.ui.Modal, title="Beta tester application"):
    mc_version = discord.ui.TextInput(label="Minecraft version(s) you play", max_length=100,
                                      placeholder="e.g. 1.20.1 and 1.21.1")
    loader = discord.ui.TextInput(label="Mod loader", max_length=100, placeholder="e.g. Fabric, NeoForge")
    why = discord.ui.TextInput(label="Why do you want to be a beta tester?", style=discord.TextStyle.paragraph,
                               max_length=1000)
    experience = discord.ui.TextInput(label="Testing experience / how often can you test?",
                                      style=discord.TextStyle.paragraph, max_length=1000, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        data = _load()
        if not _window_open(data):
            await interaction.response.send_message("Sorry, applications closed in the meantime.", ephemeral=True)
            return
        guild = interaction.guild
        channel = discord.utils.get(guild.text_channels, name=_config().get("applications_channel", ""))
        if channel is None:
            await interaction.response.send_message("The applications channel is missing - please tell the staff.",
                                                    ephemeral=True)
            return
        member = interaction.user
        embed = discord.Embed(title="New beta application", color=0x3498DB, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=f"{member} ({member.id})", icon_url=member.display_avatar.url)
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        if getattr(member, "joined_at", None):
            embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        embed.add_field(name="Minecraft version(s)", value=self.mc_version.value, inline=True)
        embed.add_field(name="Loader", value=self.loader.value, inline=True)
        embed.add_field(name="Why", value=self.why.value, inline=False)
        if self.experience.value:
            embed.add_field(name="Experience / time", value=self.experience.value, inline=False)
        embed.add_field(name="Status", value="⏳ Pending", inline=False)
        try:
            message = await channel.send(embed=embed, view=BetaReviewView())
        except discord.HTTPException:
            await interaction.response.send_message("Sending your application failed - please try again later.",
                                                    ephemeral=True)
            return
        app_id = uuid.uuid4().hex[:8]
        data = _load()
        data["applications"][app_id] = {
            "user_id": member.id,
            "guild_id": guild.id,
            "window_id": data["window"].get("id"),
            "message_id": message.id,
            "status": "pending",
            "created": time.time(),
        }
        save_json(BETA_FILE, data)
        await interaction.response.send_message(
            "✅ Application sent! You'll get a DM when the team has decided.", ephemeral=True
        )
        log.info("Beta application from %s", member)


def _find_application(data: dict, message_id: int):
    for app_id, a in data["applications"].items():
        if a.get("message_id") == message_id:
            return app_id, a
    return None, None


def _set_status_field(embed: discord.Embed, text: str, color: int) -> discord.Embed:
    embed.color = color
    for i, field in enumerate(embed.fields):
        if field.name == "Status":
            embed.set_field_at(i, name="Status", value=text, inline=False)
            return embed
    embed.add_field(name="Status", value=text, inline=False)
    return embed


class RejectModal(discord.ui.Modal, title="Reject application"):
    reason = discord.ui.TextInput(label="Reason (sent to the applicant, optional)",
                                  style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, message: discord.Message):
        super().__init__()
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        data = _load()
        app_id, app = _find_application(data, self.message.id)
        if app is None or app.get("status") != "pending":
            await interaction.response.send_message("This application was already handled.", ephemeral=True)
            return
        app["status"] = "rejected"
        app["handled_by"] = interaction.user.id
        save_json(BETA_FILE, data)
        embed = self.message.embeds[0] if self.message.embeds else discord.Embed()
        note = f"\n> {self.reason.value}" if self.reason.value else ""
        _set_status_field(embed, f"❌ Rejected by {interaction.user.mention}{note}", 0xE74C3C)
        await interaction.response.edit_message(embed=embed, view=None)
        member = interaction.guild.get_member(app["user_id"])
        if member:
            text = f"Thanks for applying to the beta program on **{interaction.guild.name}**. This time it didn't work out."
            if self.reason.value:
                text += f"\n\n**Note from the team:** {self.reason.value}"
            try:
                await member.send(text)
            except (discord.Forbidden, discord.HTTPException):
                pass
        log.info("Beta application %s rejected by %s", app_id, interaction.user)


class BetaReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only members with the Administrator permission can decide on applications.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id="beta:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = _load()
        app_id, app = _find_application(data, interaction.message.id)
        if app is None or app.get("status") != "pending":
            await interaction.response.send_message("This application was already handled.", ephemeral=True)
            return
        guild = interaction.guild
        member = guild.get_member(app["user_id"])
        if member is None:
            await interaction.response.send_message("That member isn't on the server anymore.", ephemeral=True)
            return
        role = discord.utils.get(guild.roles, name=_role_name())
        if role is None:
            await interaction.response.send_message(f"The role `{_role_name()}` is missing - run `/update`.",
                                                    ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"Beta application accepted by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't give that role - move my role above it.", ephemeral=True)
            return
        app["status"] = "accepted"
        app["handled_by"] = interaction.user.id
        save_json(BETA_FILE, data)
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        _set_status_field(embed, f"✅ Accepted by {interaction.user.mention}", 0x57F287)
        await interaction.response.edit_message(embed=embed, view=None)

        testing = discord.utils.get(guild.text_channels, name=_config().get("testing_channel", ""))
        where = f" Head over to {testing.mention} to get started." if testing else ""
        try:
            await member.send(f"\U0001F389 You're in! Your beta tester application on **{guild.name}** was accepted.{where}")
        except (discord.Forbidden, discord.HTTPException):
            pass
        log.info("Beta application %s accepted by %s", app_id, interaction.user)

        slots = data["window"].get("slots")
        if slots and _window_open(data) and _accepted_in_window(data) >= slots:
            cog = interaction.client.get_cog("Beta")
            if cog:
                await cog.close_window("all spots filled")

    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id="beta:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectModal(interaction.message))


class Beta(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(BetaPanelView())
        bot.add_view(BetaReviewView())
        self.window_loop.start()

    def cog_unload(self):
        if self.window_loop.is_running():
            self.window_loop.cancel()

    group = app_commands.Group(
        name="beta",
        description="Beta tester program (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def refresh_panels(self) -> None:
        """Edit the bot's panel message in every beta-program channel (or post it)."""
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=CHANNELS.get("beta_program", ""))
            if channel is None:
                continue
            embed = build_panel_embed()
            try:
                async for msg in channel.history(limit=30):
                    if msg.author.id == self.bot.user.id:
                        await msg.edit(embed=embed, view=BetaPanelView())
                        break
                else:
                    await channel.send(embed=embed, view=BetaPanelView())
            except discord.HTTPException:
                log.warning("Could not refresh beta panel in %s", guild.name)

    async def close_window(self, reason: str) -> None:
        data = _load()
        if not data["window"].get("open"):
            return
        data["window"]["open"] = False
        data["window"]["closed_at"] = time.time()
        data["window"]["close_reason"] = reason
        save_json(BETA_FILE, data)
        log.info("Beta applications closed (%s)", reason)
        await self.refresh_panels()

    @tasks.loop(seconds=60)
    async def window_loop(self):
        data = _load()
        w = data["window"]
        if w.get("open") and w.get("until", 0) <= time.time():
            await self.close_window("time is up")

    @window_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @group.command(name="open", description="Open beta applications for a time window.")
    @app_commands.describe(duration="How long applications stay open, e.g. 3d, 1w",
                           slots="Close automatically after this many accepted testers (optional)")
    async def open_(self, interaction: discord.Interaction, duration: str,
                    slots: app_commands.Range[int, 1, 500] | None = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta.total_seconds() < 600 or delta.days > 90:
            await interaction.response.send_message("Use a duration between 10 minutes and 90 days, e.g. `3d` or `1w`.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = _load()
        until = time.time() + delta.total_seconds()
        data["window"] = {
            "id": uuid.uuid4().hex[:6],
            "open": True,
            "until": until,
            "slots": slots,
            "opened_by": interaction.user.id,
            "opened_at": time.time(),
        }
        save_json(BETA_FILE, data)
        await self.refresh_panels()
        channel = discord.utils.get(interaction.guild.text_channels, name=CHANNELS.get("beta_program", ""))
        where = channel.mention if channel else f"#{CHANNELS.get('beta_program')}"
        await interaction.followup.send(
            f"\U0001F9EA Beta applications are open until <t:{int(until)}:f> ({format_duration(delta)})"
            + (f", max. {slots} testers" if slots else "") + f". Panel: {where}",
            ephemeral=True,
        )
        log.info("Beta applications opened by %s for %s", interaction.user, format_duration(delta))

    @group.command(name="close", description="Close beta applications now.")
    async def close(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if not _window_open(_load()):
            await interaction.response.send_message("Applications aren't open.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.close_window(f"closed by {interaction.user}")
        await interaction.followup.send("Beta applications closed.", ephemeral=True)

    @group.command(name="status", description="Show the beta application window and numbers.")
    async def status(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        w = data["window"]
        apps = [a for a in data["applications"].values() if a.get("guild_id") == interaction.guild_id]
        pending = sum(1 for a in apps if a.get("status") == "pending")
        role = discord.utils.get(interaction.guild.roles, name=_role_name())
        embed = discord.Embed(title="Beta program", color=0x1ABC9C)
        if _window_open(data):
            state = f"\U0001F7E2 Open until <t:{int(w['until'])}:f>"
            if w.get("slots"):
                state += f"\n{_accepted_in_window(data)} / {w['slots']} spots filled"
        else:
            state = "\U0001F534 Closed"
        embed.add_field(name="Applications", value=state, inline=False)
        embed.add_field(name="Pending", value=str(pending), inline=True)
        embed.add_field(name="Beta testers", value=str(len(role.members)) if role else "role missing", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="remove", description="Remove the Beta Tester role from a member.")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        role = discord.utils.get(interaction.guild.roles, name=_role_name())
        if role is None or role not in member.roles:
            await interaction.response.send_message(f"{member.mention} isn't a beta tester.", ephemeral=True)
            return
        try:
            await member.remove_roles(role, reason=f"Removed from beta by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("I can't remove that role.", ephemeral=True)
            return
        await interaction.response.send_message(f"{member.mention} is no longer a beta tester.", ephemeral=True)

    @group.command(name="panel", description="Post or refresh the beta panel in the beta-program channel.")
    async def panel(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.refresh_panels()
        await interaction.followup.send("Beta panel refreshed.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Beta(bot))
