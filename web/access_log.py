"""Who opened the panel, when and from which device (panel page "Access").

Devices:
- browser logins (Discord or password) get a long-lived random "panel_device" cookie,
- every panel launcher file / Android app is its own device (its key).
The first time a device shows up, the server owner gets a DM (and a push in the app).

State: data/panel_access.json - devices + the last MAX_EVENTS events.
"""

import asyncio
import logging
import time
from pathlib import Path

import discord

log = logging.getLogger("setup-bot.panel.access")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACCESS_FILE = DATA_DIR / "panel_access.json"
MAX_EVENTS = 300
LAUNCHER_EVENT_EVERY = 30 * 60   # a launcher/app sends many requests - log at most one event per 30 min


def _load() -> dict:
    from cogs.common import load_json
    data = load_json(ACCESS_FILE)
    data.setdefault("devices", {})
    data.setdefault("events", [])
    return data


def _save(data: dict) -> None:
    from cogs.common import save_json
    save_json(ACCESS_FILE, data)


def record(*, user_id: int | None, name: str, method: str, device_id: str, label: str, ip: str = "",
           now: float | None = None) -> bool:
    """Log an access. Returns True if the device was never seen before."""
    now = time.time() if now is None else now
    data = _load()
    device = data["devices"].get(device_id)
    new = device is None
    if new:
        device = data["devices"][device_id] = {"first": now, "count": 0}
    elif method == "launcher" and now - device.get("last", 0) < LAUNCHER_EVENT_EVERY:
        if now - device.get("last", 0) > 60:   # keep "last seen" fresh without writing on every request
            device["last"] = now
            _save(data)
        return False
    device.update(user_id=user_id, name=name, method=method, label=label, ip=ip, last=now,
                  count=device.get("count", 0) + 1)
    data["events"].append({"t": now, "user_id": user_id, "name": name, "method": method, "label": label,
                           "ip": ip, "device": device_id, "new": new})
    del data["events"][:-MAX_EVENTS]
    _save(data)
    log.info("Panel access: %s via %s (%s)%s", name, method, label, " - NEW DEVICE" if new else "")
    return new


def forget_device(device_id: str) -> bool:
    data = _load()
    if data["devices"].pop(device_id, None) is None:
        return False
    _save(data)
    return True


def overview() -> dict:
    data = _load()
    devices = sorted(({"id": k, **v} for k, v in data["devices"].items()), key=lambda d: -d.get("last", 0))
    return {"devices": devices, "events": list(reversed(data["events"]))}


def browser_label(user_agent: str) -> str:
    ua = user_agent or ""
    system = next((s for key, s in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                    ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if key in ua), "")
    browsers = (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"),
                ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
    browser = next((b for key, b in browsers if key in ua), "Browser")
    return f"{browser} on {system}" if system else browser


async def notify_new_device(bot, name: str, method: str, label: str, ip: str = "") -> None:
    """DM the server owner(s) about a device that opened the panel for the first time."""
    if bot is None:
        return
    how = {"discord": "Discord login", "password": "password login", "launcher": "launcher / app"}.get(method, method)
    embed = discord.Embed(title="\U0001F511 New device opened the bot panel", color=0xE67E22,
                          description=f"**{name}** opened the panel from a device that wasn't used before.")
    embed.add_field(name="Device", value=label or "unknown", inline=True)
    embed.add_field(name="How", value=how, inline=True)
    if ip and ip not in ("127.0.0.1", "testclient"):
        embed.add_field(name="IP", value=ip, inline=True)
    embed.set_footer(text="Not you or your team? Change the panel password / revoke the launcher "
                          "(/panel-launcher revoke) right away.")
    owners = {g.owner_id for g in bot.guilds if g.owner_id}
    for owner_id in owners:
        try:
            user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
            await user.send(embed=embed)
        except (discord.HTTPException, AttributeError):
            log.info("Could not DM the owner about a new panel device")
    try:
        from cogs.push import safe_notify
        safe_notify("panel", "\U0001F511 New panel device", f"{name} - {label}", "/access")
    except Exception:
        pass


def record_and_notify(bot, **kwargs) -> None:
    """record() + a DM in the background if it's a new device. Never raises."""
    try:
        if record(**kwargs):
            asyncio.get_running_loop().create_task(
                notify_new_device(bot, kwargs["name"], kwargs["method"], kwargs["label"], kwargs.get("ip", "")))
    except Exception:
        log.exception("Panel access log failed")
