"""Backups.

/backup-config   exports the current server's structural state as JSON
                 (roles, channels, categories with overwrites and topics, settings).

Automatic backups: once a day (SERVER_TEMPLATE["backups"]) the bot zips its
data files (warnings, levels, FAQs, polls, giveaways, ...) plus the server
structure and sends the zip to the webhook set with /settings backup-webhook.
Nothing is kept on the bot host - the webhook's channel is the archive.

/backup-now      send a backup right now (administrator)
/backup-restore  restore the bot data from a backup zip (administrator)

data/settings.json (webhook URLs) is never included in or overwritten by a backup.
"""

import io
import json
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, get_setting, is_admin, post_to_webhook, save_json, set_setting, webhook_url
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.backup")

BACKUP_EXCLUDE = {"settings.json"}
MAX_WEBHOOK_BYTES = 9 * 1024 * 1024       # Discord's upload limit for webhooks is ~10 MB
MAX_RESTORE_FILE_BYTES = 20 * 1024 * 1024
DATA_NAME_RE = re.compile(r"^[\w\-]+\.json$")


def _config() -> dict:
    return SERVER_TEMPLATE.get("backups", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_config().get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def _overwrites_to_dict(channel: discord.abc.GuildChannel) -> dict:
    out = {}
    for target, perms in channel.overwrites.items():
        name = "@everyone" if isinstance(target, discord.Role) and target.is_default() else getattr(target, "name", str(target))
        allow, deny = perms.pair()
        out[name] = {
            "allow": [p for p, v in allow if v],
            "deny": [p for p, v in deny if v],
        }
    return out


def _role_to_dict(role: discord.Role) -> dict:
    return {
        "name": role.name,
        "color": f"#{role.color.value:06X}",
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "position": role.position,
        "permissions": [p for p, v in role.permissions if v],
    }


def _channel_to_dict(channel: discord.abc.GuildChannel) -> dict:
    base = {
        "name": channel.name,
        "type": str(channel.type),
        "position": channel.position,
        "overwrites": _overwrites_to_dict(channel),
    }
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        base["topic"] = channel.topic
        base["nsfw"] = channel.is_nsfw()
        if isinstance(channel, discord.TextChannel):
            base["slowmode"] = channel.slowmode_delay
    if isinstance(channel, discord.VoiceChannel):
        base["bitrate"] = channel.bitrate
        base["user_limit"] = channel.user_limit
    return base


def _build_snapshot(guild: discord.Guild) -> dict:
    categories = []
    for cat in guild.categories:
        children = [_channel_to_dict(c) for c in cat.channels]
        categories.append({
            "name": cat.name,
            "position": cat.position,
            "overwrites": _overwrites_to_dict(cat),
            "channels": children,
        })
    orphan_channels = [_channel_to_dict(c) for c in guild.channels if c.category is None and not isinstance(c, discord.CategoryChannel)]
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "guild": {
            "id": guild.id,
            "name": guild.name,
            "description": guild.description,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "owner_id": guild.owner_id,
            "member_count": guild.member_count,
            "premium_tier": guild.premium_tier,
            "verification_level": str(guild.verification_level),
            "default_notifications": str(guild.default_notifications),
            "explicit_content_filter": str(guild.explicit_content_filter),
            "features": list(guild.features),
            "afk_channel": guild.afk_channel.name if guild.afk_channel else None,
            "afk_timeout": guild.afk_timeout,
            "system_channel": guild.system_channel.name if guild.system_channel else None,
            "rules_channel": guild.rules_channel.name if guild.rules_channel else None,
            "public_updates_channel": guild.public_updates_channel.name if guild.public_updates_channel else None,
        },
        "roles": [_role_to_dict(r) for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)],
        "categories": categories,
        "uncategorized_channels": orphan_channels,
    }


def build_backup_zip(bot) -> tuple[str, bytes, list[str]]:
    """Returns (filename, zip bytes, list of included data files)."""
    buf = io.BytesIO()
    included = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(DATA_DIR.glob("*.json")):
            if path.name in BACKUP_EXCLUDE:
                continue
            z.write(path, f"data/{path.name}")
            included.append(path.name)
        for guild in bot.guilds:
            z.writestr(f"server-structure/{guild.id}.json",
                       json.dumps(_build_snapshot(guild), indent=2, ensure_ascii=False))
        z.writestr("README.txt", (
            "Discord bot backup\n"
            f"Created: {datetime.now(timezone.utc).isoformat()}\n\n"
            "data/              bot data (restore with /backup-restore and this zip)\n"
            "server-structure/  roles + channels per server, for reference\n"
        ))
    stamp = datetime.now(_tz()).strftime("%Y%m%d-%H%M")
    return f"bot-backup-{stamp}.zip", buf.getvalue(), included


