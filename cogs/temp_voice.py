"""Join-to-create voice channels.

Joining CHANNELS["create_vc"] ("➕ Create Voice") creates a voice channel for
you in the same category and moves you into it. It's deleted as soon as it's
empty. If the owner leaves while others are still inside, ownership passes on.

Every new channel gets a control panel (buttons) in its text chat. Only the
owner can use it. The same things work as commands:
    /vc name <name>     rename it (the name is remembered for your next channel)
    /vc limit <0-99>    user limit (0 = unlimited)
    /vc lock / unlock   only people already inside (plus people you allow) can join
    /vc hide / show     make the channel invisible for everyone else
    /vc allow @member   let someone join (and see) a locked / hidden channel
    /vc kick @member    disconnect someone - they can't rejoin this channel
    /vc transfer @member

Channels, their lock/hide state and remembered names are kept in
data/temp_voice.json, so leftovers are cleaned up after a restart.
Config: SERVER_TEMPLATE["temp_voice"].
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, feature_on, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.temp_voice")

STATE_FILE = DATA_DIR / "temp_voice.json"
CREATE_COOLDOWN_SECONDS = 15
# Discord allows 2 channel renames per 10 minutes. Waiting longer would block the bot.
RENAMES_PER_WINDOW = 2
RENAME_WINDOW_SECONDS = 600

# Names of channels being created right now - the logging cog skips them.
CREATING: set[str] = set()


def _config() -> dict:
    return SERVER_TEMPLATE.get("temp_voice", {})


def _default_name(member: discord.Member) -> str:
    return _config().get("name_format", "\U0001F50A {user}'s channel").format(user=member.display_name)[:100]


def is_temp_channel(channel) -> bool:
    return str(getattr(channel, "id", "")) in load_json(STATE_FILE).get("channels", {}) or \
        getattr(channel, "name", None) in CREATING


def _cog(interaction: discord.Interaction) -> "TempVoice | None":
    return interaction.client.get_cog("TempVoice")


# -------- Control panel ---------------------------------------------------

class RenameModal(discord.ui.Modal, title="Rename your channel"):
    name = discord.ui.TextInput(label="New name (empty = default name)", max_length=90, required=False)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel
        self.name.default = channel.name

    async def on_submit(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is not None and await cog._own_channel(interaction, self.channel):
            await cog.rename(interaction, self.channel, str(self.name.value).strip())


class LimitModal(discord.ui.Modal, title="User limit"):
    limit = discord.ui.TextInput(label="Max. people (0 = unlimited)", max_length=2)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel
        self.limit.default = str(channel.user_limit)

    async def on_submit(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None or not await cog._own_channel(interaction, self.channel):
            return
        value = str(self.limit.value).strip()
        if not value.isdigit() or int(value) > 99:
            await interaction.response.send_message("Please enter a number from 0 to 99.", ephemeral=True)
            return
        await cog.set_limit(interaction, self.channel, int(value))


class AllowSelect(discord.ui.UserSelect):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(placeholder="Who may join?", min_values=1, max_values=5)
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is not None and await cog._own_channel(interaction, self.channel):
            members = [m for m in self.values if isinstance(m, discord.Member) and not m.bot]
            await cog.allow_members(interaction, self.channel, members)


class MemberPick(discord.ui.Select):
    """Pick someone who is in the channel right now (for kick / transfer)."""

    def __init__(self, channel: discord.VoiceChannel, members: list[discord.Member], action: str):
        super().__init__(
            placeholder="Kick who?" if action == "kick" else "Give the channel to?",
            options=[discord.SelectOption(label=m.display_name[:100], value=str(m.id), description=str(m)[:100])
                     for m in members[:25]],
        )
        self.channel = channel
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None or not await cog._own_channel(interaction, self.channel):
            return
        member = self.channel.guild.get_member(int(self.values[0]))
        if member is None:
            await interaction.response.send_message("That member isn't here anymore.", ephemeral=True)
        elif self.action == "kick":
            await cog.kick_member(interaction, self.channel, member)
        else:
            await cog.transfer_to(interaction, self.channel, member)


def _picker(item: discord.ui.Item) -> discord.ui.View:
    view = discord.ui.View(timeout=120)
    view.add_item(item)
    return view


class PanelView(discord.ui.View):
    """Buttons in the text chat of every temp channel. Persistent - works after a restart."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _channel(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Temporary voice channels are turned off.", ephemeral=True)
            return None
        return await cog._own_channel(interaction, interaction.channel)

    @discord.ui.button(label="Lock", emoji="\U0001F512", style=discord.ButtonStyle.secondary, custom_id="tvc:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await _cog(interaction).set_flag(interaction, channel, "locked", True)

    @discord.ui.button(label="Unlock", emoji="\U0001F513", style=discord.ButtonStyle.secondary, custom_id="tvc:unlock", row=0)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await _cog(interaction).set_flag(interaction, channel, "locked", False)

    @discord.ui.button(label="Hide", emoji="\U0001F47B", style=discord.ButtonStyle.secondary, custom_id="tvc:hide", row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await _cog(interaction).set_flag(interaction, channel, "hidden", True)

    @discord.ui.button(label="Show", emoji="\U0001F441", style=discord.ButtonStyle.secondary, custom_id="tvc:show", row=0)
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await _cog(interaction).set_flag(interaction, channel, "hidden", False)

    @discord.ui.button(label="Rename", emoji="✏", style=discord.ButtonStyle.primary, custom_id="tvc:rename", row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await interaction.response.send_modal(RenameModal(channel))

    @discord.ui.button(label="Limit", emoji="\U0001F465", style=discord.ButtonStyle.primary, custom_id="tvc:limit", row=1)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await interaction.response.send_modal(LimitModal(channel))

    @discord.ui.button(label="Allow", emoji="➕", style=discord.ButtonStyle.success, custom_id="tvc:allow", row=1)
    async def allow(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await interaction.response.send_message("Who may join your channel (even when it's locked or hidden)?",
                                                    view=_picker(AllowSelect(channel)), ephemeral=True)

    @discord.ui.button(label="Kick", emoji="\U0001F462", style=discord.ButtonStyle.danger, custom_id="tvc:kick", row=2)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await self._pick(interaction, channel, "kick")

    @discord.ui.button(label="Transfer", emoji="\U0001F451", style=discord.ButtonStyle.secondary, custom_id="tvc:transfer", row=2)
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if channel := await self._channel(interaction):
            await self._pick(interaction, channel, "transfer")

    async def _pick(self, interaction: discord.Interaction, channel: discord.VoiceChannel, action: str):
        others = [m for m in channel.members if not m.bot and m.id != interaction.user.id]
        if not others:
            await interaction.response.send_message("Nobody else is in your channel.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Who should be kicked? They can't rejoin this channel." if action == "kick" else "Who gets the channel?",
            view=_picker(MemberPick(channel, others, action)), ephemeral=True)


class TempVoice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = load_json(STATE_FILE)
        self.state.setdefault("channels", {})  # channel_id -> owner_id
        self.state.setdefault("settings", {})  # channel_id -> {locked, hidden, allowed, kicked, panel}
        self.state.setdefault("names", {})     # user_id -> remembered channel name
        self._cooldown: dict[int, float] = {}
        self._renames: dict[int, list[float]] = {}
        bot.add_view(PanelView())

    def _save(self) -> None:
        save_json(STATE_FILE, self.state)

    def owner_of(self, channel) -> int | None:
        return self.state["channels"].get(str(channel.id))

    def _settings(self, channel) -> dict:
        s = self.state["settings"].setdefault(str(channel.id), {})
        for key, default in (("locked", False), ("hidden", False), ("allowed", []), ("kicked", [])):
            s.setdefault(key, default)
        return s

    def _forget(self, channel_id: str) -> None:
        self.state["channels"].pop(channel_id, None)
        self.state["settings"].pop(channel_id, None)
        self._renames.pop(int(channel_id), None)

    @commands.Cog.listener()
    async def on_ready(self):
        # Clean up channels that were left behind while the bot was offline.
        for cid in list(self.state["channels"]):
            channel = self.bot.get_channel(int(cid))
            if channel is None:
                self._forget(cid)
            elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                try:
                    await channel.delete(reason="Empty temporary voice channel")
                except discord.HTTPException:
                    pass
                self._forget(cid)
        for cid in list(self.state["settings"]):
            if cid not in self.state["channels"]:
                self.state["settings"].pop(cid, None)
        self._save()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not _config().get("enabled", True) or before.channel == after.channel:
            return
        if after.channel is not None and after.channel.name == CHANNELS.get("create_vc") and feature_on("temp_voice"):
            await self._create_for(member, after.channel)
        if before.channel is not None and self.owner_of(before.channel) is not None:
            await self._handle_leave(member, before.channel)

    async def _create_for(self, member: discord.Member, hub: discord.VoiceChannel) -> None:
        now = time.monotonic()
        if now - self._cooldown.get(member.id, 0) < CREATE_COOLDOWN_SECONDS:
            try:
                await member.move_to(None, reason="Temp voice cooldown")
            except discord.HTTPException:
                pass
            return
        self._cooldown[member.id] = now
        # Already owns one? Move them back there instead of making a second.
        for cid, owner in self.state["channels"].items():
            if owner == member.id:
                existing = self.bot.get_channel(int(cid))
                if isinstance(existing, discord.VoiceChannel):
                    try:
                        await member.move_to(existing)
                    except discord.HTTPException:
                        pass
                    return
        name = (self.state["names"].get(str(member.id)) or _default_name(member))[:100]
        CREATING.add(name)
        try:
            channel = await member.guild.create_voice_channel(
                name=name,
                category=hub.category,
                user_limit=int(_config().get("default_limit", 0)),
                reason=f"Temporary voice channel for {member}",
            )
            self.state["channels"][str(channel.id)] = member.id
            self._settings(channel)
            self._save()
            # Start from the category's permissions, then give the owner a few extras.
            await channel.set_permissions(member, connect=True, speak=True, stream=True, move_members=True,
                                          view_channel=True, reason="Temp voice owner")
            await member.move_to(channel, reason="Join to create")
            log.info("Created temp voice channel %s for %s", channel.name, member)
        except discord.HTTPException:
            log.exception("Failed to create temp voice channel for %s", member)
            return
        finally:
            CREATING.discard(name)
        await self._post_panel(channel, member)

    async def _post_panel(self, channel: discord.VoiceChannel, owner: discord.Member) -> None:
        try:
            msg = await channel.send(content=owner.mention, embed=self._panel_embed(channel), view=PanelView())
        except discord.HTTPException:
            log.warning("Could not post the control panel in %s", channel.name)
            return
        self._settings(channel)["panel"] = msg.id
        self._save()

    def _panel_embed(self, channel: discord.VoiceChannel) -> discord.Embed:
        s = self._settings(channel)
        owner = self.owner_of(channel)
        embed = discord.Embed(
            title="\U0001F39B Channel controls",
            description=(f"Owner: <@{owner}>\nOnly the owner can use these buttons. "
                         "The name you give the channel is remembered for next time."),
            color=0x5865F2,
        )
        embed.add_field(name="Access", value="\U0001F512 Locked" if s["locked"] else "\U0001F513 Open")
        embed.add_field(name="Visibility", value="\U0001F47B Hidden" if s["hidden"] else "\U0001F441 Visible")
        embed.add_field(name="Limit", value=str(channel.user_limit or "unlimited"))
        return embed

    async def _refresh_panel(self, channel: discord.VoiceChannel) -> None:
        msg_id = self._settings(channel).get("panel")
        if not msg_id:
            return
        try:
            await channel.get_partial_message(msg_id).edit(embed=self._panel_embed(channel))
        except discord.HTTPException:
            pass

    async def _handle_leave(self, member: discord.Member, channel: discord.VoiceChannel) -> None:
        if not channel.members:
            try:
                await channel.delete(reason="Empty temporary voice channel")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Could not delete temp voice channel %s", channel.name)
                return
            # Forget it only after the delete, so the logging cog still recognises it.
            self._forget(str(channel.id))
            self._save()
            return
        if self.owner_of(channel) == member.id:
            new_owner = next((m for m in channel.members if not m.bot), None)
            if new_owner is None:
                return
            await self._transfer(channel, member, new_owner)
            try:
                await channel.send(f"\U0001F451 {new_owner.mention} is now the owner of this channel.")
            except discord.HTTPException:
                pass

    async def _transfer(self, channel: discord.VoiceChannel, old: discord.Member, new: discord.Member) -> None:
        self.state["channels"][str(channel.id)] = new.id
        self._save()
        await self._apply(channel)
        await self._refresh_panel(channel)

    def _overwrites(self, channel: discord.VoiceChannel) -> dict:
        """The channel's permissions from scratch: category, then lock/hide, allowed, kicked, owner."""
        guild = channel.guild
        s = self._settings(channel)
        base = channel.category.overwrites if channel.category else {}
        ow = {t: discord.PermissionOverwrite.from_pair(*o.pair()) for t, o in base.items()}

        def change(target, **perms):
            o = ow.get(target) or discord.PermissionOverwrite()
            o.update(**perms)
            ow[target] = o

        closed = {}
        if s["locked"]:
            closed["connect"] = False
        if s["hidden"]:
            closed["view_channel"] = False
        if closed:
            for target in [t for t in ow if isinstance(t, discord.Role)] + [guild.default_role]:
                change(target, **closed)
            for m in channel.members:
                change(m, connect=True, view_channel=True)
        for uid in s["allowed"]:
            if (m := guild.get_member(uid)) is not None:
                change(m, connect=True, view_channel=True)
        for uid in s["kicked"]:
            if (m := guild.get_member(uid)) is not None:
                change(m, connect=False)
        if (owner := guild.get_member(self.owner_of(channel) or 0)) is not None:
            change(owner, connect=True, speak=True, stream=True, move_members=True, view_channel=True)
        if guild.me is not None:
            change(guild.me, view_channel=True, connect=True, send_messages=True)
        return ow

    async def _apply(self, channel: discord.VoiceChannel) -> bool:
        try:
            await channel.edit(overwrites=self._overwrites(channel), reason="Temp voice settings changed by owner")
            return True
        except discord.HTTPException:
            log.warning("Could not update the permissions of temp voice channel %s", channel.name)
            return False

    # -------- Owner actions (panel and commands) ---------------------------

    async def _own_channel(self, interaction: discord.Interaction, channel=None) -> discord.VoiceChannel | None:
        if channel is None:
            voice = getattr(interaction.user, "voice", None)
            channel = voice.channel if voice else None
        if not isinstance(channel, discord.VoiceChannel) or self.owner_of(channel) is None:
            await interaction.response.send_message(
                f"Join **{CHANNELS.get('create_vc')}** to get your own channel, then use this inside it.", ephemeral=True
            )
            return None
        if self.owner_of(channel) != interaction.user.id:
            await interaction.response.send_message("Only the owner of this channel can do that.", ephemeral=True)
            return None
        return channel

    async def _done(self, interaction: discord.Interaction, channel: discord.VoiceChannel, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
        await self._refresh_panel(channel)

    async def rename(self, interaction: discord.Interaction, channel: discord.VoiceChannel, name: str) -> None:
        member = interaction.user
        new_name = name or _default_name(member)
        if new_name == channel.name:
            if name:
                self.state["names"][str(member.id)] = new_name
            else:
                self.state["names"].pop(str(member.id), None)
            self._save()
            await self._done(interaction, channel, f"The name is already **{new_name}**.")
            return
        now = time.monotonic()
        recent = [t for t in self._renames.get(channel.id, []) if now - t < RENAME_WINDOW_SECONDS]
        if len(recent) >= RENAMES_PER_WINDOW:
            wait = int(RENAME_WINDOW_SECONDS - (now - recent[0])) // 60 + 1
            await self._done(interaction, channel,
                             f"Discord only allows 2 renames per 10 minutes. Try again in about {wait} min.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await channel.edit(name=new_name, reason=f"Renamed by owner {member}")
        except discord.HTTPException:
            await self._done(interaction, channel, "Renaming failed - please try again later.")
            return
        self._renames[channel.id] = recent + [now]
        if name:
            self.state["names"][str(member.id)] = new_name
            note = "Your next channel gets this name too."
        else:
            self.state["names"].pop(str(member.id), None)
            note = "Back to the default name - nothing is remembered."
        self._save()
        await self._done(interaction, channel, f"Renamed to **{new_name}**. {note}")

    async def set_limit(self, interaction: discord.Interaction, channel: discord.VoiceChannel, limit: int) -> None:
        await channel.edit(user_limit=limit, reason=f"Limit set by owner {interaction.user}")
        await self._done(interaction, channel, f"User limit: **{limit or 'unlimited'}**.")

    async def set_flag(self, interaction: discord.Interaction, channel: discord.VoiceChannel,
                       flag: str, value: bool) -> None:
        s = self._settings(channel)
        s[flag] = value
        self._save()
        if not await self._apply(channel):
            await self._done(interaction, channel, "That didn't work - the bot is missing permissions.")
            return
        text = {
            ("locked", True): "\U0001F512 Channel locked. Only people inside and people you allow can join.",
            ("locked", False): "\U0001F513 Channel unlocked.",
            ("hidden", True): "\U0001F47B Channel hidden. Only people inside and people you allow can see it.",
            ("hidden", False): "\U0001F441 Channel visible again.",
        }[(flag, value)]
        await self._done(interaction, channel, text)

    async def allow_members(self, interaction: discord.Interaction, channel: discord.VoiceChannel,
                            members: list[discord.Member]) -> None:
        if not members:
            await self._done(interaction, channel, "Pick at least one member (no bots).")
            return
        s = self._settings(channel)
        for m in members:
            if m.id not in s["allowed"]:
                s["allowed"].append(m.id)
            if m.id in s["kicked"]:
                s["kicked"].remove(m.id)
        self._save()
        await self._apply(channel)
        names = ", ".join(m.mention for m in members)
        await self._done(interaction, channel, f"{names} can join now.")

    async def kick_member(self, interaction: discord.Interaction, channel: discord.VoiceChannel,
                          member: discord.Member) -> None:
        if member.voice is None or member.voice.channel != channel or member.id == interaction.user.id:
            await self._done(interaction, channel, "That member isn't in your channel.")
            return
        s = self._settings(channel)
        if member.id in s["allowed"]:
            s["allowed"].remove(member.id)
        if member.id not in s["kicked"]:
            s["kicked"].append(member.id)
        self._save()
        try:
            await member.move_to(None, reason=f"Kicked from temp channel by {interaction.user}")
        except discord.HTTPException:
            pass
        await self._apply(channel)
        await self._done(interaction, channel, f"{member.mention} was removed and can't rejoin.")

    async def transfer_to(self, interaction: discord.Interaction, channel: discord.VoiceChannel,
                          member: discord.Member) -> None:
        if member.bot or member.voice is None or member.voice.channel != channel:
            await self._done(interaction, channel, "They have to be in your channel.")
            return
        await self._transfer(channel, interaction.user, member)
        await self._done(interaction, channel, f"{member.mention} owns this channel now.")
        try:
            await channel.send(f"\U0001F451 {member.mention} is now the owner of this channel.")
        except discord.HTTPException:
            pass

    # -------- Owner commands ------------------------------------------------

    group = app_commands.Group(name="vc", description="Manage your own voice channel.", guild_only=True)

    @group.command(name="name", description="Rename your voice channel (remembered for next time).")
    async def name(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        if channel := await self._own_channel(interaction):
            await self.rename(interaction, channel, name.strip())

    @group.command(name="limit", description="Set a user limit (0 = unlimited).")
    async def limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        if channel := await self._own_channel(interaction):
            await self.set_limit(interaction, channel, limit)

    @group.command(name="lock", description="Lock your channel - only people inside (or allowed) can join.")
    async def lock(self, interaction: discord.Interaction):
        if channel := await self._own_channel(interaction):
            await self.set_flag(interaction, channel, "locked", True)

    @group.command(name="unlock", description="Unlock your channel again.")
    async def unlock(self, interaction: discord.Interaction):
        if channel := await self._own_channel(interaction):
            await self.set_flag(interaction, channel, "locked", False)

    @group.command(name="hide", description="Make your channel invisible - only people inside (or allowed) see it.")
    async def hide(self, interaction: discord.Interaction):
        if channel := await self._own_channel(interaction):
            await self.set_flag(interaction, channel, "hidden", True)

    @group.command(name="show", description="Make your channel visible again.")
    async def show(self, interaction: discord.Interaction):
        if channel := await self._own_channel(interaction):
            await self.set_flag(interaction, channel, "hidden", False)

    @group.command(name="allow", description="Let someone join your (locked or hidden) channel.")
    async def allow(self, interaction: discord.Interaction, member: discord.Member):
        if channel := await self._own_channel(interaction):
            await self.allow_members(interaction, channel, [member])

    @group.command(name="kick", description="Disconnect someone from your channel.")
    async def kick(self, interaction: discord.Interaction, member: discord.Member):
        if channel := await self._own_channel(interaction):
            await self.kick_member(interaction, channel, member)

    @group.command(name="transfer", description="Give your channel to someone else in it.")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member):
        if channel := await self._own_channel(interaction):
            await self.transfer_to(interaction, channel, member)


async def setup(bot):
    await bot.add_cog(TempVoice(bot))
