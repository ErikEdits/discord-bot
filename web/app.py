"""FastAPI web panel for the Discord bot.

Login:
- "Login with Discord" (OAuth2) when DISCORD_CLIENT_SECRET is set. Only people who
  have the Administrator permission on a server the bot is in get in, and that is
  re-checked against Discord on every request - lose the permission, lose access.
- Password login (PANEL_PASSWORD) otherwise. When Discord login is configured the
  password login is switched off unless PANEL_ALLOW_PASSWORD=true.
Cookie-based sessions stored in-memory.
Reads bot state directly from the in-process discord.py bot instance.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
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
_jinja_env.globals["panel_user"] = lambda request: _panel_user(request)
_jinja_env.globals["relay_mode"] = lambda request: _relay(request) is not None
# Live-refresh interval of the dashboard/logs pages. Over the Discord relay (panel launcher)
# every refresh is a Discord message, so refresh less often there.
_jinja_env.globals["poll_ms"] = lambda request: 15000 if _relay(request) else 3000
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "admin")
SESSION_COOKIE = "panel_session"
DEVICE_COOKIE = "panel_device"
STATE_COOKIE = "panel_oauth_state"
# token -> {"user_id": int | None, "name": str, "created": float}; user_id None = password login
SESSIONS: dict[str, dict] = {}

DISCORD_API = "https://discord.com/api/v10"
OAUTH_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
OAUTH_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()  # default: the bot's application ID
PANEL_PUBLIC_URL = os.getenv("PANEL_PUBLIC_URL", "").strip().rstrip("/")
OAUTH_ENABLED = bool(OAUTH_CLIENT_SECRET)
PASSWORD_LOGIN = bool(PANEL_PASSWORD) and (
    not OAUTH_ENABLED or os.getenv("PANEL_ALLOW_PASSWORD", "").lower() in ("1", "true", "yes")
)

_BOT: Optional[discord.Client] = None
_START_TIME = time.time()


def set_bot(bot: discord.Client) -> None:
    global _BOT, _START_TIME
    _BOT = bot
    _START_TIME = time.time()


def current_bot() -> Optional[discord.Client]:
    return _BOT


# -------- Auth helpers ---------------------------------------------------

def _discord_admin_guilds(user_id: int) -> list[discord.Guild]:
    """Servers (shared with the bot) where this user has the Administrator permission."""
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return []
    out = []
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is not None and member.guild_permissions.administrator:
            out.append(guild)
    return out


def _relay(request: Request) -> Optional[dict]:
    """Requests from the panel launcher (cogs/panel_launcher.py) are handed to the app
    in-process with the verified Discord user in the ASGI scope. That key can't be set
    by an HTTP client, so it only exists on requests the relay already authenticated."""
    return request.scope.get("panel_relay")


def _is_authed(request: Request) -> bool:
    relay = _relay(request)
    if relay is not None:
        return bool(_discord_admin_guilds(relay["user_id"]))
    token = request.cookies.get(SESSION_COOKIE)
    session = SESSIONS.get(token) if token else None
    if session is None:
        return False
    if session.get("user_id") is None:
        return PASSWORD_LOGIN  # password sessions die when password login gets switched off
    if not _discord_admin_guilds(session["user_id"]):
        SESSIONS.pop(token, None)  # admin permission gone -> logged out
        log.info("Panel session of %s ended: no longer administrator", session.get("name"))
        return False
    return True


def _panel_user(request: Request) -> Optional[str]:
    relay = _relay(request)
    if relay is not None:
        return relay.get("name")
    token = request.cookies.get(SESSION_COOKIE)
    session = SESSIONS.get(token) if token else None
    return session.get("name") if session else None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _new_session(user_id: Optional[int], name: str, request: Optional[Request] = None,
                 method: str = "password") -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user_id": user_id, "name": name, "created": time.time()}
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400 * 7,
                    secure=PANEL_PUBLIC_URL.startswith("https://"))
    if request is not None:
        # A long-lived random id per browser, so the access log can tell new devices apart (web/access_log.py).
        device = request.cookies.get(DEVICE_COOKIE) or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", device):
            device = secrets.token_urlsafe(24)
        resp.set_cookie(DEVICE_COOKIE, device, httponly=True, samesite="lax", max_age=86400 * 400,
                        secure=PANEL_PUBLIC_URL.startswith("https://"))
        from web.access_log import browser_label, record_and_notify
        record_and_notify(current_bot(), user_id=user_id, name=name, method=method, device_id=f"browser:{device}",
                          label=browser_label(request.headers.get("user-agent", "")), ip=_client_ip(request))
    return resp


def _redirect_uri(request: Request) -> str:
    base = PANEL_PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/oauth/callback"


def _client_id() -> str:
    if OAUTH_CLIENT_ID:
        return OAUTH_CLIENT_ID
    bot = current_bot()
    return str(bot.application_id) if bot and bot.application_id else ""


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
    if _relay(request) is not None and _is_authed(request):
        return RedirectResponse("/", status_code=303)  # the launcher is already logged in
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
        "authenticated": False,
        "oauth_enabled": OAUTH_ENABLED,
        "password_login": PASSWORD_LOGIN,
        "redirect_uri": _redirect_uri(request),
    })


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if not PASSWORD_LOGIN:
        return RedirectResponse("/login?error=password_disabled", status_code=303)
    if secrets.compare_digest(password, PANEL_PASSWORD):
        log.info("Panel login with password")
        return _new_session(None, "password login", request, "password")
    log.warning("Panel login with wrong password")
    return RedirectResponse("/login?error=wrong_password", status_code=303)


@app.get("/login/discord")
async def login_discord(request: Request):
    if not OAUTH_ENABLED or not _client_id():
        return RedirectResponse("/login?error=oauth_unavailable", status_code=303)
    from urllib.parse import urlencode
    state = secrets.token_urlsafe(24)
    params = urlencode({
        "client_id": _client_id(),
        "response_type": "code",
        "redirect_uri": _redirect_uri(request),
        "scope": "identify",
        "state": state,
        "prompt": "none",
    })
    resp = RedirectResponse(f"https://discord.com/oauth2/authorize?{params}", status_code=303)
    resp.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600,
                    secure=PANEL_PUBLIC_URL.startswith("https://"))
    return resp


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None,
                         error: Optional[str] = None):
    expected = request.cookies.get(STATE_COOKIE)
    if error or not code or not state or not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse("/login?error=oauth_failed", status_code=303)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.post(f"{DISCORD_API}/oauth2/token", data={
                "client_id": _client_id(),
                "client_secret": OAUTH_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(request),
            }) as r:
                if r.status != 200:
                    log.warning("Discord OAuth token exchange failed: HTTP %s", r.status)
                    return RedirectResponse("/login?error=oauth_failed", status_code=303)
                token = (await r.json()).get("access_token")
            async with http.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status != 200:
                    return RedirectResponse("/login?error=oauth_failed", status_code=303)
                user = await r.json()
    except aiohttp.ClientError:
        log.exception("Discord OAuth request failed")
        return RedirectResponse("/login?error=oauth_failed", status_code=303)
    user_id = int(user["id"])
    name = user.get("global_name") or user.get("username") or str(user_id)
    if not _discord_admin_guilds(user_id):
        log.warning("Panel login refused for %s (%s): not an administrator", name, user_id)
        return RedirectResponse("/login?error=not_admin", status_code=303)
    log.info("Panel login via Discord: %s (%s)", name, user_id)
    resp = _new_session(user_id, name, request, "discord")
    resp.delete_cookie(STATE_COOKIE)
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        SESSIONS.pop(token, None)
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


# -------- Routes: Minecraft logs live -------------------------------------

def _mclog_cog():
    bot = current_bot()
    return bot.get_cog("McLogs") if bot else None


@app.get("/mclog", response_class=HTMLResponse)
async def mclog_live_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    # Over the Discord relay every refresh is a Discord message - refresh less often there.
    return templates.TemplateResponse(request, "mclog_live.html", {
        "authenticated": True,
        "live_ms": 5000 if _relay(request) else 2000,
        "available": _mclog_cog() is not None,
    })


@app.get("/api/mclog/live")
async def api_mclog_live(request: Request, since: int = 0):
    if not _is_authed(request):
        raise HTTPException(401)
    cog = _mclog_cog()
    if cog is None:
        return JSONResponse({"error": "Minecraft logs are not loaded."}, status_code=503)
    return await cog.live_snapshot(since, 200)


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
async def faqs_save(request: Request, guild_id: str = Form(...), name: str = Form(...), answer: str = Form(...),
                    keywords: str = Form("")):
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
    from cogs.community import parse_keywords
    data.setdefault(str(guild.id), {})[key] = {
        "answer": answer.strip(),
        "keywords": parse_keywords(keywords),
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
    return _page(request, "announce.html", channels=_writable_channels())


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


# -------- Routes: scheduled announcements ----------------------------------

def _writable_channels() -> list[dict]:
    channels = []
    for g in _guilds():
        for ch in sorted(g.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
            if ch.permissions_for(g.me).send_messages:
                channels.append({"id": ch.id, "label": f"{g.name} / #{ch.name}"})
    return channels


@app.get("/scheduled", response_class=HTMLResponse)
async def scheduled_page(request: Request, edit: Optional[str] = None):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.scheduler import load_items, local_str, tz
    bot = current_bot()
    items = sorted(load_items(), key=lambda i: i["send_at"])
    for i in items:
        ch = bot.get_channel(i["channel_id"]) if bot else None
        i["channel_label"] = f"#{ch.name}" if ch else str(i["channel_id"])
        i["local"] = local_str(i["send_at"])
        i["input_value"] = i["local"].replace(" ", "T")
    editing = next((i for i in items if i["id"] == edit), None) if edit else None
    return _page(request, "scheduled.html", items=items, editing=editing, channels=_writable_channels(),
                 timezone=str(tz()))


@app.post("/scheduled/save")
async def scheduled_save(request: Request, channel_id: str = Form(""), when: str = Form(...), title: str = Form(...),
                         message: str = Form(...), ping: str = Form("none"), item_id: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.scheduler import add_item, parse_when, update_item, validate_time
    ts = parse_when(when)
    error = validate_time(ts)
    if error:
        return _redirect("/scheduled" + (f"?edit={item_id}" if item_id else ""), err=error.replace("`", ""))
    if not title.strip() or not message.strip():
        return _redirect("/scheduled", err="Title and text are required.")
    if ping not in ("none", "everyone") and not ping.startswith("role:"):
        ping = "none"
    if item_id:
        item = update_item(item_id, title=title.strip()[:256], message=message.replace("\r", "").strip()[:4000],
                           send_at=ts, ping=ping)
        if item is None:
            return _redirect("/scheduled", err="That announcement was already sent or deleted.")
        return _redirect("/scheduled", msg=f"Announcement {item_id} updated.")
    bot = current_bot()
    try:
        channel = bot.get_channel(int(channel_id)) if bot else None
    except ValueError:
        channel = None
    if not isinstance(channel, discord.TextChannel):
        return _redirect("/scheduled", err="Channel not found.")
    item = add_item(channel.guild.id, channel.id, title, message, ts, ping, "web panel")
    log.info("Announcement %s scheduled via panel", item["id"])
    return _redirect("/scheduled", msg=f"Scheduled for {when.replace('T', ' ')}.")


@app.post("/scheduled/delete")
async def scheduled_delete(request: Request, item_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.scheduler import delete_item
    if not delete_item(item_id):
        return _redirect("/scheduled", err="Not found (maybe already sent).")
    return _redirect("/scheduled", msg="Deleted.")


# -------- Routes: statistics -----------------------------------------------

def _stats_payload(guild_id: str | None) -> dict:
    from datetime import date as _date, timedelta as _td
    from cogs.stats import load_stats, today_key
    bot = current_bot()
    cog = bot.get_cog("Stats") if bot else None
    data = cog.data if cog is not None else load_stats()
    guilds = _guilds()
    guild = _guild(guild_id) if guild_id else (guilds[0] if guilds else None)
    gid = str(guild.id) if guild else next(iter(data["guilds"]), None)
    g = data["guilds"].get(gid or "", {"days": {}, "ratings": [], "claims": {}, "channel_names": {}})

    end = _date.fromisoformat(today_key())
    days = []
    for offset in range(179, -1, -1):
        key = (end - _td(days=offset)).isoformat()
        d = g["days"].get(key, {})
        days.append({
            "date": key,
            "members": d.get("members"),
            "joins": d.get("joins", 0),
            "leaves": d.get("leaves", 0),
            "messages": d.get("messages", 0),
            "tickets_opened": d.get("tickets_opened", 0),
            "tickets_closed": d.get("tickets_closed", 0),
            "channels": d.get("channels", {}),
        })
    names = dict(g.get("channel_names", {}))
    if guild:
        for ch in guild.channels:
            names[str(ch.id)] = ch.name  # current names win over the recorded ones

    levels = []
    levels_cog = bot.get_cog("Levels") if bot else None
    level_data = levels_cog.data if levels_cog else _load_json(DATA_DIR / "levels.json")
    if gid:
        from cogs.levels import level_from_xp
        ranked = sorted(level_data.get(gid, {}).items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)[:10]
        for uid, entry in ranked:
            member = guild.get_member(int(uid)) if guild else None
            levels.append({"name": member.display_name if member else entry.get("name", uid),
                           "level": level_from_xp(entry.get("xp", 0)), "xp": entry.get("xp", 0),
                           "messages": entry.get("messages", 0)})
    claims = sorted(g.get("claims", {}).values(), key=lambda c: c.get("count", 0), reverse=True)[:10]
    return {
        "guild_name": guild.name if guild else None,
        "members_now": guild.member_count if guild else None,
        "days": days,
        "channel_names": names,
        "ratings": g.get("ratings", []),
        "claims": claims,
        "levels": levels,
    }


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, guild: Optional[str] = None):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    payload = _stats_payload(guild)
    return _page(request, "stats.html", payload=payload, selected_guild=guild)


@app.get("/api/stats")
async def api_stats(request: Request, guild: Optional[str] = None):
    if not _is_authed(request):
        raise HTTPException(401)
    return _stats_payload(guild)


# -------- Routes: embed builder --------------------------------------------

_MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def _http_url(value: str) -> Optional[str]:
    value = (value or "").strip()
    return value if value.startswith(("https://", "http://")) and len(value) <= 2000 else None


def build_embed_message(payload: dict) -> tuple[Optional[str], Optional[discord.Embed], Optional[discord.ui.View]]:
    """Turn the builder's JSON into content/embed/view. Raises ValueError with a readable message."""
    content = (payload.get("content") or "").strip()[:2000] or None
    e = payload.get("embed") or {}
    embed = None
    has_embed = any((e.get(k) or "").strip() for k in ("title", "description", "image", "thumbnail", "author", "footer")) \
        or e.get("fields")
    if has_embed:
        color_text = (e.get("color") or "#5865f2").lstrip("#")
        try:
            color = int(color_text, 16)
        except ValueError:
            raise ValueError("Invalid color.")
        embed = discord.Embed(
            title=(e.get("title") or "").strip()[:256] or None,
            description=(e.get("description") or "").strip()[:4096] or None,
            color=color,
            url=_http_url(e.get("url", "")) if (e.get("title") or "").strip() else None,
            timestamp=datetime.now(timezone.utc) if e.get("timestamp") else None,
        )
        if (e.get("author") or "").strip():
            embed.set_author(name=e["author"].strip()[:256], icon_url=_http_url(e.get("author_icon", "")))
        if _http_url(e.get("thumbnail", "")):
            embed.set_thumbnail(url=_http_url(e["thumbnail"]))
        if _http_url(e.get("image", "")):
            embed.set_image(url=_http_url(e["image"]))
        if (e.get("footer") or "").strip():
            embed.set_footer(text=e["footer"].strip()[:2048])
        fields = e.get("fields") or []
        if len(fields) > 25:
            raise ValueError("Max. 25 fields.")
        for f in fields:
            name, value = (f.get("name") or "").strip(), (f.get("value") or "").strip()
            if not name or not value:
                raise ValueError("Every field needs a name and a value.")
            embed.add_field(name=name[:256], value=value[:1024], inline=bool(f.get("inline")))
        if len(embed) > 6000:
            raise ValueError(f"The embed is too long ({len(embed)} of 6000 characters).")
    buttons = payload.get("buttons") or []
    view = None
    if buttons:
        if len(buttons) > 25:
            raise ValueError("Max. 25 buttons.")
        view = discord.ui.View(timeout=None)
        for b in buttons:
            url = _http_url(b.get("url", ""))
            label = (b.get("label") or "").strip()[:80]
            if not url or not label:
                raise ValueError("Every button needs a label and an http(s) link.")
            view.add_item(discord.ui.Button(label=label, url=url))
    if not content and embed is None:
        raise ValueError("The message is empty.")
    return content, embed, view


