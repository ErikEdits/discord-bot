"""Notifications for the Bot Panel app (and anything else that wants to listen).

Other cogs call push.notify(...) for things the team should know right away (new
ticket, reported message, ban appeal, ...). The Android app keeps a background
connection to /api/push/wait (web/app.py) through the panel relay and shows each one
as a phone notification. Kept in memory only (last 100).
"""

import asyncio
import time
from collections import deque

ITEMS: deque = deque(maxlen=100)
_seq = 0
_event: asyncio.Event | None = None
BOOT = f"{int(time.time())}"


def seq() -> int:
    return _seq


def notify(kind: str, title: str, body: str = "", path: str = "/") -> None:
    """Queue a notification. kind: ticket, report, appeal, alt, invite, flag, urgent, error ..."""
    global _seq, _event
    _seq += 1
    ITEMS.append({"seq": _seq, "kind": kind, "title": title[:100], "body": body[:300], "path": path,
                  "ts": int(time.time())})
    if _event is not None:
        _event.set()
        _event = None


async def wait(since: int, timeout: float = 20.0) -> list[dict]:
    """Notifications newer than `since`; waits up to `timeout` seconds for new ones."""
    global _event
    if since > _seq:   # the app counted from before a bot restart
        since = 0
    items = [i for i in ITEMS if i["seq"] > since]
    if items:
        return items
    if _event is None:
        _event = asyncio.Event()
    event = _event
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        return []
    return [i for i in ITEMS if i["seq"] > since]


def safe_notify(*args, **kwargs) -> None:
    try:
        notify(*args, **kwargs)
    except Exception:
        pass