async def run_backup(bot, reason: str) -> tuple[bool, str]:
    url = webhook_url("backup_webhook", "BACKUP_WEBHOOK_URL")
    if not url:
        return False, "No backup webhook set. Use `/settings backup-webhook` first."
    filename, data, included = build_backup_zip(bot)
    if len(data) > MAX_WEBHOOK_BYTES:
        log.warning("Backup is %d bytes - too big for a webhook", len(data))
        return False, f"The backup is {len(data) // 1024} KB - too big for a Discord webhook."
    embed = discord.Embed(
        title="\U0001F5C4\uFE0F Bot backup",
        description=(
            f"**Reason:** {reason}\n"
            f"**Servers:** {', '.join(g.name for g in bot.guilds) or '-'}\n"
            f"**Data files:** {len(included)}\n"
            f"**Size:** {max(1, len(data) // 1024)} KB\n\n"
            "Restore with `/backup-restore` and this file."
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    ok = await post_to_webhook(url, embed=embed, file_name=filename, file_bytes=data, username="Bot Backups")
    if not ok:
        return False, "Sending the backup to the webhook failed - check `/settings show`."
    set_setting("last_backup_ts", time.time())
    set_setting("last_backup_date", datetime.now(_tz()).date().isoformat())
    log.info("Backup sent (%s, %d KB, %d files)", reason, len(data) // 1024, len(included))
    return True, f"Backup `{filename}` sent ({max(1, len(data) // 1024)} KB)."


def read_backup_zip(data: bytes) -> dict[str, object]:
    """Validate a backup zip and return {data file name: parsed JSON}. Raises ValueError."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That's not a zip file.")
    files: dict[str, object] = {}
    with z:
        for info in z.infolist():
            if not info.filename.startswith("data/"):
                continue
            name = info.filename[len("data/"):]
            if not DATA_NAME_RE.match(name) or name in BACKUP_EXCLUDE:
                continue
            if info.file_size > MAX_RESTORE_FILE_BYTES:
                raise ValueError(f"`{name}` is too big.")
            try:
                files[name] = json.loads(z.read(info).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError(f"`{name}` isn't valid JSON.")
    if not files:
        raise ValueError("No bot data found in that zip. Use a zip created by the bot's backup.")
    return files


class ConfirmRestoreView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Restore", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="Restoring...", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.edit_message(content="Restore cancelled.", view=None)
        self.stop()


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if _config().get("enabled", True):
            self.schedule_loop.start()

    def cog_unload(self):
        if self.schedule_loop.is_running():
            self.schedule_loop.cancel()

    @tasks.loop(minutes=15)
    async def schedule_loop(self):
        now = datetime.now(_tz())
        if now.hour < int(_config().get("hour", 4)):
            return
        if get_setting("last_backup_date") == now.date().isoformat():
            return
        if not webhook_url("backup_webhook", "BACKUP_WEBHOOK_URL"):
            return
        await run_backup(self.bot, "daily automatic backup")

    @schedule_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="backup-now", description="Send a backup of the bot data to the backup webhook now.")
    @app_commands.default_permissions(administrator=True)
    async def backup_now(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, text = await run_backup(self.bot, f"manual backup by {interaction.user}")
        await interaction.followup.send(("\u2705 " if ok else "\u274C ") + text, ephemeral=True)

    @app_commands.command(name="backup-restore", description="Restore the bot data (warnings, levels, FAQs, ...) from a backup zip.")
    @app_commands.describe(file="A bot-backup-....zip from the backup channel")
    @app_commands.default_permissions(administrator=True)
    async def backup_restore(self, interaction: discord.Interaction, file: discord.Attachment):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if file.size > 25 * 1024 * 1024:
            await interaction.response.send_message("That file is too big.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            files = read_backup_zip(await file.read())
        except ValueError as e:
            await interaction.followup.send(f"\u274C {e}", ephemeral=True)
            return
        view = ConfirmRestoreView(interaction.user.id)
        await interaction.followup.send(
            "**This replaces the current bot data with the backup:**\n"
            + ", ".join(f"`{n}`" for n in sorted(files))
            + "\n\nServer channels and roles are NOT touched. Continue?",
            view=view, ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return
        for name, content in files.items():
            save_json(DATA_DIR / name, content)
        # Cogs that keep data in memory need to pick up the restored files.
        levels = self.bot.get_cog("Levels")
        if levels is not None:
            levels.reload()
        if "cogs.poll" in self.bot.extensions:
            try:
                await self.bot.reload_extension("cogs.poll")
            except Exception:
                log.exception("Failed to reload polls after restore")
        log.info("Backup restored by %s (%d files)", interaction.user, len(files))
        await interaction.followup.send(f"\u2705 Restored {len(files)} data file(s).", ephemeral=True)

    @app_commands.command(name="backup-config", description="Export this server's roles + channels + settings as a JSON file.")
    @app_commands.default_permissions(administrator=True)
    async def backup_config(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = _build_snapshot(guild)
        body = json.dumps(snapshot, indent=2, ensure_ascii=False)
        filename = f"backup-{guild.id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
        file = discord.File(io.BytesIO(body.encode("utf-8")), filename=filename)
        summary = (
            f"**Backup created.**\n"
            f"- Roles: {len(snapshot['roles'])}\n"
            f"- Categories: {len(snapshot['categories'])}\n"
            f"- Channels: {sum(len(c['channels']) for c in snapshot['categories']) + len(snapshot['uncategorized_channels'])}\n"
            f"- File: `{filename}` ({len(body) // 1024} KB)"
        )
        await interaction.followup.send(summary, file=file, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Backup(bot))
