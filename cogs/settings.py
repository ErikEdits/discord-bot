"""/settings - admin-only bot settings that shouldn't live in the code.

    /settings bug-webhook url:<webhook>     where "Forward bug" in bug tickets sends to
    /settings backup-webhook url:<webhook>  where automatic backups are sent to
    /settings show                          what's configured (URLs are masked)

Leave `url` empty to remove a webhook. Values are stored in data/settings.json.
A value in .env (BUG_WEBHOOK_URL / BACKUP_WEBHOOK_URL) is used as fallback.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import WEBHOOK_RE, get_setting, is_admin, post_to_webhook, set_setting, webhook_url

log = logging.getLogger("setup-bot.settings")

WEBHOOKS = {
    "bug_webhook":    ("BUG_WEBHOOK_URL",    "Bug forwarding"),
    "backup_webhook": ("BACKUP_WEBHOOK_URL", "Backups"),
}


def _mask(url: str | None) -> str:
    if not url:
        return "not set"
    return url[:45] + "…" + url[-4:]


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(
        name="settings",
        description="Bot settings (administrator only).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def _set_webhook(self, interaction: discord.Interaction, key: str, url: str | None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        _env, label = WEBHOOKS[key]
        if not url:
            set_setting(key, None)
            await interaction.response.send_message(f"**{label}** webhook removed.", ephemeral=True)
            log.info("%s webhook removed by %s", label, interaction.user)
            return
        url = url.strip()
        if not WEBHOOK_RE.match(url):
            await interaction.response.send_message(
                "That doesn't look like a Discord webhook URL "
                "(`https://discord.com/api/webhooks/<id>/<token>`).",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await post_to_webhook(
            url,
            content=f"✅ This webhook is now used for **{label}** by the bot in **{interaction.guild.name}**.",
        )
        if not ok:
            await interaction.followup.send(
                "I couldn't send a test message to that webhook. Check the URL and try again.",
                ephemeral=True,
            )
            return
        set_setting(key, url)
        log.info("%s webhook set by %s", label, interaction.user)
        await interaction.followup.send(
            f"**{label}** webhook saved. A test message was sent to it.", ephemeral=True
        )

    @group.command(name="bug-webhook", description="Webhook that receives forwarded bug reports. Leave empty to remove.")
    @app_commands.describe(url="Discord webhook URL")
    async def bug_webhook(self, interaction: discord.Interaction, url: str | None = None):
        await self._set_webhook(interaction, "bug_webhook", url)

    @group.command(name="backup-webhook", description="Webhook that receives the automatic backups. Leave empty to remove.")
    @app_commands.describe(url="Discord webhook URL")
    async def backup_webhook(self, interaction: discord.Interaction, url: str | None = None):
        await self._set_webhook(interaction, "backup_webhook", url)

    @group.command(name="show", description="Show the current bot settings.")
    async def show(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        embed = discord.Embed(title="Bot settings", color=0x5865F2)
        for key, (env_key, label) in WEBHOOKS.items():
            source = "via /settings" if get_setting(key) else ("via .env" if webhook_url(key, env_key) else "")
            value = _mask(webhook_url(key, env_key))
            embed.add_field(name=f"{label} webhook", value=f"`{value}` {source}".strip(), inline=False)
        last_backup = get_setting("last_backup_ts")
        if last_backup:
            embed.add_field(name="Last backup", value=f"<t:{int(last_backup)}:R>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Settings(bot))
