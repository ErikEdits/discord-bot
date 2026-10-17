"""Support waiting room (voice).

Joining CHANNELS["support_vc"] ("🎧 Support Waiting Room") sends every member
with one of the tickets.support_role_names roles a DM with a "Take over"
button (at most once per notify_cooldown_minutes per waiting member). If no
DM can be delivered, the request goes to the staff chat instead.

"Take over" creates a private voice channel (the member + the support roles),
moves the member in, and moves the staff member too if they are in a voice
channel - otherwise they get a link. The other DMs then say who took it.
Private channels are deleted when they're empty, also after a restart
(data/support_voice.json). The waiting room is created if it's missing.
Config: SERVER_TEMPLATE["support_voice"].
"""

import logging
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from cogs.common import DATA_DIR, feature_on, is_staff, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE, SUPPORT_VC_OVERWRITES

log = logging.getLogger("setup-bot.support_voice")

STATE_FILE = DATA_DIR / "support_voice.json"
MAX_DMS = 25

# Names of private channels being created right now - the logging cog skips them.
CREATING: set[str] = set()


def _config() -> dict:
    return SERVER_TEMPLATE.get("support_voice", {})


def _support_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


def is_support_channel(channel) -> bool:
    return str(getattr(channel, "id", "")) in load_json(STATE_FILE).get("channels", {}) or \
        getattr(channel, "name", None) in CREATING


def _waiting_room(guild: discord.Guild) -> discord.VoiceChannel | None:
    return discord.utils.get(guild.voice_channels, name=CHANNELS["support_vc"])


def _request_embed(guild: discord.Guild, member: discord.abc.User, status: str, staff=None) -> discord.Embed:
    room = CHANNELS["support_vc"]
    if status == "waiting":
        embed = discord.Embed(title="\U0001F3A7 Someone needs help",
                              description=f"{member.mention} (**{member}**) is waiting in **{room}** on **{guild.name}**.",
                              color=0xF1C40F)
        embed.set_footer(text="Take over: the bot makes a private voice channel and moves them in.")
    elif status == "left":
        embed = discord.Embed(title="\U0001F3A7 Support request",
                              description=f"{member.mention} (**{member}**) left the waiting room.", color=0x95A5A6)
    else:
        embed = discord.Embed(title="\U0001F3A7 Support request",
                              description=f"✅ {staff.mention} took over {member.mention} (**{member}**).",
                              color=0x2ECC71)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


class TakeOverButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"supportvc:take:(?P<guild>\d+):(?P<member>\d+)"):
    """"Take over" in the staff DMs. Works across restarts (the IDs are in the custom_id)."""

    def __init__(self, guild_id: int, member_id: int):
        self.guild_id = guild_id
        self.member_id = member_id
        super().__init__(discord.ui.Button(
            label="Take over",
            emoji="\U0001F64B",
            style=discord.ButtonStyle.success,
            custom_id=f"supportvc:take:{guild_id}:{member_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), int(match["member"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SupportVoice")
        if cog is None:
            await interaction.response.send_message("The support waiting room is turned off.", ephemeral=True)
            return
        await cog.take_over(interaction, self.guild_id, self.member_id)


def _take_view(guild_id: int, member_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TakeOverButton(guild_id, member_id))
    return view


class SupportVoice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = load_json(STATE_FILE)
        self.state.setdefault("channels", {})  # channel_id -> {"member": id, "staff": id}
        # member_id -> {"guild", "messages": [(channel_id, message_id)], "status", "ts"}
        self.requests: dict[int, dict] = {}
        bot.add_dynamic_items(TakeOverButton)

    def _save(self) -> None:
        save_json(STATE_FILE, self.state)

    @commands.Cog.listener()
    async def on_ready(self):
        if not _config().get("enabled", True):
            return
        for guild in self.bot.guilds:
            await self._ensure_waiting_room(guild)
        # Private channels left behind while the bot was offline.
        for cid in list(self.state["channels"]):
            channel = self.bot.get_channel(int(cid))
            if isinstance(channel, discord.VoiceChannel) and channel.members:
                continue
            if channel is not None:
                try:
                    await channel.delete(reason="Empty support voice channel")
                except discord.HTTPException:
                    continue
            self.state["channels"].pop(cid, None)
        self._save()

    async def _ensure_waiting_room(self, guild: discord.Guild) -> None:
        if _waiting_room(guild) is not None:
            return
        hub = discord.utils.get(guild.voice_channels, name=CHANNELS.get("create_vc"))
        category = hub.category if hub else None
        if category is None:
            return  # no voice category (server not set up) - /setup creates it
        overwrites = {}
        for name, perms in SUPPORT_VC_OVERWRITES.items():
            target = guild.default_role if name == "@everyone" else discord.utils.get(guild.roles, name=name)
            if target is not None:
                overwrites[target] = discord.PermissionOverwrite(**perms)
        try:
            await guild.create_voice_channel(CHANNELS["support_vc"], category=category, overwrites=overwrites,
                                             position=hub.position + 1, reason="Support waiting room")
            log.info("Created the support waiting room in %s", guild.name)
        except discord.HTTPException:
            log.exception("Could not create the support waiting room in %s", guild.name)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not _config().get("enabled", True) or before.channel == after.channel or member.bot:
            return
        if not feature_on("support_voice"):
            return
        room = CHANNELS["support_vc"]
        if after.channel is not None and after.channel.name == room and not is_staff(member, _support_roles()):
            await self._member_waiting(member, after.channel)
        if before.channel is not None and before.channel.name == room:
            await self._member_left(member)
        if before.channel is not None and str(before.channel.id) in self.state["channels"] and not before.channel.members:
            try:
                await before.channel.delete(reason="Empty support voice channel")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Could not delete support voice channel %s", before.channel.name)
                return
            self.state["channels"].pop(str(before.channel.id), None)
            self._save()

    async def _member_waiting(self, member: discord.Member, room: discord.VoiceChannel) -> None:
        cooldown = float(_config().get("notify_cooldown_minutes", 10)) * 60
        req = self.requests.get(member.id)
        if req and req["status"] != "taken" and time.monotonic() - req["ts"] < cooldown:
            # Came back soon after leaving: the DMs from before work again.
            if req["status"] == "left":
                req["status"] = "waiting"
                await self._update_messages(member, req)
            return
        messages = await self._notify_staff(member)
        self.requests[member.id] = {"guild": member.guild.id, "messages": messages,
                                    "status": "waiting", "ts": time.monotonic()}
        text = ("The team has been notified - someone will be with you soon." if messages
                else "Nobody from the team could be reached right now - please open a ticket instead.")
        try:
            await room.send(f"{member.mention} {text}", delete_after=120)
        except discord.HTTPException:
            pass

    async def _notify_staff(self, member: discord.Member) -> list[tuple[int, int]]:
        guild = member.guild
        names = set(_support_roles())
        staff = [m for m in guild.members if not m.bot and any(r.name in names for r in m.roles)]
        embed = _request_embed(guild, member, "waiting")
        sent = []
        for person in staff[:MAX_DMS]:
            try:
                msg = await person.send(embed=embed, view=_take_view(guild.id, member.id))
                sent.append((msg.channel.id, msg.id))
            except discord.HTTPException:
                pass
        if not sent:
            # Nobody has DMs open - put it in the staff chat instead.
            chat = discord.utils.get(guild.text_channels, name=CHANNELS.get("staff_chat"))
            if chat is not None:
                try:
                    msg = await chat.send(embed=embed, view=_take_view(guild.id, member.id))
                    sent.append((msg.channel.id, msg.id))
                except discord.HTTPException:
                    pass
        log.info("Support waiting room: %s is waiting, notified %d", member, len(sent))
        return sent

    async def _member_left(self, member: discord.Member) -> None:
        req = self.requests.get(member.id)
        if req and req["status"] == "waiting":
            req["status"] = "left"
            await self._update_messages(member, req)

    async def _update_messages(self, member, req: dict, staff=None, skip: int | None = None) -> None:
        guild = self.bot.get_guild(req["guild"])
        if guild is None:
            return
        embed = _request_embed(guild, member, req["status"], staff)
        view = _take_view(guild.id, member.id) if req["status"] == "waiting" else None
        for channel_id, message_id in req["messages"]:
            if message_id == skip:
                continue
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                await channel.get_partial_message(message_id).edit(embed=embed, view=view)
            except discord.HTTPException:
                pass

    async def take_over(self, interaction: discord.Interaction, guild_id: int, member_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        staff = guild.get_member(interaction.user.id) if guild else None
        if staff is None or not is_staff(staff, _support_roles()):
            await interaction.response.send_message("Only the team can take over support requests.", ephemeral=True)
            return
        member = guild.get_member(member_id)
        req = self.requests.get(member_id)
        if req and req["status"] == "taken":
            taker = guild.get_member(req.get("staff", 0))
            await interaction.response.edit_message(
                embed=_request_embed(guild, member or interaction.user, "taken", taker or interaction.user), view=None)
            return
        room = _waiting_room(guild)
        if member is None:
            await interaction.response.edit_message(
                embed=discord.Embed(description="They're not on the server anymore.", color=0x95A5A6), view=None)
            return
        if room is None or member.voice is None or member.voice.channel != room:
            await interaction.response.edit_message(embed=_request_embed(guild, member, "left"), view=None)
            return
        await interaction.response.defer()
        channel = await self._create_private(guild, member, staff, room)
        if channel is None:
            await interaction.followup.send("Couldn't start the support call - they left the waiting room, "
                                            "or the bot is missing permissions.", ephemeral=True)
            return
        if req is None:
            req = self.requests[member_id] = {"guild": guild.id, "messages": [], "ts": time.monotonic()}
        req.update(status="taken", staff=staff.id)
        moved = False
        if staff.voice is not None:
            try:
                await staff.move_to(channel, reason="Support call")
                moved = True
            except discord.HTTPException:
                pass
        embed = _request_embed(guild, member, "taken", staff)
        embed.add_field(name="Channel", value=channel.mention if moved else
                        f"{channel.mention} - join it now (you weren't in a voice channel, so I couldn't move you).")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open channel", url=channel.jump_url))
        await interaction.edit_original_response(embed=embed, view=view)
        await self._update_messages(member, req, staff, skip=interaction.message.id if interaction.message else None)
        try:
            await channel.send(f"\U0001F3A7 {member.mention} {staff.mention} Support call started. "
                               "This channel is deleted when it's empty.")
        except discord.HTTPException:
            pass
        log.info("Support: %s took over %s (%s)", staff, member, channel.name)

    async def _create_private(self, guild: discord.Guild, member: discord.Member, staff: discord.Member,
                              room: discord.VoiceChannel) -> discord.VoiceChannel | None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
            member: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True,
                                                use_voice_activation=True),
            staff: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True),
        }
        for name in _support_roles():
            if (role := discord.utils.get(guild.roles, name=name)) is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True,
                                                               move_members=True)
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, send_messages=True,
                                                               move_members=True)
        name = _config().get("name_format", "\U0001F3A7 Support: {user}").format(user=member.display_name)[:100]
        CREATING.add(name)
        try:
            channel = await guild.create_voice_channel(name, category=room.category, overwrites=overwrites,
                                                       reason=f"Support call for {member} (by {staff})")
            self.state["channels"][str(channel.id)] = {"member": member.id, "staff": staff.id}
            self._save()
        except discord.HTTPException:
            log.exception("Could not create a support voice channel for %s", member)
            return None
        finally:
            CREATING.discard(name)
        try:
            await member.move_to(channel, reason=f"Support call (taken over by {staff})")
        except discord.HTTPException:
            # They left in the meantime - nobody will ever join, so don't leave it behind.
            try:
                await channel.delete(reason="Support call could not start")
            except discord.HTTPException:
                pass
            self.state["channels"].pop(str(channel.id), None)
            self._save()
            return None
        return channel


async def setup(bot):
    await bot.add_cog(SupportVoice(bot))
