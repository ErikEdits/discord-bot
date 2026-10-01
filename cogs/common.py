"""Shared helpers for the cogs. Not an extension (no setup function).

- DATA_DIR / load_json / save_json: small JSON state files, written atomically
  so a crash mid-write can't leave a half-written file behind.
- get_setting / set_setting: bot-wide settings in data/settings.json
  (webhook URLs, counter channel IDs, schedule bookkeeping).
- parse_duration / format_duration: "1h30m", "2d", "90s" <-> timedelta.
- is_admin / is_staff / has_perm: permission checks shared by the cogs. They also
  accept members the server owner unlocked the running command for (/grant).
- post_to_webhook: send an embed + optional file to a Discord webhook URL.
"""

import json
import logging
import os
import re
from contextvars import ContextVar
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import aiohttp
import discord
from discord import app_commands

log = logging.getLogger("setup-bot.common")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"

WEBHOOK_RE = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$"
)


# -------- JSON state ------------------------------------------------------

def load_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read %s", path.name)
        return {} if default is None else default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# -------- Settings --------------------------------------------------------

def get_setting(key: str, default=None):
    return load_json(SETTINGS_FILE).get(key, default)


def set_setting(key: str, value) -> None:
    data = load_json(SETTINGS_FILE)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    save_json(SETTINGS_FILE, data)


def webhook_url(setting_key: str, env_key: str) -> str | None:
    """A webhook set via /settings wins; the .env value is the fallback."""
    return get_setting(setting_key) or os.getenv(env_key) or None


# -------- Durations -------------------------------------------------------

_DURATION_PART = re.compile(r"(\d+)\s*(w|d|h|m|s)?", re.IGNORECASE)
_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> timedelta | None:
    """Parse "10m", "1h30m", "2d 4h", "90" (seconds). Returns None if invalid."""
    if not text:
        return None
    cleaned = text.strip().lower().replace(" ", "")
    if not cleaned or not re.fullmatch(r"(?:\d+[wdhms]?)+", cleaned):
        return None
    total = 0
    for number, unit in _DURATION_PART.findall(cleaned):
        total += int(number) * _UNIT_SECONDS.get((unit or "s").lower(), 1)
    return timedelta(seconds=total) if total > 0 else None


def format_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n}{unit}")
    return " ".join(parts) or "0s"


# -------- Permissions -----------------------------------------------------

# The slash command being handled in this task ("poll create"). Set by
# GrantAwareTree before every command, so the checks below know which command a
# grant has to cover. Outside a command (buttons, events) it's None -> no grants.
_CURRENT_COMMAND: ContextVar[str | None] = ContextVar("current_command", default=None)
GRANTS_FILE = DATA_DIR / "grants.json"


class GrantAwareTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        _CURRENT_COMMAND.set(command.qualified_name if command is not None else None)
        return True


def load_grants() -> dict:
    """{guild_id: {user_id: ["mclog", "poll create", ...]}}"""
    return load_json(GRANTS_FILE)


def is_granted(user, command: str | None = None) -> bool:
    """Did the server owner unlock this command (or its parent group) for this member?"""
    command = command or _CURRENT_COMMAND.get()
    guild = getattr(user, "guild", None)
    if not command or guild is None:
        return False
    granted = set(load_grants().get(str(guild.id), {}).get(str(user.id), []))
    parts = command.split()
    return any(" ".join(parts[:i]) in granted for i in range(1, len(parts) + 1))


def has_perm(user, permission: str) -> bool:
    """Has the Discord permission (Administrator counts for all), or a grant for this command."""
    perms = getattr(user, "guild_permissions", None)
    if perms and (perms.administrator or getattr(perms, permission, False)):
        return True
    return is_granted(user)


def is_admin(user) -> bool:
    return has_perm(user, "administrator")


def is_staff(member, support_role_names) -> bool:
    """Staff = has one of the support roles, Manage Channels / Administrator, or a grant."""
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_channels):
        return True
    names = set(support_role_names or [])
    return any(r.name in names for r in getattr(member, "roles", [])) or is_granted(member)


# -------- Self-test ------------------------------------------------------

# Everything /selftest creates starts with this name; the logging cog ignores it.
SELFTEST_PREFIX = "bot-selftest"


def is_selftest_name(name: str | None) -> bool:
    return bool(name) and name.startswith(SELFTEST_PREFIX)


# -------- Messages the bot deletes itself ---------------------------------

# IDs of messages the bot removes on purpose (reposted suggestions, filtered links).
# The logging cog skips these so #message-logs isn't flooded with its own cleanup.
_BOT_DELETED: set[int] = set()


def mark_bot_delete(message_id: int) -> None:
    if len(_BOT_DELETED) > 2000:
        _BOT_DELETED.clear()
    _BOT_DELETED.add(message_id)


def was_deleted_by_bot(message_id: int) -> bool:
    return message_id in _BOT_DELETED


# -------- Webhooks --------------------------------------------------------

async def post_to_webhook(url: str, *, content: str | None = None,
                          embed: discord.Embed | None = None,
                          file_name: str | None = None, file_bytes: bytes | None = None,
                          username: str | None = None) -> bool:
    """Send a message to a Discord webhook. Returns True on success."""
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(url, session=session)
            kwargs = {"wait": True}
            if content:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if username:
                kwargs["username"] = username
            if file_bytes is not None and file_name:
                kwargs["file"] = discord.File(BytesIO(file_bytes), filename=file_name)
            await webhook.send(**kwargs)
        return True
    except Exception:
        log.exception("Webhook send failed")
        return False
