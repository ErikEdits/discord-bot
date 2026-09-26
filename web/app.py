"""FastAPI web panel for the Discord bot.

Single-user auth via PANEL_PASSWORD env var. Cookie-based sessions stored in-memory.
Reads bot state directly from the in-process discord.py bot instance.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
import psutil
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from web.log_buffer import LOG_BUFFER

log = logging.getLogger("setup-bot.panel")

_WEB_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Discord Bot Panel", docs_url=None, redoc_url=None)
# cache_size=0 works around a Jinja2/Python 3.14 cache-key hashing bug (weakref hash on FileSystemLoader).
_jinja_env = Environment(
    loader=FileSystemLoader(str(_WEB_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
    auto_reload=True,
)
templates = Jinja2Templates(env=_jinja_env)
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "admin")
SESSION_COOKIE = "panel_session"
SESSIONS: set[str] = set()

_BOT: Optional[discord.Client] = None
_START_TIME = time.time()


def set_bot(bot: discord.Client) -> None:
    global _BOT, _START_TIME
    _BOT = bot
    _START_TIME = time.time()


def current_bot() -> Optional[discord.Client]:
    return _BOT


# -------- Auth helpers ---------------------------------------------------

def _is_authed(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return token is not None and token in SESSIONS


def require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return True


# -------- Data helpers ---------------------------------------------------

DATA_DIR = _WEB_DIR.parent / "data"
WARNINGS_FILE = DATA_DIR / "warnings.json"
TICKETS_FILE = DATA_DIR / "tickets.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


_PROCESS = psutil.Process()
# Prime cpu_percent so the first real call returns a useful value.
psutil.cpu_percent(interval=None)
_PROCESS.cpu_percent(interval=None)


def _system_stats() -> dict:
    try:
        mem = psutil.virtual_memory()
    except Exception:
        mem = None
    try:
        disk = psutil.disk_usage("/")
    except Exception:
        disk = None
    try:
        boot_time = psutil.boot_time()
        host_uptime = time.time() - boot_time
    except Exception:
        host_uptime = None
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = None
    try:
        proc_mem_mb = _PROCESS.memory_info().rss // (1024 * 1024)
    except Exception:
        proc_mem_mb = None
    try:
        proc_cpu = _PROCESS.cpu_percent(interval=None)
    except Exception:
        proc_cpu = None
    try:
        net = psutil.net_io_counters()
        net_out_mb = net.bytes_sent // (1024 * 1024)
        net_in_mb = net.bytes_recv // (1024 * 1024)
    except Exception:
        net_out_mb = net_in_mb = None
    return {
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "count": psutil.cpu_count(logical=True) or 1,
            "load1": round(load1, 2) if load1 is not None else None,
            "load5": round(load5, 2) if load5 is not None else None,
            "load15": round(load15, 2) if load15 is not None else None,
        },
        "memory": {
            "total_mb": mem.total // (1024 * 1024) if mem else 0,
            "used_mb": mem.used // (1024 * 1024) if mem else 0,
            "percent": mem.percent if mem else 0,
        },
        "disk": {
            "total_gb": round(disk.total / (1024 ** 3), 1) if disk else 0,
            "used_gb": round(disk.used / (1024 ** 3), 1) if disk else 0,
            "percent": disk.percent if disk else 0,
        },
        "network": {
            "sent_mb": net_out_mb,
            "recv_mb": net_in_mb,
        },
        "host_uptime_seconds": host_uptime,
        "process": {
            "cpu_percent": proc_cpu,
            "memory_mb": proc_mem_mb,
        },
    }


def _bot_status() -> dict:
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return {"ready": False}
    guilds = list(bot.guilds)
    guild = guilds[0] if guilds else None
    return {
        "ready": True,
        "bot_name": str(bot.user),
        "bot_id": bot.user.id,
        "latency_ms": round(bot.latency * 1000, 1),
        "uptime": _format_uptime(time.time() - _START_TIME),
        "guild_count": len(guilds),
        "guild_name": guild.name if guild else None,
        "guild_id": guild.id if guild else None,
        "member_count": guild.member_count if guild else 0,
        "channel_count": len(guild.channels) if guild else 0,
        "role_count": len(guild.roles) if guild else 0,
    }


def _active_tickets() -> list[dict]:
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return []
    out = []
    for guild in bot.guilds:
        category = discord.utils.get(guild.categories, name="TICKETS")
        if category is None:
            continue
        for ch in category.text_channels:
            creator_id, type_key = None, None
            if ch.topic:
                for part in ch.topic.split("|"):
                    part = part.strip()
                    if part.startswith("user "):
                        try: creator_id = int(part.split(" ", 1)[1])
                        except (ValueError, IndexError): pass
                    elif part.startswith("type "):
                        bits = part.split(" ", 1)
                        type_key = bits[1] if len(bits) > 1 else None
            out.append({
                "channel_id": ch.id,
                "channel_name": ch.name,
                "guild_id": guild.id,
                "creator_id": creator_id,
                "type_key": type_key,
                "created_at": ch.created_at.isoformat(),
                "jump_url": ch.jump_url,
            })
    return sorted(out, key=lambda t: t["created_at"], reverse=True)


def _all_warnings() -> list[dict]:
    data = _load_json(WARNINGS_FILE)
    bot = current_bot()
    out = []
    for guild_id, users in data.items():
        for user_id, warns in users.items():
            for w in warns:
                out.append({
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "warn_id": w.get("id", "?"),
                    "reason": w.get("reason", ""),
                    "moderator": w.get("moderator_name", "Unknown"),
                    "timestamp": w.get("timestamp", ""),
                })
    return sorted(out, key=lambda x: x["timestamp"], reverse=True)


# -------- Routes: auth ---------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
        "authenticated": False,
    })


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if not PANEL_PASSWORD:
        return RedirectResponse("/login?error=panel_disabled", status_code=303)
    if secrets.compare_digest(password, PANEL_PASSWORD):
        token = secrets.token_urlsafe(32)
        SESSIONS.add(token)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    return RedirectResponse("/login?error=wrong_password", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        SESSIONS.discard(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# -------- Routes: pages --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html", {
        "authenticated": True,
        "status": _bot_status(),
        "system": _system_stats(),
        "ticket_count": len(_active_tickets()),
        "warning_count": len(_all_warnings()),
        "recent_logs": list(reversed(list(LOG_BUFFER)[-15:])),
    })


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "tickets.html", {
        "authenticated": True,
        "tickets": _active_tickets(),
    })


@app.post("/tickets/{channel_id}/close")
async def tickets_close(request: Request, channel_id: int):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return JSONResponse({"ok": False, "error": "channel not found"}, status_code=404)
    try:
        from cogs.tickets import _archive_and_delete
        asyncio.create_task(_archive_and_delete(channel, bot.user))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return RedirectResponse("/tickets", status_code=303)


@app.get("/warnings", response_class=HTMLResponse)
async def warnings_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "warnings.html", {
        "authenticated": True,
        "warnings": _all_warnings(),
    })


@app.post("/warnings/{guild_id}/{user_id}/{warn_id}/delete")
async def warnings_delete(request: Request, guild_id: str, user_id: str, warn_id: str):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    data = _load_json(WARNINGS_FILE)
    warns = data.get(guild_id, {}).get(user_id, [])
    new_warns = [w for w in warns if w.get("id") != warn_id]
    if guild_id in data and user_id in data[guild_id]:
        if new_warns:
            data[guild_id][user_id] = new_warns
        else:
            del data[guild_id][user_id]
        _save_json(WARNINGS_FILE, data)
    return RedirectResponse("/warnings", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    entries = list(reversed(list(LOG_BUFFER)))
    return templates.TemplateResponse(request, "logs.html", {
        "authenticated": True,
        "logs": entries,
    })


# -------- Routes: API for live refresh -----------------------------------

@app.get("/api/status")
async def api_status(request: Request):
    if not _is_authed(request):
        raise HTTPException(401)
    return _bot_status()


@app.get("/api/system")
async def api_system(request: Request):
    if not _is_authed(request):
        raise HTTPException(401)
    return _system_stats()


@app.get("/api/logs")
async def api_logs(request: Request, since: float = 0):
    if not _is_authed(request):
        raise HTTPException(401)
    entries = [e for e in LOG_BUFFER if e["time"] > since]
    return {"entries": entries, "now": time.time()}


# -------- Helpers for the management pages ---------------------------------

def _redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse(path + (("?" + urlencode(params)) if params else ""), status_code=303)


def _guilds() -> list[discord.Guild]:
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return []
    return sorted(bot.guilds, key=lambda g: g.name.lower())


def _guild(guild_id) -> Optional[discord.Guild]:
    try:
        gid = int(guild_id)
    except (TypeError, ValueError):
        return None
    return next((g for g in _guilds() if g.id == gid), None)


def _page(request: Request, template: str, **context):
    return templates.TemplateResponse(request, template, {
        "authenticated": True,
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
        "guilds": _guilds(),
        **context,
    })


# -------- Routes: FAQs -----------------------------------------------------

@app.get("/faqs", response_class=HTMLResponse)
async def faqs_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.community import _load_faqs
    data = _load_faqs()
    per_guild = []
    for g in _guilds():
        entries = sorted(data.get(str(g.id), {}).items())
        per_guild.append({"guild": g, "faqs": entries})
    return _page(request, "faqs.html", per_guild=per_guild)


@app.post("/faqs/save")
async def faqs_save(request: Request, guild_id: str = Form(...), name: str = Form(...), answer: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.community import _load_faqs, _save_faqs
    guild = _guild(guild_id)
    key = name.strip().lower()
    if guild is None or not key or not answer.strip():
        return _redirect("/faqs", err="Server, name and answer are required.")
    if len(key) > 100 or len(answer) > 4000:
        return _redirect("/faqs", err="Name max. 100 and answer max. 4000 characters.")
    data = _load_faqs()
    data.setdefault(str(guild.id), {})[key] = {
        "answer": answer.strip(),
        "added_by": "web panel",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save_faqs(data)
    log.info("FAQ '%s' saved via panel", key)
    return _redirect("/faqs", msg=f"FAQ '{key}' saved.")


@app.post("/faqs/delete")
async def faqs_delete(request: Request, guild_id: str = Form(...), name: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.community import _load_faqs, _save_faqs
    data = _load_faqs()
    if data.get(guild_id, {}).pop(name, None) is None:
        return _redirect("/faqs", err="That FAQ doesn't exist.")
    _save_faqs(data)
    log.info("FAQ '%s' deleted via panel", name)
    return _redirect("/faqs", msg=f"FAQ '{name}' deleted.")


# -------- Routes: polls ----------------------------------------------------

@app.get("/polls", response_class=HTMLResponse)
async def polls_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.poll import _load_state, _presets
    polls = list(_load_state()["polls"].values())
    names = {g.id: g.name for g in _guilds()}
    for p in polls:
        p["guild_name"] = names.get(p.get("guild_id"), str(p.get("guild_id")))
    open_polls = sorted((p for p in polls if p.get("status") == "open"), key=lambda p: p["end_ts"])
    closed = sorted((p for p in polls if p.get("status") != "open"),
                    key=lambda p: p.get("closed_at", 0), reverse=True)[:15]
    for p in closed:
        counts = p.get("counts") or [0] * len(p["options"])
        p["results"] = sorted(zip(p["options"], counts), key=lambda r: r[1], reverse=True)
    return _page(request, "polls.html", open_polls=open_polls, closed_polls=closed, presets=_presets(), now=time.time())


@app.post("/polls/create")
async def polls_create(request: Request, guild_id: str = Form(...), question: str = Form(""),
                       options: str = Form(""), duration_hours: float = Form(24.0), preset: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    bot = current_bot()
    cog = bot.get_cog("Poll") if bot else None
    guild = _guild(guild_id)
    if cog is None or guild is None:
        return _redirect("/polls", err="Bot not ready or poll feature not loaded.")
    if preset:
        from cogs.poll import _preset_by_key
        p = _preset_by_key(preset)
        if p is None:
            return _redirect("/polls", err="Unknown preset.")
        question, option_list = p["question"], list(p["options"])
    else:
        raw = options.replace("\r", "")
        parts = raw.split("\n") if "\n" in raw.strip() else raw.split(",")
        option_list = [o.strip() for o in parts if o.strip()]
    try:
        poll_id, channel = await cog.start_poll(guild, question.strip(), option_list, duration_hours, 0)
    except ValueError as e:
        return _redirect("/polls", err=str(e))
    log.info("Poll %s created via panel", poll_id)
    return _redirect("/polls", msg=f"Poll {poll_id} posted in #{channel.name}.")


@app.post("/polls/close")
async def polls_close(request: Request, poll_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    bot = current_bot()
    cog = bot.get_cog("Poll") if bot else None
    if cog is None:
        return _redirect("/polls", err="Poll feature not loaded.")
    await cog._close_poll(poll_id, reason="closed via web panel")
    return _redirect("/polls", msg=f"Poll {poll_id} closed.")


# -------- Routes: maintenance ----------------------------------------------

@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.maintenance import FEATURES, _load
    state = _load()
    features = [
        {"key": k, "label": label, "enabled": state.get(k, {}).get("enabled", False),
         "message": state.get(k, {}).get("message", "")}
        for k, label in FEATURES.items()
    ]
    return _page(request, "maintenance.html", features=features)


@app.post("/maintenance/set")
async def maintenance_set(request: Request, feature: str = Form(...), state: str = Form(...), message: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.maintenance import FEATURES, set_state
    if feature not in FEATURES:
        return _redirect("/maintenance", err="Unknown feature.")
    enabled = state == "on"
    set_state(feature, enabled, message.strip()[:500])
    log.info("Maintenance %s -> %s via panel", feature, "on" if enabled else "off")
    return _redirect("/maintenance", msg=f"{FEATURES[feature]} is now {'in maintenance' if enabled else 'online'}.")


# -------- Routes: announcements --------------------------------------------

@app.get("/announce", response_class=HTMLResponse)
async def announce_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    channels = []
    for g in _guilds():
        for ch in sorted(g.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
            if ch.permissions_for(g.me).send_messages:
                channels.append({"id": ch.id, "label": f"{g.name} / #{ch.name}",
                                 "category": ch.category.name if ch.category else ""})
    return _page(request, "announce.html", channels=channels)


@app.post("/announce")
async def announce_send(request: Request, channel_id: str = Form(...), title: str = Form(...),
                        message: str = Form(...), ping_everyone: Optional[str] = Form(None)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return _redirect("/announce", err="Bot not connected.")
    try:
        channel = bot.get_channel(int(channel_id))
    except ValueError:
        channel = None
    if not isinstance(channel, discord.TextChannel):
        return _redirect("/announce", err="Channel not found.")
    if not title.strip() or not message.strip():
        return _redirect("/announce", err="Title and message are required.")
    embed = discord.Embed(
        title=title.strip()[:256],
        description=message.replace("\r", "").strip()[:4000],
        color=0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Announcement")
    ping = ping_everyone == "on"
    try:
        await channel.send(content="@everyone" if ping else None, embed=embed,
                           allowed_mentions=discord.AllowedMentions(everyone=ping))
    except discord.HTTPException as e:
        return _redirect("/announce", err=f"Sending failed: {e}")
    log.info("Announcement posted via panel in #%s", channel.name)
    return _redirect("/announce", msg=f"Announcement posted in #{channel.name}.")
