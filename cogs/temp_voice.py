"""Join-to-create voice channels.

Joining CHANNELS["create_vc"] ("➕ Create Voice") creates a voice channel for
you in the same category and moves you into it. It's deleted as soon as it's
empty. If the owner leaves while others are still inside, ownership passes on.

Owner commands (only in your own channel):
    /vc name <name>     rename it
    /vc limit <0-99>    user limit (0 = unlimited)
    /vc lock / unlock   only people already inside (plus people you /vc allow) can join
    /vc allow @member   let someone join a locked channel
    /vc kick @member    disconnect someone from your channel
    /vc transfer @member

Channels are tracked in data/temp_voice.json so leftovers are cleaned up after
a restart. Config: SERVER_TEMPLATE["temp_voice"].
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.temp_voice")

STATE_FILE = DATA_DIR / "temp_voice.json"
CREATE_COOLDOWN_SECONDS = 15

# Names of channels being created right now - the logging cog skips them.
CREATING: set[str] = set()


def _config() -> dict:
    return SERVER_TEMPLATE.get("temp_voice", {})


def is_temp_channel(channel) -> bool:
    return str(getattr(channel, "id", "")) in load_json(STATE_FILE).get("channels", {}) or \
        getattr(channel, "name", None) in CREATING


class TempVoice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = load_json(STATE_FILE)
        self.state.setdefault("channels", {})  # channel_id -> owner_id
        self._cooldown: dict[int, float] = {}

    def _save(self) -> None:
        save_json(STATE_FILE, self.state)

    def owner_of(self, channel) -> int | None:
        return self.state["channels"].get(str(channel.id))

    @commands.Cog.listener()
    async def on_ready(self):
        # Clean up channels that were left behind while the bot was offline.
        for cid in list(self.state["channels"]):
            channel = self.bot.get_channel(int(cid))
            if channel is None:
                self.state["channels"].pop(cid, None)
            elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                try:
                    await channel.delete(reason="Empty temporary voice channel")
                except discord.HTTPException:
                    pass
                self.state["channels"].pop(cid, None)
        self._save()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not _config().get("enabled", True) or before.channel == after.channel:
            return
        if after.channel is not None and after.channel.name == CHANNELS.get("create_vc"):
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
        name = _config().get("name_format", "\U0001F50A {user}'s channel").format(user=member.display_name)[:100]
        CREATING.add(name)
        try:
            channel = await member.guild.create_voice_channel(
                name=name,
                category=hub.category,
                user_limit=int(_config().get("default_limit", 0)),
                reason=f"Temporary voice channel for {member}",
            )
            self.state["channels"][str(channel.id)] = member.id
            self._save()
            # Start from the category's permissions, then give the owner a few extras.
            await channel.set_permissions(member, connect=True, speak=True, stream=True, move_members=True,
                                          reason="Temp voice owner")
            await member.move_to(channel, reason="Join to create")
            log.info("Created temp voice channel %s for %s", channel.name, member)
        except discord.HTTPException:
            log.exception("Failed to create temp voice channel for %s", member)
        finally:
            CREATING.discard(name)

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
            self.state["channels"].pop(str(channel.id), None)
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
        try:
            await channel.set_permissions(old, overwrite=None, reason="Temp voice owner changed")
            await channel.set_permissions(new, connect=True, speak=True, stream=True, move_members=True,
                                          reason="Temp voice owner changed")
        except discord.HTTPException:
            pass

    # -------- Owner commands ------------------------------------------------

    group = app_commands.Group(name="vc", description="Manage your own voice channel.", guild_only=True)

    async def _own_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        voice = getattr(interaction.user, "voice", None)
        channel = voice.channel if voice else None
        if channel is None or self.owner_of(channel) is None:
            await interaction.response.send_message(
                f"Join **{CHANNELS.get('create_vc')}** to get your own channel, then use this inside it.", ephemeral=True
            )
            return None
        if self.owner_of(channel) != interaction.user.id:
            await interaction.response.send_message("Only the owner of this channel can do that.", ephemeral=True)
            return None
        return channel

    @group.command(name="name", description="Rename your voice channel.")
    async def name(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        try:
            await channel.edit(name=name, reason=f"Renamed by owner {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(
                "Renaming failed - Discord only allows 2 renames per 10 minutes.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"Renamed to **{name}**.", ephemeral=True)

    @group.command(name="limit", description="Set a user limit (0 = unlimited).")
    async def limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        await channel.edit(user_limit=limit, reason=f"Limit set by owner {interaction.user}")
        await interaction.response.send_message(f"User limit: **{limit or 'unlimited'}**.", ephemeral=True)

    @group.command(name="lock", description="Lock your channel - only people inside (or allowed) can join.")
    async def lock(self, interaction: discord.Interaction):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        overwrites = dict(channel.overwrites)
        for target in list(overwrites):
            if isinstance(target, discord.Role):
                ow = overwrites[target]
                ow.connect = False
                overwrites[target] = ow
        default = overwrites.get(interaction.guild.default_role, discord.PermissionOverwrite())
        default.connect = False
        overwrites[interaction.guild.default_role] = default
        for m in channel.members:
            ow = overwrites.get(m, discord.PermissionOverwrite())
            ow.connect = True
            overwrites[m] = ow
        await channel.edit(overwrites=overwrites, reason=f"Locked by owner {interaction.user}")
        await interaction.response.send_message("\U0001F512 Channel locked. Use `/vc allow` to let someone in.",
                                                ephemeral=True)

    @group.command(name="unlock", description="Unlock your channel again.")
    async def unlock(self, interaction: discord.Interaction):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        await channel.edit(sync_permissions=True, reason=f"Unlocked by owner {interaction.user}")
        await channel.set_permissions(interaction.user, connect=True, speak=True, stream=True, move_members=True)
        await interaction.response.send_message("\U0001F513 Channel unlocked.", ephemeral=True)

    @group.command(name="allow", description="Let someone join your (locked) channel.")
    async def allow(self, interaction: discord.Interaction, member: discord.Member):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        await channel.set_permissions(member, connect=True, view_channel=True, reason=f"Allowed by {interaction.user}")
        await interaction.response.send_message(f"{member.mention} can join now.", ephemeral=True)

    @group.command(name="kick", description="Disconnect someone from your channel.")
    async def kick(self, interaction: discord.Interaction, member: discord.Member):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        if member.voice is None or member.voice.channel != channel or member.id == interaction.user.id:
            await interaction.response.send_message("That member isn't in your channel.", ephemeral=True)
            return
        await channel.set_permissions(member, connect=False, reason=f"Kicked by owner {interaction.user}")
        try:
            await member.move_to(None, reason=f"Kicked from temp channel by {interaction.user}")
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"{member.mention} was removed and can't rejoin.", ephemeral=True)

    @group.command(name="transfer", description="Give your channel to someone else in it.")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member):
        channel = await self._own_channel(interaction)
        if channel is None:
            return
        if member.bot or member.voice is None or member.voice.channel != channel:
            await interaction.response.send_message("They have to be in your channel.", ephemeral=True)
            return
        await self._transfer(channel, interaction.user, member)
        await interaction.response.send_message(f"{member.mention} owns this channel now.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(TempVoice(bot))
