"""Nightly cleanup of old, finished data - keeps the data folder small and the bot quick.

Every night (around 04:00) it removes entries that are finished and older than
keep_days (default 90):
    polls.json              closed polls
    appeals.json            decided ban appeals
    invite_requests.json    decided invite requests
    emoji_suggestions.json  added/rejected emoji suggestions (+ their image files)
    beta_feedback.json      summarized beta feedback rounds
    bugs.json               fixed bugs (kept twice as long)
    role_restore.json       members who left long ago (see cogs/role_restore.py)
    data/image_cache/       old saved images (see cogs/logging_cog.py)
Open/pending things are never touched.

Config: SERVER_TEMPLATE["data_cleanup"]. Feature switch: "data_cleanup".
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from discord.ext import commands, tasks

from cogs.common import DATA_DIR, feature_on, get_setting, load_json, save_json, set_setting
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.data_cleanup")

LAST_RUN_KEY = "data_cleanup_last"


def _config() -> dict:
    return SERVER_TEMPLATE.get("data_cleanup", {})


def _old(ts, now: float, days: float) -> bool:
    try:
        return now - float(ts) > days * 86400
    except (TypeError, ValueError):
        return False


def _prune_dict(items: dict, finished, now: float, days: float) -> int:
    """Remove entries where finished(entry) returns a timestamp that's older than `days`."""
    remove = []
    for key, entry in items.items():
        ts = finished(entry) if isinstance(entry, dict) else None
        if ts is not None and _old(ts, now, days):
            remove.append(key)
    for key in remove:
        del items[key]
    return len(remove)


def _file_job(name: str, collection: str, finished, days_factor: float = 1.0, on_remove=None):
    def job(now: float, days: float) -> int:
        path = DATA_DIR / name
        if not path.exists():
            return 0
        data = load_json(path)
        items = data.get(collection)
        if not isinstance(items, dict):
            return 0
        before = dict(items)
        removed = _prune_dict(items, finished, now, days * days_factor)
        if removed:
            if on_remove is not None:
                for key in set(before) - set(items):
                    on_remove(before[key])
            save_json(path, data)
        return removed
    return job


def _remove_emoji_file(entry: dict) -> None:
    fname = entry.get("file")
    if fname and "/" not in fname and "\\" not in fname:
        (DATA_DIR / "emoji_suggestions" / fname).unlink(missing_ok=True)


JOBS = {
    "polls": _file_job("polls.json", "polls",
                       lambda e: e.get("closed_at", e.get("end_ts")) if e.get("status") != "open" else None),
    "appeals": _file_job("appeals.json", "appeals",
                         lambda e: e.get("decided", e.get("ts")) if e.get("status") != "pending" else None),
    "invite requests": _file_job("invite_requests.json", "requests",
                                 lambda e: e.get("ts") if e.get("status") != "pending" else None),
    "emoji suggestions": _file_job("emoji_suggestions.json", "suggestions",
                                   lambda e: e.get("ts") if e.get("status") != "open" else None,
                                   on_remove=_remove_emoji_file),
    "beta feedback rounds": _file_job("beta_feedback.json", "rounds",
                                      lambda e: e.get("ts") if e.get("summarized") else None),
    "fixed bugs": _file_job("bugs.json", "bugs",
                            lambda e: e.get("fixed_at") if e.get("status") == "fixed" else None, days_factor=2),
}


def run_cleanup(now: float | None = None) -> dict[str, int]:
    """Run every job. Returns {what: how many removed} (only non-zero)."""
    now = time.time() if now is None else now
    days = float(_config().get("keep_days", 90))
    result: dict[str, int] = {}
    for what, job in JOBS.items():
        try:
            n = job(now, days)
        except Exception:
            log.exception("Cleanup of %s failed", what)
            continue
        if n:
            result[what] = n
    try:
        from cogs.role_restore import STATE_FILE as ROLE_FILE, prune
        data = load_json(ROLE_FILE)
        n = prune(data, now)
        if n:
            save_json(ROLE_FILE, data)
            result["left members (role restore)"] = n
    except Exception:
        log.exception("Cleanup of role restore failed")
    try:
        from cogs.logging_cog import cleanup_image_cache
        n = cleanup_image_cache(now)
        if n:
            result["cached images"] = n
    except Exception:
        log.exception("Cleanup of the image cache failed")
    return result


class DataCleanup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if _config().get("enabled", True):
            self.loop.start()

    def cog_unload(self):
        if self.loop.is_running():
            self.loop.cancel()

    @tasks.loop(minutes=30)
    async def loop(self):
        if not feature_on("data_cleanup"):
            return
        try:
            tz = ZoneInfo(_config().get("timezone", "Europe/Berlin"))
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        today = now.strftime("%Y-%m-%d")
        if now.hour < int(_config().get("hour", 4)) or get_setting(LAST_RUN_KEY) == today:
            return
        set_setting(LAST_RUN_KEY, today)
        result = run_cleanup()
        if result:
            log.info("Data cleanup: %s", ", ".join(f"{n} {what}" for what, n in result.items()))
        else:
            log.info("Data cleanup: nothing to remove")

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(DataCleanup(bot))