@app.get("/embeds", response_class=HTMLResponse)
async def embeds_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return _page(request, "embeds.html", channels=_writable_channels())


@app.get("/api/embeds/load")
async def embeds_load(request: Request, link: str):
    if not _is_authed(request):
        raise HTTPException(401)
    bot = current_bot()
    m = _MESSAGE_LINK.search(link or "")
    if not m or bot is None:
        return JSONResponse({"error": "That's not a message link."}, status_code=400)
    channel = bot.get_channel(int(m.group(2)))
    if not isinstance(channel, discord.TextChannel):
        return JSONResponse({"error": "Channel not found."}, status_code=404)
    try:
        message = await channel.fetch_message(int(m.group(3)))
    except discord.HTTPException:
        return JSONResponse({"error": "Message not found."}, status_code=404)
    if message.author.id != bot.user.id:
        return JSONResponse({"error": "Only messages sent by the bot can be edited."}, status_code=400)
    e = message.embeds[0] if message.embeds else None
    buttons = [{"label": c.label, "url": c.url} for row in message.components for c in getattr(row, "children", [])
               if getattr(c, "url", None)]
    return {
        "channel_id": str(channel.id),
        "content": message.content,
        "embed": {
            "title": e.title or "", "url": e.url or "", "description": e.description or "",
            "color": f"#{e.color.value:06x}" if e and e.color else "#5865f2",
            "author": e.author.name or "", "author_icon": e.author.icon_url or "",
            "thumbnail": e.thumbnail.url or "", "image": e.image.url or "",
            "footer": e.footer.text or "", "timestamp": bool(e.timestamp),
            "fields": [{"name": f.name, "value": f.value, "inline": f.inline} for f in e.fields],
        } if e else {},
        "buttons": buttons,
    }


