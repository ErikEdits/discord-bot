"""Cloudflare mailbox for the Minecraft logs (no Discord code in here).

Discord lets a webhook post only about 30 messages per minute, which is far too little for a
busy server. Instead, the plugin sends its log lines to a small Cloudflare Worker (free plan,
address <name>.<subdomain>.workers.dev, no own domain needed) that keeps them in a D1 database
until the bot picks them up (cogs/mc_logs.py polls every few seconds).

setup_mailbox() builds everything with the admin's Cloudflare API token:
    verify token -> find account -> D1 database + table -> upload mclog_cloudflare/worker.js with
    the database and two random keys -> workers.dev address -> health check
The plugin side is mclog_cloudflare/CloudflareLogSender.java.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from pathlib import Path

import aiohttp

CF_API = "https://api.cloudflare.com/client/v4"
WORKER_NAME = "mclog-mailbox"
DB_NAME = "mclog-mailbox"
WORKER_FILE = Path(__file__).resolve().parent.parent / "mclog_cloudflare" / "worker.js"
JAVA_FILE = WORKER_FILE.with_name("CloudflareLogSender.java")
WORKERS_DEV_URL = "https://{name}.{subdomain}.workers.dev"   # (tests point this somewhere else)
COMPATIBILITY_DATE = "2024-09-23"
TABLE_SQL = ("CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
             "received INTEGER NOT NULL, body TEXT NOT NULL)")
GLOBAL_KEY_RE = re.compile(r"^[0-9a-f]{37}$")
TIMEOUT = aiohttp.ClientTimeout(total=30)

# What the token needs for each step (shown when Cloudflare says "no permission").
STEP_PERMISSION = {
    "account": "Account · Account Settings · Read",
    "database": "Account · D1 · Edit",
    "table": "Account · D1 · Edit",
    "worker": "Account · Workers Scripts · Edit",
    "subdomain": "Account · Workers Scripts · Edit",
}
STEP_TEXT = {
    "token": "Check the token", "account": "Find your Cloudflare account", "database": "Create the D1 database",
    "table": "Create the table", "worker": "Upload the mailbox worker", "subdomain": "Turn on the workers.dev address",
    "health": "Test the mailbox",
}

GUIDE = [
    ("1. Free Cloudflare account",
     "Sign up at https://dash.cloudflare.com/sign-up and confirm your e-mail. It's free - no credit card and "
     "no own domain needed."),
    ("2. Open Workers once",
     "In the dashboard click **Compute (Workers) → Workers & Pages** in the left menu. If Cloudflare asks for a "
     "*workers.dev subdomain*, pick any name (e.g. your name) - your mailbox address will be "
     "`mclog-mailbox.<name>.workers.dev`."),
    ("3. Create the API token",
     "Open https://dash.cloudflare.com/profile/api-tokens → **Create Token** → scroll down to "
     "**Create Custom Token** → **Get started**.\n"
     "• **Token name:** `Discord bot mclog`\n"
     "• **Permissions** (use **+ Add more** for the 2nd and 3rd row):\n"
     "  `Account` · `Workers Scripts` · `Edit`\n"
     "  `Account` · `D1` · `Edit`\n"
     "  `Account` · `Account Settings` · `Read`\n"
     "• **Account Resources:** `Include` · *your account*\n"
     "• Client IP Address Filtering and TTL: leave empty\n"
     "→ **Continue to summary** → **Create Token** → **Copy** the token. Cloudflare shows it **only once**."),
    ("4. Give it to the bot",
     "Run `/mclog cloudflare setup` and paste the token. The *Account ID* field is optional (only needed if your "
     "token can see several accounts - you find it on the Workers & Pages overview on the right, or in the "
     "address bar: `dash.cloudflare.com/<account id>`).\n"
     "The bot creates the mailbox by itself and shows you the **address** and the **key** for the plugin."),
    ("5. Plugin",
     "`/mclog cloudflare plugin` gives you the Java file and the `config.yml` lines with your address and key "
     "already filled in."),
    ("Good to know",
     "• Not the *Global API Key* - it has to be an **API Token**.\n"
     "• Free plan: 100,000 requests per day - plenty for a batch every 2 seconds.\n"
     "• The token is only kept in the bot's settings (never in backups). You can delete it any time on the API "
     "tokens page; the mailbox keeps working without it."),
]


class CloudflareError(Exception):
    def __init__(self, step: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.step, self.message, self.hint = step, message, hint


def clean_token(value: str) -> str:
    value = (value or "").strip().strip("\"'`").strip()
    return value[7:].strip() if value.lower().startswith("bearer ") else value


def _hint(step: str, status: int, errors: list[dict]) -> str | None:
    codes = {e.get("code") for e in errors}
    text = " ".join(str(e.get("message", "")) for e in errors).lower()
    if step == "subdomain":
        return ("Open **Workers & Pages** in the Cloudflare dashboard once and pick a workers.dev subdomain, then "
                "run setup again (`/mclog cloudflare guide`, step 2). The token needs **Account · Workers Scripts · "
                "Edit**.")
    if status in (401, 403) or codes & {10000, 9109} or "authentication" in text or "permission" in text:
        need = STEP_PERMISSION.get(step)
        if need:
            return f"The token is missing the permission **{need}** (see `/mclog cloudflare guide`, step 3)."
        return "The token was refused - copy it again or create a new one (`/mclog cloudflare guide`)."
    return None


async def cf_call(session: aiohttp.ClientSession, token: str, step: str, method: str, path: str, **kw):
    """One Cloudflare API call; returns "result" or raises CloudflareError."""
    try:
        async with session.request(method, CF_API + path, headers={"Authorization": f"Bearer {token}"},
                                   timeout=TIMEOUT, **kw) as r:
            status = r.status
            try:
                data = await r.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                data = {"success": False, "errors": [{"message": (await r.text())[:200]}]}
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise CloudflareError(step, f"Cloudflare not reachable ({type(exc).__name__})") from exc
    if not isinstance(data, dict) or not data.get("success"):
        errors = (data or {}).get("errors") or [] if isinstance(data, dict) else []
        message = "; ".join(f"{e.get('message')} (code {e.get('code')})" for e in errors) or f"HTTP {status}"
        raise CloudflareError(step, message, _hint(step, status, errors))
    return data.get("result")


async def setup_mailbox(token: str, account_id: str | None = None, progress=None,
                        keys: dict | None = None) -> dict:
    """Create (or update) the mailbox. Returns {account_id, account_name, db_id, url, ingest_key, bot_key,
    subdomain, healthy}. `progress(step)` is awaited before each step. `keys` keeps existing keys."""
    token = clean_token(token)
    account_id = (account_id or "").strip() or None

    async def step(name):
        if progress:
            await progress(name)

    if GLOBAL_KEY_RE.match(token):
        raise CloudflareError("token", "That's the Global API Key, not an API Token.",
                              "Create an **API Token** instead (`/mclog cloudflare guide`, step 3).")
    async with aiohttp.ClientSession() as s:
        await step("token")
        try:
            info = await cf_call(s, token, "token", "GET", "/user/tokens/verify")
        except CloudflareError:
            # account-owned tokens are verified per account
            if not account_id:
                try:
                    found = await cf_call(s, token, "token", "GET", "/accounts", params={"per_page": 50}) or []
                except CloudflareError:
                    found = []
                if not found:
                    raise CloudflareError("token", "Cloudflare doesn't accept this token.",
                                          "Copy the token again (no spaces) or create a new one "
                                          "(`/mclog cloudflare guide`).")
                account_id = found[0]["id"]
            info = await cf_call(s, token, "token", "GET", f"/accounts/{account_id}/tokens/verify")
        if (info or {}).get("status") not in (None, "active"):
            raise CloudflareError("token", f"The token is {info.get('status')}.", "Create a new token.")

        await step("account")
        if account_id:
            account_name = account_id
            try:
                acc = await cf_call(s, token, "account", "GET", f"/accounts/{account_id}")
                account_name = (acc or {}).get("name") or account_id
            except CloudflareError:
                pass  # works without "Account Settings: Read" if the id is given
        else:
            accounts = await cf_call(s, token, "account", "GET", "/accounts", params={"per_page": 50}) or []
            if not accounts:
                raise CloudflareError("account", "The token can't see any Cloudflare account.",
                                      "Set **Account Resources → Include → your account** on the token.")
            account_id, account_name = accounts[0]["id"], accounts[0].get("name") or accounts[0]["id"]

        await step("database")
        dbs = await cf_call(s, token, "database", "GET", f"/accounts/{account_id}/d1/database",
                            params={"name": DB_NAME}) or []
        db = next((d for d in dbs if d.get("name") == DB_NAME), None)
        if db is None:
            db = await cf_call(s, token, "database", "POST", f"/accounts/{account_id}/d1/database",
                               json={"name": DB_NAME})
        db_id = db.get("uuid") or db.get("id")

        await step("table")
        await cf_call(s, token, "table", "POST", f"/accounts/{account_id}/d1/database/{db_id}/query",
                      json={"sql": TABLE_SQL})

        await step("worker")
        keys = dict(keys or {})
        ingest_key = keys.get("ingest_key") or secrets.token_urlsafe(32)
        bot_key = keys.get("bot_key") or secrets.token_urlsafe(32)
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": COMPATIBILITY_DATE,
            "bindings": [
                {"type": "d1", "name": "DB", "id": db_id},
                {"type": "secret_text", "name": "INGEST_KEY", "text": ingest_key},
                {"type": "secret_text", "name": "BOT_KEY", "text": bot_key},
            ],
        }
        form = aiohttp.FormData()
        form.add_field("metadata", json.dumps(metadata), content_type="application/json")
        form.add_field("worker.js", WORKER_FILE.read_text("utf-8"), filename="worker.js",
                       content_type="application/javascript+module")
        await cf_call(s, token, "worker", "PUT", f"/accounts/{account_id}/workers/scripts/{WORKER_NAME}", data=form)

        await step("subdomain")
        subdomain = None
        try:
            subdomain = ((await cf_call(s, token, "subdomain", "GET", f"/accounts/{account_id}/workers/subdomain"))
                         or {}).get("subdomain")
        except CloudflareError:
            subdomain = None
        if not subdomain:
            wanted = re.sub(r"[^a-z0-9-]", "", (account_name or "mclog").lower().split("@")[0])[:20].strip("-")
            wanted = f"{wanted or 'mclog'}-{secrets.token_hex(3)}"
            subdomain = ((await cf_call(s, token, "subdomain", "PUT", f"/accounts/{account_id}/workers/subdomain",
                                        json={"subdomain": wanted})) or {}).get("subdomain") or wanted
        await cf_call(s, token, "subdomain", "POST",
                      f"/accounts/{account_id}/workers/scripts/{WORKER_NAME}/subdomain",
                      json={"enabled": True, "previews_enabled": False})
        url = WORKERS_DEV_URL.format(name=WORKER_NAME, subdomain=subdomain)

        await step("health")
        healthy = False
        for _ in range(12):   # a new workers.dev address can take a little while
            try:
                async with s.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200 and (await r.json(content_type=None)).get("ok"):
                        healthy = True
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError):
                pass
            await asyncio.sleep(5)
    return {"account_id": account_id, "account_name": account_name, "db_id": db_id, "url": url,
            "ingest_key": ingest_key, "bot_key": bot_key, "subdomain": subdomain, "healthy": healthy}


async def fetch_batches(session: aiohttp.ClientSession, url: str, bot_key: str, after: int,
                        limit: int = 100) -> dict:
    """{"batches": [{id, received, body}], "more": bool}. Raises on errors."""
    async with session.post(f"{url}/fetch", params={"after": str(after), "limit": str(limit)},
                            headers={"Authorization": f"Bearer {bot_key}"}, timeout=TIMEOUT) as r:
        if r.status == 401:
            raise PermissionError("the mailbox refused the bot key - run /mclog cloudflare setup again")
        if r.status != 200:
            raise RuntimeError(f"mailbox answered HTTP {r.status}")
        return await r.json(content_type=None)


async def health(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=10)) as r:
        return await r.json(content_type=None) if r.status == 200 else {"ok": False, "status": r.status}


def batch_lines(body: str, received_ms: int) -> list[tuple[int, str]]:
    """[(unix seconds, line)] of a batch: {"lines": [{"t": ms, "l": "..."}]} or plain text lines."""
    fallback = int(received_ms) // 1000
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        data = None
    out = []
    if isinstance(data, dict) and isinstance(data.get("lines"), list):
        for item in data["lines"]:
            if isinstance(item, dict):
                line, t = item.get("l") or item.get("line"), item.get("t") or item.get("ts")
            elif isinstance(item, str):
                line, t = item, None
            else:
                continue
            if not isinstance(line, str) or not line.strip():
                continue
            try:
                ts = int(t) // 1000 if t and int(t) > 10**11 else int(t) if t else fallback
            except (TypeError, ValueError):
                ts = fallback
            for part in line.splitlines():
                if part.strip():
                    out.append((ts, part.strip()))
        return out
    for part in (body or "").splitlines():
        if part.strip():
            out.append((fallback, part.strip()))
    return out
