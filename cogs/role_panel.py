"""Self-assign role panel (buttons that toggle a role). Not an extension.

The panel's title, text and buttons come from data/role_panel.json when it
exists (edited in the web panel's "Role panel" page), otherwise from
SERVER_TEMPLATE["reaction_role_panel"].

Safety: roles with powerful permissions (administrator, manage/ban/kick, ...)
can never be handed out through the panel, even if someone configures it.
"""

import logging

import discord

from cogs.common import DATA_DIR, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.role_panel")

PANEL_FILE = DATA_DIR / "role_panel.json"
MAX_BUTTONS = 25

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

DANGEROUS_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "manage_messages", "manage_threads", "manage_nicknames", "manage_expressions", "manage_events",
    "ban_members", "kick_members", "moderate_members", "mention_everyone", "view_audit_log",
    "mute_members", "deafen_members", "move_members",
)


def is_safe_role(role: discord.Role) -> bool:
    if role.is_default() or role.managed:
        return False
    return not any(getattr(role.permissions, p, False) for p in DANGEROUS_PERMISSIONS)


def assignable_roles(guild: discord.Guild) -> list[discord.Role]:
    """Roles the bot can hand out safely: below its top role and without powerful permissions."""
    top = guild.me.top_role
    return [r for r in reversed(guild.roles) if r < top and is_safe_role(r)]


def get_panel_config() -> dict:
    stored = load_json(PANEL_FILE)
    if stored.get("buttons") is not None:
        base = dict(SERVER_TEMPLATE.get("reaction_role_panel", {}))
        base.update(stored)
        return base
    return dict(SERVER_TEMPLATE.get("reaction_role_panel", {}))


def save_panel_config(config: dict) -> None:
    save_json(PANEL_FILE, {
        "title": config.get("title", "Pick Your Roles"),
        "description": config.get("description", ""),
        "buttons": config.get("buttons", [])[:MAX_BUTTONS],
    })


class RoleToggleButton(discord.ui.Button):
    def __init__(self, role_name, label, emoji, style, row):
        super().__init__(label=label, emoji=emoji or None, style=style, custom_id=f"rr:{role_name}", row=row)
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Not in a server.", ephemeral=True)
            return
        role = discord.utils.get(guild.roles, name=self.role_name)
        if role is None:
            await interaction.response.send_message(
                f"Role `{self.role_name}` is missing. Ask an admin to re-run `/setup`.", ephemeral=True)
            return
        if not is_safe_role(role):
            await interaction.response.send_message("This role can't be self-assigned.", ephemeral=True)
            log.warning("Blocked self-assign of powerful role %s", role.name)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("Could not resolve your member object.", ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-assign panel")
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Self-assign panel")
                await interaction.response.send_message(f"Added **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't change that role - my role is below it in the hierarchy.", ephemeral=True)


class ReactionRolesView(discord.ui.View):
    def __init__(self, buttons_def):
        super().__init__(timeout=None)
        for b in buttons_def[:MAX_BUTTONS]:
            try:
                self.add_item(RoleToggleButton(
                    role_name=b["role"],
                    label=b["label"],
                    emoji=b.get("emoji"),
                    style=BUTTON_STYLES.get(b.get("style", "secondary"), discord.ButtonStyle.secondary),
                    row=b.get("row", 0),
                ))
            except ValueError:
                log.warning("Role panel: row %s is full, skipped button %s", b.get("row"), b.get("label"))


def build_panel() -> tuple[discord.Embed | None, discord.ui.View | None]:
    panel = get_panel_config()
    if not panel.get("buttons"):
        return None, None
    embed = discord.Embed(
        title=panel.get("title", "Self-Assign Roles"),
        description=panel.get("description", ""),
        color=0x5865F2,
    )
    return embed, ReactionRolesView(panel["buttons"])


async def refresh_panel_message(bot, guild: discord.Guild) -> str:
    """Re-register the buttons and edit (or post) the panel in the roles channel."""
    panel = get_panel_config()
    embed, view = build_panel()
    if view is not None:
        bot.add_view(ReactionRolesView(panel["buttons"]))
    channel = discord.utils.get(guild.text_channels, name=panel.get("channel", ""))
    if channel is None:
        return "roles channel not found"
    message = None
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id:
            message = msg
            break
    try:
        if embed is None:
            if message:
                await message.delete()
            return "panel removed (no buttons)"
        if message:
            await message.edit(embed=embed, view=view)
            return "panel updated"
        await channel.send(embed=embed, view=view)
        return "panel posted"
    except discord.HTTPException as e:
        return f"failed: {e}"
