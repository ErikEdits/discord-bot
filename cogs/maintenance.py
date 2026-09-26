"""Maintenance toggles per feature.

Admins can put individual bot features into maintenance via slash commands:
    /maintenance ticket state:on [message]
    /maintenance moddownload state:off
    /maintenance status

When a feature is "on" (maintenance), the corresponding panel/button gracefully
rejects user interactions with the configured message instead of acting.

State is persisted to data/maintenance.json so toggles survive restarts.
"""

import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("setup-bot.maintenance")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "maintenance.json"

# Known features — name in the command + pretty label for messages.
FEATURES = {
    "ticket":       "Ticket System",
    "moddownload":  "Mod Downloads",
    "antispam":     "Anti-Spam",
}


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read maintenance state")
        return {}


def _save(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_under_maintenance(feature: str) -> bool:
    return bool(_load().get(feature, {}).get("enabled", False))


def get_maintenance_message(feature: str) -> str:
    state = _load().get(feature, {})
    if not state.get("enabled"):
        return ""
    msg = state.get("message", "")
    label = FEATURES.get(feature, feature)
    return msg or f"**{label}** is currently under maintenance. Please try again later."


def set_state(feature: str, enabled: bool, message: str = "") -> None:
    data = _load()
    data[feature] = {"enabled": enabled, "message": message or ""}
    _save(data)


STATE_CHOICES = [
    app_commands.Choice(name="on (enable maintenance)", value="on"),
    app_commands.Choice(name="off (back online)", value="off"),
]


class Maintenance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(
        name="maintenance",
        description="Toggle maintenance mode for bot features.",
        default_permissions=discord.Permissions(administrator=True),
    )

    async def _toggle(self, interaction: discord.Interaction, feature: str, state_value: str, message: str) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        enabled = state_value == "on"
        set_state(feature, enabled, message)
        label = FEATURES.get(feature, feature)
        if enabled:
            shown = message.strip() or f"`{label}` is currently under maintenance. Please try again later."
            text = (
                f"\U0001F527 **{label}** is now under maintenance.\n"
                f"Users will see:\n> {shown}"
            )
        else:
            text = f"✅ **{label}** is back online."
        await interaction.response.send_message(text, ephemeral=True)

    @group.command(name="ticket", description="Put the ticket panel into maintenance mode.")
    @app_commands.describe(state="Enable or disable maintenance", message="Optional message shown when users try to open a ticket")
    @app_commands.choices(state=STATE_CHOICES)
    async def ticket(self, interaction: discord.Interaction, state: app_commands.Choice[str], message: str = ""):
        await self._toggle(interaction, "ticket", state.value, message)

    @group.command(name="moddownload", description="Put the mod-download dropdown into maintenance mode.")
    @app_commands.describe(state="Enable or disable maintenance", message="Optional message shown when users pick a mod")
    @app_commands.choices(state=STATE_CHOICES)
    async def moddownload(self, interaction: discord.Interaction, state: app_commands.Choice[str], message: str = ""):
        await self._toggle(interaction, "moddownload", state.value, message)

    @group.command(name="antispam", description="Pause the anti-spam protection (maintenance mode).")
    @app_commands.describe(state="Enable or disable maintenance", message="Optional note shown in /maintenance status")
    @app_commands.choices(state=STATE_CHOICES)
    async def antispam(self, interaction: discord.Interaction, state: app_commands.Choice[str], message: str = ""):
        await self._toggle(interaction, "antispam", state.value, message)

    @group.command(name="status", description="Show the maintenance status of every feature.")
    async def status(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        lines = []
        for key, label in FEATURES.items():
            state = data.get(key, {})
            enabled = state.get("enabled", False)
            symbol = "\U0001F527" if enabled else "✅"
            status = "Maintenance" if enabled else "Available"
            line = f"{symbol} **{label}** — {status}"
            msg = (state.get("message") or "").strip()
            if enabled and msg:
                line += f"\n   ↳ _{msg}_"
            lines.append(line)
        embed = discord.Embed(
            title="Maintenance status",
            description="\n".join(lines) or "Nothing configured.",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Maintenance(bot))