@app.post("/api/embeds/send")
async def embeds_send(request: Request):
    if not _is_authed(request):
        raise HTTPException(401)
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415)
    payload = await request.json()
    bot = current_bot()
    if bot is None or not bot.is_ready():
        return JSONResponse({"error": "Bot not connected."}, status_code=503)
    try:
        content, embed, view = build_embed_message(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    edit_link = (payload.get("edit_link") or "").strip()
    kwargs = {"content": content, "embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
    try:
        if edit_link:
            m = _MESSAGE_LINK.search(edit_link)
            channel = bot.get_channel(int(m.group(2))) if m else None
            if not isinstance(channel, discord.TextChannel):
                return JSONResponse({"error": "Message to edit not found."}, status_code=404)
            message = await channel.fetch_message(int(m.group(3)))
            if message.author.id != bot.user.id:
                return JSONResponse({"error": "Only messages sent by the bot can be edited."}, status_code=400)
            kwargs.pop("allowed_mentions")
            await message.edit(**kwargs, view=view)
            log.info("Embed edited via panel in #%s", channel.name)
            return {"ok": True, "text": f"Message in #{channel.name} updated.", "link": message.jump_url}
        channel = bot.get_channel(int(payload.get("channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            return JSONResponse({"error": "Pick a channel."}, status_code=400)
        if view is not None:
            kwargs["view"] = view
        message = await channel.send(**kwargs)
        log.info("Embed posted via panel in #%s", channel.name)
        return {"ok": True, "text": f"Posted in #{channel.name}.", "link": message.jump_url}
    except discord.HTTPException as e:
        return JSONResponse({"error": f"Discord refused it: {e.text or e}"}, status_code=400)


# -------- Routes: role panel editor ----------------------------------------

async def _publish_role_panel() -> str:
    from cogs.role_panel import refresh_panel_message
    bot = current_bot()
    results = [await refresh_panel_message(bot, g) for g in _guilds()] if bot else []
    return ", ".join(results) or "bot not connected"


@app.get("/role-panel", response_class=HTMLResponse)
async def role_panel_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.role_panel import BUTTON_STYLES, assignable_roles, get_panel_config
    guilds = _guilds()
    roles = assignable_roles(guilds[0]) if guilds else []
    config = get_panel_config()
    buttons = config.get("buttons", [])
    existing = {b["role"] for b in buttons}
    return _page(request, "role_panel.html", config=config, buttons=buttons, styles=list(BUTTON_STYLES),
                 roles=[r for r in roles if r.name not in existing])


async def _save_role_panel(config: dict, msg: str):
    from cogs.role_panel import save_panel_config
    save_panel_config(config)
    status_text = await _publish_role_panel()
    log.info("Role panel changed via web panel: %s (%s)", msg, status_text)
    return _redirect("/role-panel", msg=f"{msg} - {status_text}.")


@app.post("/role-panel/text")
async def role_panel_text(request: Request, title: str = Form(...), description: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.role_panel import get_panel_config
    config = get_panel_config()
    config["title"] = title.strip()[:256] or "Pick Your Roles"
    config["description"] = description.replace("\r", "").strip()[:4000]
    return await _save_role_panel(config, "Text saved")


@app.post("/role-panel/add")
async def role_panel_add(request: Request, role_id: str = Form(...), label: str = Form(""), emoji: str = Form(""),
                         style: str = Form("secondary"), row: int = Form(0)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.role_panel import BUTTON_STYLES, MAX_BUTTONS, assignable_roles, get_panel_config
    guilds = _guilds()
    role = next((r for r in (assignable_roles(guilds[0]) if guilds else []) if str(r.id) == role_id), None)
    if role is None:
        return _redirect("/role-panel", err="That role can't be self-assigned (too powerful, or above the bot's role).")
    config = get_panel_config()
    buttons = config.setdefault("buttons", [])
    if any(b["role"] == role.name for b in buttons):
        return _redirect("/role-panel", err="That role already has a button.")
    if len(buttons) >= MAX_BUTTONS:
        return _redirect("/role-panel", err="Max. 25 buttons.")
    row = max(0, min(4, row))
    if sum(1 for b in buttons if b.get("row", 0) == row) >= 5:
        return _redirect("/role-panel", err=f"Row {row + 1} is full (5 buttons) - pick another row.")
    emoji = emoji.strip()
    if emoji:
        parsed = discord.PartialEmoji.from_str(emoji)
        if parsed.id is None and (len(emoji) > 8 or emoji.isascii()):
            return _redirect("/role-panel", err="Emoji must be a single emoji or a custom emoji like <:name:123>.")
    buttons.append({
        "role": role.name,
        "label": (label.strip() or role.name)[:80],
        "emoji": emoji or None,
        "style": style if style in BUTTON_STYLES else "secondary",
        "row": row,
    })
    return await _save_role_panel(config, f"Button for {role.name} added")


@app.post("/role-panel/change")
async def role_panel_change(request: Request, index: int = Form(...), action: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.role_panel import get_panel_config
    config = get_panel_config()
    buttons = config.get("buttons", [])
    if not 0 <= index < len(buttons):
        return _redirect("/role-panel", err="Button not found.")
    if action == "remove":
        removed = buttons.pop(index)
        return await _save_role_panel(config, f"Button for {removed['role']} removed")
    if action in ("up", "down"):
        other = index - 1 if action == "up" else index + 1
        if 0 <= other < len(buttons):
            buttons[index], buttons[other] = buttons[other], buttons[index]
        return await _save_role_panel(config, "Order changed")
    return _redirect("/role-panel", err="Unknown action.")


# -------- Routes: members (moderation from the panel / the app) -------------

def _panel_actor(request: Request):
    """Who acts in the panel: id (if known) + name, shaped like a discord user for the mod log."""
    from types import SimpleNamespace
    relay = _relay(request)
    token = request.cookies.get(SESSION_COOKIE)
    session = SESSIONS.get(token) if token else None
    uid = (relay or {}).get("user_id") or (session or {}).get("user_id")
    name = _panel_user(request) or "panel"
    return SimpleNamespace(id=uid, name=name, mention=f"<@{uid}>" if uid else f"{name} (panel)")


class _Actor:
    def __init__(self, ns):
        self.id, self.name, self.mention = ns.id, ns.name, ns.mention

    def __str__(self):
        return f"{self.name} (panel)"


def _member_extra(guild: discord.Guild, m: discord.Member, flags: dict, join_order: dict) -> dict:
    """Extra team-only info for the members page: level, invites, join number, flags, voice."""
    bot = current_bot()
    extra: dict = {"join_number": join_order.get(m.id)}
    levels = bot.get_cog("Levels") if bot else None
    if levels is not None:
        from cogs.levels import level_from_xp
        entry = levels.data.get(str(guild.id), {}).get(str(m.id))
        if entry:
            ranked = [uid for uid, _ in levels._ranked(guild.id)]
            extra["level"] = level_from_xp(int(entry.get("xp", 0)))
            extra["xp"] = int(entry.get("xp", 0))
            extra["messages"] = int(entry.get("messages", 0))
            extra["rank"] = ranked.index(str(m.id)) + 1 if str(m.id) in ranked else None
    invites = bot.get_cog("Invites") if bot else None
    if invites is not None:
        g = invites.data.get(str(guild.id), {})
        info = g.get("members", {}).get(str(m.id)) or {}
        if info.get("inviter"):
            inviter = guild.get_member(int(info["inviter"]))
            extra["invited_by"] = inviter.display_name if inviter else f"ID {info['inviter']}"
        elif info.get("code"):
            extra["invited_by"] = f"invite {info['code']}"
        mine = g.get("inviters", {}).get(str(m.id))
        if mine:
            joins = mine.get("joins", 0)
            extra["invited"] = f"{joins - mine.get('left', 0)} still here ({joins} joined)"
    flag = flags.get(str(m.id))
    if flag:
        when = datetime.fromtimestamp(flag["ts"]).strftime("%Y-%m-%d") if flag.get("ts") else ""
        extra["flagged"] = f"{flag.get('count', 1)}x" + (f", last {when}" if when else "")
    voice = getattr(m, "voice", None)
    if voice and voice.channel:
        extra["voice"] = voice.channel.name
    return extra


def _member_rows(guild: discord.Guild, q: str) -> list[dict]:
    q = q.strip().lower().lstrip("@")
    if not q:
        return []
    warns = _load_json(WARNINGS_FILE).get(str(guild.id), {})
    modlog = _load_json(DATA_DIR / "modlog.json").get(str(guild.id), {})
    flags = _load_json(DATA_DIR / "flagged_accounts.json").get(str(guild.id), {})
    joined = sorted((m for m in guild.members if m.joined_at), key=lambda m: m.joined_at)
    join_order = {m.id: i + 1 for i, m in enumerate(joined)}
    rows = []
    for m in guild.members:
        if m.bot:
            continue
        hay = f"{m.name} {m.display_name} {m.global_name or ''} {m.id}".lower()
        if q not in hay:
            continue
        rows.append({
            "id": m.id, "name": str(m), "display": m.display_name, "avatar": m.display_avatar.replace(size=64).url,
            "joined": m.joined_at.strftime("%Y-%m-%d") if m.joined_at else "?",
            "created": m.created_at.strftime("%Y-%m-%d"),
            "roles": [r.name for r in m.roles[1:]][-6:],
            "timed_out": bool(m.timed_out_until and m.timed_out_until > datetime.now(timezone.utc)),
            "warnings": len(warns.get(str(m.id), [])),
            "history": [f"{e.get('timestamp', '')[:10]} {e.get('action')}: {e.get('reason', '')}"[:120]
                        for e in modlog.get(str(m.id), [])[-5:]],
            "extra": _member_extra(guild, m, flags, join_order),
        })
        if len(rows) >= 25:
            break
    return rows


@app.get("/members", response_class=HTMLResponse)
async def members_page(request: Request, q: str = "", guild_id: str = ""):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    guilds = _guilds()
    guild = _guild(guild_id) if guild_id else (guilds[0] if guilds else None)
    return _page(request, "members.html", q=q, guild=guild, rows=_member_rows(guild, q) if guild else [])


@app.post("/members/action")
async def members_action(request: Request, guild_id: str = Form(...), user_id: str = Form(...),
                         action: str = Form(...), reason: str = Form(""), duration: str = Form("1h"), q: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from urllib.parse import quote
    back = f"/members?guild_id={guild_id}&q={quote(q)}"
    guild = _guild(guild_id)
    member = guild.get_member(int(user_id)) if guild and user_id.isdigit() else None
    if member is None:
        return _redirect(back, err="Member not found (left the server?).")
    from cogs.common import parse_duration
    from cogs.moderation import _ban_notice, add_warning, mod_action_embed, record_action, send_to_mod_logs
    actor = _Actor(_panel_actor(request))
    reason = (reason.strip() or "No reason given")[:400]
    try:
        if action == "warn":
            count = add_warning(guild.id, member, actor, reason)
            await send_to_mod_logs(guild, mod_action_embed("Member Warned", 0xF39C12, member, actor, reason,
                                                           {"Total warnings": str(count), "Via": "panel"}))
            try:
                await member.send(f"You were warned in **{guild.name}**.\nReason: {reason}")
            except discord.HTTPException:
                pass
            text = f"{member} warned ({count} warning(s) now)."
        elif action == "timeout":
            delta = parse_duration(duration)
            if delta is None or delta.total_seconds() < 60 or delta.days > 28:
                return _redirect(back, err="Timeout: use 10m, 1h, 1d ... (max. 28 days).")
            await member.timeout(delta, reason=f"By {actor}: {reason}")
            record_action(guild.id, member.id, "timeout", actor, reason, duration=duration)
            await send_to_mod_logs(guild, mod_action_embed("Member Timed Out", 0xE67E22, member, actor, reason,
                                                           {"Duration": duration, "Via": "panel"}))
            text = f"{member} timed out for {duration}."
        elif action == "untimeout":
            await member.timeout(None, reason=f"By {actor}")
            record_action(guild.id, member.id, "untimeout", actor, reason)
            text = f"Timeout of {member} removed."
        elif action == "kick":
            await member.kick(reason=f"By {actor}: {reason}")
            record_action(guild.id, member.id, "kick", actor, reason)
            await send_to_mod_logs(guild, mod_action_embed("Member Kicked", 0xE67E22, member, actor, reason, {"Via": "panel"}))
            text = f"{member} kicked."
        elif action == "ban":
            await _ban_notice(guild, member, reason)
            await member.ban(reason=f"By {actor}: {reason}")
            record_action(guild.id, member.id, "ban", actor, reason)
            await send_to_mod_logs(guild, mod_action_embed("Member Banned", 0xC0392B, member, actor, reason, {"Via": "panel"}))
            text = f"{member} banned."
        else:
            return _redirect(back, err="Unknown action.")
    except discord.Forbidden:
        return _redirect(back, err="The bot isn't allowed to do that (role too high or missing permission).")
    except discord.HTTPException as exc:
        return _redirect(back, err=f"Discord refused it: {exc.text or exc}")
    log.info("Panel moderation by %s: %s %s", actor, action, member)
    return _redirect(back, msg=text)


# -------- Routes: today at a glance (dashboard) ------------------------------

_ONLINE_CACHE: dict[int, tuple[float, Optional[int]]] = {}


async def _online_count(guild: discord.Guild) -> Optional[int]:
    """Members online (Discord's approximate count - the bot has no presence intent). Cached 60s."""
    cached = _ONLINE_CACHE.get(guild.id)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    bot = current_bot()
    value = None
    try:
        full = await bot.fetch_guild(guild.id, with_counts=True)
        value = full.approximate_presence_count
    except Exception:
        value = cached[1] if cached else None
    _ONLINE_CACHE[guild.id] = (time.time(), value)
    return value


def _count_pending(path: Path, collection: str, status: str, guild_id: int, guild_key: str = "guild") -> int:
    items = _load_json(path).get(collection, {})
    return sum(1 for e in items.values() if isinstance(e, dict) and e.get("status") == status
               and e.get(guild_key, guild_id) == guild_id)


async def _overview() -> dict:
    bot = current_bot()
    if bot is None or not bot.is_ready() or not bot.guilds:
        return {"ready": False}
    guild = bot.guilds[0]
    tickets = [t for t in _active_tickets() if t["guild_id"] == guild.id]
    out: dict = {
        "ready": True,
        "online": await _online_count(guild),
        "members": guild.member_count or 0,
        "in_voice": sum(len(c.members) for c in guild.voice_channels + list(guild.stage_channels)),
        "tickets": len(tickets),
        "urgent": sum(1 for t in tickets if t["channel_name"].startswith("\U0001F534")),
        "pending": {
            "Ban appeals": _count_pending(DATA_DIR / "appeals.json", "appeals", "pending", guild.id),
            "Invite requests": _count_pending(DATA_DIR / "invite_requests.json", "requests", "pending", guild.id),
            "Emoji suggestions": _count_pending(DATA_DIR / "emoji_suggestions.json", "suggestions", "open", guild.id),
            "Open bugs": _count_pending(DATA_DIR / "bugs.json", "bugs", "open", guild.id),
        },
    }
    try:
        from cogs.stats import _ACTIVE, today_key
        day = (_ACTIVE.data if _ACTIVE else _load_json(DATA_DIR / "stats.json")).get("guilds", {}) \
            .get(str(guild.id), {}).get("days", {}).get(today_key(), {})
        out["today"] = {"joins": day.get("joins", 0), "leaves": day.get("leaves", 0),
                        "messages": day.get("messages", 0), "tickets_opened": day.get("tickets_opened", 0)}
    except Exception:
        out["today"] = {}
    from cogs.common import get_setting
    daily = get_setting("downloads_daily") or {}
    from cogs.stats import today_key as _today
    if daily.get("date") == _today():
        out["downloads"] = {"total": daily.get("last", 0), "today": daily.get("last", 0) - daily.get("start", 0)}
    try:
        from cogs.bot_health import errors_since
        errors = errors_since(86400)
        last = [{"t": int(t), "where": name.replace("setup-bot.", ""), "msg": msg[:140]}
                for t, name, msg in errors[-3:][::-1]]
        out["errors"] = {"count": len(errors), "last": last}
    except Exception:
        out["errors"] = {"count": 0, "last": []}
    return out


@app.get("/api/overview")
async def api_overview(request: Request):
    if not _is_authed(request):
        raise HTTPException(401)
    return await _overview()


# -------- Routes: feature switches -------------------------------------------

@app.get("/features", response_class=HTMLResponse)
async def features_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.common import FEATURES, feature_states
    states = feature_states()
    groups: dict[str, list] = {}
    for key, (group, name, what) in FEATURES.items():
        groups.setdefault(group, []).append({"key": key, "name": name, "what": what, "on": states[key]})
    return _page(request, "features.html", groups=groups)


@app.post("/features/toggle")
async def features_toggle(request: Request, key: str = Form(...), on: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.common import FEATURES, set_feature
    if key not in FEATURES:
        return _redirect("/features", err="Unknown feature.")
    enable = on == "1"
    set_feature(key, enable)
    name = FEATURES[key][1]
    log.info("Feature %s switched %s in the panel by %s", key, "on" if enable else "off", _panel_actor(request).name)
    return _redirect("/features", msg=f"{name} is now {'on' if enable else 'off'}.")


# -------- Routes: panel access log -------------------------------------------

@app.get("/access", response_class=HTMLResponse)
async def access_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from web.access_log import overview
    data = overview()
    fmt = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"  # noqa: E731
    for d in data["devices"]:
        d["first_s"], d["last_s"] = fmt(d.get("first")), fmt(d.get("last"))
    for e in data["events"]:
        e["t_s"] = fmt(e.get("t"))
    return _page(request, "access.html", devices=data["devices"], events=data["events"][:100])


@app.post("/access/forget")
async def access_forget(request: Request, device: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from web.access_log import forget_device
    if not forget_device(device):
        return _redirect("/access", err="Device not found.")
    return _redirect("/access", msg="Device forgotten - if it's used again, you get a new-device DM.")


# -------- Routes: command statistics ----------------------------------------

@app.get("/command-stats", response_class=HTMLResponse)
async def command_stats_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    from cogs.command_stats import all_commands, load, summary
    bot = current_bot()
    stats = summary(load())
    cmds = all_commands(bot) if bot else []
    rows = [{"key": k, **stats.get(k, {"total": 0, "week": 0, "last": None})} for k in cmds]
    rows += [{"key": k, **v} for k, v in stats.items() if k not in cmds and not k.startswith("button ")]
    rows.sort(key=lambda r: (-r["total"], r["key"]))
    buttons = sorted(({"key": k[7:], **v} for k, v in stats.items() if k.startswith("button ")),
                     key=lambda r: -r["total"])
    for r in rows + buttons:
        r["last_text"] = datetime.fromtimestamp(r["last"]).strftime("%Y-%m-%d %H:%M") if r.get("last") else "-"
    return _page(request, "command_stats.html", rows=rows, buttons=buttons,
                 unused=sum(1 for r in rows if r["total"] == 0))


# -------- Routes: notifications for the app ---------------------------------

@app.get("/api/push/wait")
async def push_wait(request: Request, since: int = 0, timeout: float = 20):
    if not _is_authed(request):
        raise HTTPException(401)
    from cogs import push
    items = await push.wait(since, max(1.0, min(timeout, 20.0)))
    return {"seq": push.seq(), "boot": push.BOOT, "items": items}
