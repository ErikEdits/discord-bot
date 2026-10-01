"""Unlock single admin commands for members, without giving them a role (server owner only).

    /grant add member command      e.g. /grant add @Colin mclog   or   /grant add @Colin poll create
    /grant remove member command
    /grant list [member]

Granting a group ("mclog") unlocks all its subcommands; "mclog ask" only that one.
The bot's own permission checks (cogs/common.py: is_admin, has_perm, is_staff)
accept granted members while that command runs. Discord still hides admin
commands from members without the permission, so the owner also ticks the member
once under Server Settings -> Integrations -> the bot -> the command (Discord
doesn't let bots set that themselves). Stored in data/grants.json.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import GRANTS_FILE, load_grants, save_json

log = logging.getLogger("setup-bot.grants")

NOT_GRANTABLE = {"grant"}


def _restricted(command) -> bool:
    perms = getattr(command, "default_permissions", None)
    return perms is not None and perms.value != 0


def grantable_commands(tree: app_commands.CommandTree) -> list[str]:
    """All slash commands and subcommands, e.g. ["mclog", "mclog ask", "poll", ...]. Some
    admin commands are visible to everyone and only check inside, so all are offered."""
    names = []

    def walk(command):
        names.append(command.qualified_name)
        for sub in getattr(command, "commands", []):
            walk(sub)

    for command in tree.get_commands():
        if isinstance(command, (app_commands.Command, app_commands.Group)) and command.name not in NOT_GRANTABLE:
            walk(command)
    return sorted(names)


def hidden_from_members(tree: app_commands.CommandTree, top_level: str) -> bool:
    command = tree.get_command(top_level)
    return command is not None and _restricted(command)


def _is_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id


class Grants(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(
        name="grant",
        description="Unlock admin commands for single members (server owner only).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def _command_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.lower().lstrip("/")
        return [app_commands.Choice(name=f"/{n}", value=n)
                for n in grantable_commands(self.bot.tree) if current in n][:25]

    async def _granted_autocomplete(self, interaction: discord.Interaction, current: str):
        member_id = getattr(interaction.namespace, "member", None)
        member_id = getattr(member_id, "id", member_id)
        granted = load_grants().get(str(interaction.guild_id), {}).get(str(member_id), [])
        return [app_commands.Choice(name=f"/{n}", value=n) for n in granted if current.lower().lstrip("/") in n][:25]

    @group.command(name="add", description="Unlock an admin command for a member.")
    @app_commands.describe(member="Who gets the command", command="The command, e.g. mclog or poll create")
    @app_commands.autocomplete(command=_command_autocomplete)
    async def add(self, interaction: discord.Interaction, member: discord.Member, command: str):
        if not _is_owner(interaction):
            await interaction.response.send_message("Only the server owner can unlock commands.", ephemeral=True)
            return
        command = " ".join(command.lower().lstrip("/").split())
        if command not in grantable_commands(self.bot.tree):
            await interaction.response.send_message(
                f"`/{command}` isn't an admin command I can unlock. Pick one from the list.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Bots can't get commands.", ephemeral=True)
            return
        data = load_grants()
        granted = data.setdefault(str(interaction.guild_id), {}).setdefault(str(member.id), [])
        if command in granted:
            await interaction.response.send_message(f"{member.mention} already has `/{command}`.", ephemeral=True)
            return
        granted.append(command)
        granted.sort()
        save_json(GRANTS_FILE, data)
        log.info("%s unlocked /%s for %s", interaction.user, command, member)
        top = command.split()[0]
        text = f"{member.mention} can now use `/{command}`."
        hidden = hidden_from_members(self.bot.tree, top)
        if hidden:
            text += (
                f"\n\n**One more step** (Discord doesn't let bots do this): so the command shows up in "
                f"{member.display_name}'s command list:\n"
                f"1. Server Settings → **Integrations** → **{self.bot.user.name if self.bot.user else 'the bot'}**\n"
                f"2. Click the command **/{top}**\n"
                f"3. **Add Roles or Members** → pick **{member.display_name}** → make sure it's ✅ → **Save**"
            )
        embed = discord.Embed(title=f"✅ /{command} unlocked for {member.display_name}", description=text,
                              color=0x57F287)
        if hidden and " " in command:
            embed.set_footer(text=f"Discord shows the whole /{top} group - the bot only allows /{command}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="remove", description="Take an unlocked command away again.")
    @app_commands.describe(member="Whose command", command="The unlocked command")
    @app_commands.autocomplete(command=_granted_autocomplete)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, command: str):
        if not _is_owner(interaction):
            await interaction.response.send_message("Only the server owner can change unlocked commands.",
                                                    ephemeral=True)
            return
        command = " ".join(command.lower().lstrip("/").split())
        data = load_grants()
        granted = data.get(str(interaction.guild_id), {}).get(str(member.id), [])
        if command not in granted:
            await interaction.response.send_message(f"{member.mention} doesn't have `/{command}` unlocked.",
                                                    ephemeral=True)
            return
        granted.remove(command)
        if not granted:
            data[str(interaction.guild_id)].pop(str(member.id), None)
        save_json(GRANTS_FILE, data)
        log.info("%s removed /%s from %s", interaction.user, command, member)
        await interaction.response.send_message(
            f"\U0001F512 `/{command}` removed from {member.mention}. You can also untick them again under "
            f"Server Settings → Integrations.", ephemeral=True)

    @group.command(name="list", description="Show who has which admin commands unlocked.")
    @app_commands.describe(member="Only this member (optional)")
    async def list_(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not _is_owner(interaction):
            await interaction.response.send_message("Only the server owner can see unlocked commands.",
                                                    ephemeral=True)
            return
        guild_grants = load_grants().get(str(interaction.guild_id), {})
        if member is not None:
            guild_grants = {str(member.id): guild_grants.get(str(member.id), [])}
        lines = [f"<@{uid}>: " + ", ".join(f"`/{c}`" for c in cmds) for uid, cmds in guild_grants.items() if cmds]
        embed = discord.Embed(title="Unlocked commands", description="\n".join(lines)[:4000] or "Nothing unlocked.",
                              color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Grants(bot))
