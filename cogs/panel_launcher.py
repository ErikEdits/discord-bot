"""Panel launcher: open the web panel on your own PC, from anywhere, with no setup.

    /panel-launcher get [system]   DM yourself a launcher file (administrator)
    /panel-launcher status         show the relay and the active launcher files
    /panel-launcher revoke [member] make a launcher file stop working
    /panel-launcher reset          new relay webhook, every launcher file stops working

How it works
------------
The bot runs on a host whose panel port usually isn't reachable from outside. Both
the bot and your PC can reach Discord though, so Discord carries the traffic:

  browser -> launcher on your PC (http://localhost:8765)
          -> posts the request into the hidden #panel-relay channel through a webhook
          -> the bot answers it with the web panel (in-process, no open port needed)
          -> edits the answer into that message -> the launcher reads it -> browser

The launcher file (Windows .cmd running PowerShell, or a stdlib-only .py) contains
the relay webhook and a personal key. Every request is signed with that key
(HMAC-SHA256 over key id, nonce, time, method, path, content type and body hash)
and the key's owner must still have the Administrator permission, checked on every
request. A key is valid until it's revoked; getting a new file replaces your old one.

Wire format (content of the webhook message, body as attachment "body.bin"):
  request   panel-relay:1 req {"v":1,"k":key_id,"n":nonce,"t":unix,"m":"GET","p":"/x?y","c":type,"b":sha256,"s":hmac}
  response  panel-relay:1 res {"n":nonce,"s":status,"c":content_type,"l":location}
Handled messages are deleted after a short while so the channel stays empty.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import unquote

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, mark_bot_delete, save_json

log = logging.getLogger("setup-bot.panel_launcher")

STATE_FILE = DATA_DIR / "panel_launcher.json"
LAUNCHER_DIR = Path(__file__).resolve().parent.parent / "launcher"
RELAY_CHANNEL = "panel-relay"
RELAY_WEBHOOK = "Panel Relay"
PREFIX = "panel-relay:1"
PROTOCOL_VERSION = 1
MIN_LAUNCHER_VERSION = 1
MAX_CLOCK_SKEW = 600          # seconds; the launchers correct their clock with Discord's Date header
NONCE_TTL = 2 * MAX_CLOCK_SKEW
MAX_BODY = 8 * 1024 * 1024    # request and response bodies (Discord's upload limit is 10 MB)
APP_TIMEOUT = 25
DELETE_AFTER = 40             # handled relay messages are deleted after this many seconds
DISCORD_API = "https://discord.com/api/v10"

SYSTEMS = {
    "windows": ("panel_launcher.ps1", "Open-Bot-Panel.cmd"),
    "python": ("panel_launcher.py", "open_bot_panel.py"),
}

# Batch part that starts the PowerShell script below it (the whole file is valid batch
# *and* PowerShell: cmd skips the <# ... #> block comment's first line as a label).
CMD_HEADER = (
    "<# :\r\n"
    "@echo off\r\n"
    "title Bot Panel\r\n"
    "set \"PANEL_LAUNCHER_FILE=%~f0\"\r\n"
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"iex ([IO.File]::ReadAllText($env:PANEL_LAUNCHER_FILE))\"\r\n"
    "if errorlevel 1 pause\r\n"
    "exit /b\r\n"
    "#>\r\n"
)


# -------- State -----------------------------------------------------------

def load_state() -> dict:
    state = load_json(STATE_FILE, {})
    state.setdefault("relay", None)
    state.setdefault("keys", {})
    return state


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def sign(secret: bytes, k: str, n: str, t: int, m: str, p: str, c: str, b: str) -> str:
    message = "\n".join([k, n, str(t), m, p, c, b]).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _ascii(text: str) -> str:
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)


def build_launcher(system: str, webhook_url: str, key_id: str, secret_b64: str, server_name: str) -> tuple[str, bytes]:
    """Fill a launcher template with the relay webhook and the personal key."""
    template_name, file_name = SYSTEMS[system]
    text = (LAUNCHER_DIR / template_name).read_text(encoding="utf-8")
    name = _ascii(server_name)
    if system == "windows":
        values = {"__SERVER_NAME__": name.replace("'", "''")}
    else:
        values = {"__SERVER_NAME__": name.replace("\\", "\\\\").replace('"', '\\"')}
    values.update({"__WEBHOOK_URL__": webhook_url, "__KEY_ID__": key_id, "__SECRET__": secret_b64})
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    if system == "windows":
        text = CMD_HEADER + text.replace("\r\n", "\n").replace("\n", "\r\n")
    return file_name, text.encode("utf-8")


# -------- Calling the web panel in-process --------------------------------

async def call_panel(method: str, target: str, content_type: str, body: bytes, user: dict) -> tuple[int, dict, bytes]:
    """Run one HTTP request through the FastAPI app without a network socket.
    `user` lands in scope["panel_relay"], which the panel treats as a logged-in admin."""
    from web.app import app

    path, _, query = target.partition("?")
    headers = [(b"host", b"localhost"), (b"user-agent", b"panel-launcher")]
    if content_type:
        headers.append((b"content-type", content_type.encode("latin-1")))
    headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": unquote(path), "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"), "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 0), "server": ("localhost", 80), "panel_relay": user,
    }
    sent_body = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    result = {"status": 500, "headers": {}, "body": bytearray()}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            for key, value in message.get("headers", []):
                name = key.decode("latin-1").lower()
                if name in ("content-type", "location"):
                    result["headers"][name] = value.decode("latin-1")
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=APP_TIMEOUT)
    except Exception:
        # Starlette already sent its 500 response before re-raising; keep that.
        log.exception("Panel request %s %s failed", method, path)
    finally:
        disconnected.set()
    location = result["headers"].get("location", "")
    if location.startswith("http://localhost/"):
        # e.g. Starlette's trailing-slash redirect - keep the browser on the launcher's port
        result["headers"]["location"] = location[len("http://localhost"):]
    return result["status"], result["headers"], bytes(result["body"])


def error_page(status: int, title: str, text: str) -> tuple[int, dict, bytes]:
    page = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Bot Panel</title>"
        "<link rel='stylesheet' href='/static/style.css'></head><body><main>"
        f"<h1>{html.escape(title)}</h1><p>{text}</p></main></body></html>"
    )
    return status, {"content-type": "text/html; charset=utf-8"}, page.encode("utf-8")


# -------- Commands ---------------------------------------------------------

class PanelLauncher(commands.Cog):
    group = app_commands.Group(
        name="panel-launcher",
        description="Open the web panel on your own PC from anywhere (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot
        self.nonces: dict[str, float] = {}
        self.to_delete: dict[int, float] = {}   # message id -> delete after (unix time)
        self.semaphore = asyncio.Semaphore(4)
        self.lock = asyncio.Lock()
        self.purged = False
        self.cleanup_loop.start()

    def cog_unload(self):
        self.cleanup_loop.cancel()

    # -------- Relay channel + webhook ---------------------------------------

    def relay(self) -> dict | None:
        return load_state()["relay"]

    async def ensure_relay(self, guild: discord.Guild, *, reset: bool = False) -> tuple[discord.TextChannel, discord.Webhook, bool]:
        """Return the relay channel and webhook, (re)creating what's missing.
        The bool is True when the webhook is new - older launcher files then stop working."""
        async with self.lock:
            state = load_state()
            relay = state["relay"] or {}
            channel = guild.get_channel(relay.get("channel_id") or 0) if relay.get("guild_id") == guild.id else None
            if channel is None:
                me = guild.me
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                    manage_messages=True, manage_webhooks=True, attach_files=True),
                }
                category = discord.utils.find(lambda c: c.name.endswith("LOGS"), guild.categories)
                channel = await guild.create_text_channel(
                    RELAY_CHANNEL, category=category, overwrites=overwrites,
                    topic="Carries the web panel for /panel-launcher. Messages here are handled and deleted automatically.",
                    reason="Panel launcher relay")
                relay = {}
            webhook = None
            if relay.get("webhook_id") and not reset:
                try:
                    webhook = discord.utils.get(await channel.webhooks(), id=relay["webhook_id"])
                except discord.HTTPException:
                    webhook = None
            created = webhook is None or not webhook.token
            if created:
                if reset and relay.get("webhook_id"):
                    try:
                        old = discord.utils.get(await channel.webhooks(), id=relay["webhook_id"])
                        if old:
                            await old.delete(reason="Panel launcher reset")
                    except discord.HTTPException:
                        pass
                webhook = await channel.create_webhook(name=RELAY_WEBHOOK, reason="Panel launcher relay")
            state["relay"] = {"guild_id": guild.id, "channel_id": channel.id,
                              "webhook_id": webhook.id, "webhook_token": webhook.token}
            save_state(state)
            return channel, webhook, created

    def relay_webhook(self) -> discord.Webhook | None:
        relay = self.relay()
        if not relay or not relay.get("webhook_token"):
            return None
        return discord.Webhook.partial(relay["webhook_id"], relay["webhook_token"], client=self.bot)

    # -------- Relay: handling requests --------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        relay = self.relay()
        if not relay or message.channel.id != relay.get("channel_id"):
            return
        if message.webhook_id != relay.get("webhook_id") or not message.content.startswith(PREFIX + " req "):
            if message.author.id != getattr(self.bot.user, "id", None):
                self.to_delete[message.id] = time.time()  # stray message -> just remove it
            return
        asyncio.create_task(self.handle(message), name=f"panel-relay-{message.id}")

    def _check(self, header: dict, body: bytes) -> tuple[dict | None, tuple[int, dict, bytes] | None]:
        """Verify a request. Returns (key record, None) or (None, error response)."""
        state = load_state()
        key_id = str(header.get("k", ""))
        record = next((dict(r, user_id=int(uid)) for uid, r in state["keys"].items() if r.get("key_id") == key_id), None)
        if record is None:
            return None, error_page(401, "Launcher file not valid",
                                    "This launcher file was revoked or replaced. Get a new one with "
                                    "<code>/panel-launcher get</code> in Discord.")
        try:
            t = int(header["t"])
            expected = sign(base64.b64decode(record["secret"]), key_id, str(header["n"]), t, str(header["m"]),
                            str(header["p"]), str(header.get("c", "")), str(header.get("b", "")))
        except (KeyError, TypeError, ValueError):
            return None, error_page(400, "Bad request", "The launcher sent an invalid request.")
        if not hmac.compare_digest(expected, str(header.get("s", ""))):
            log.warning("Panel relay: bad signature for key %s", key_id)
            return None, error_page(401, "Launcher file not valid", "The request signature didn't match.")
        now = time.time()
        if abs(now - t) > MAX_CLOCK_SKEW:
            return None, error_page(401, "Clock out of sync", "The request was too old. Restart the launcher.")
        nonce = str(header["n"])
        if nonce in self.nonces:
            return None, error_page(409, "Duplicate request", "This request was already handled.")
        self.nonces[nonce] = now
        if hashlib.sha256(body).hexdigest() != (header.get("b") or hashlib.sha256(b"").hexdigest()):
            return None, error_page(400, "Bad request", "The request body was damaged on the way.")
        return record, None

    async def _answer(self, message: discord.Message, header: dict) -> tuple[int, dict, bytes]:
        body = b""
        attachment = discord.utils.get(message.attachments, filename="body.bin")
        if attachment is not None:
            if attachment.size > MAX_BODY:
                return error_page(413, "Too large", "The upload is too large for the relay (max. 8 MB).")
            body = await attachment.read()
        record, error = self._check(header, body)
        if error:
            return error
        if int(header.get("v") or 0) < MIN_LAUNCHER_VERSION:
            return error_page(426, "Launcher outdated", "Get a new launcher file with <code>/panel-launcher get</code>.")
        from web.app import _discord_admin_guilds, current_bot, set_bot
        if current_bot() is None:
            set_bot(self.bot)  # the panel's HTTP server may be switched off (PANEL_ENABLED=false)
        user_id = record["user_id"]
        guilds = _discord_admin_guilds(user_id)
        if not guilds:
            return error_page(403, "No access", "The owner of this launcher file is no longer an administrator.")
        self._touch(user_id)
        member = guilds[0].get_member(user_id)
        name = member.display_name if member else record.get("name", str(user_id))
        method, target = str(header["m"]).upper(), str(header["p"])
        if method not in ("GET", "POST", "HEAD") or not target.startswith("/") or not target.isascii():
            return error_page(400, "Bad request", "Unsupported request.")
        if target == "/__relay/ping":
            info = {"user": name, "server": guilds[0].name, "protocol": PROTOCOL_VERSION}
            return 200, {"content-type": "application/json"}, json.dumps(info).encode()
        status, headers, data = await call_panel(method, target, str(header.get("c", "")), body,
                                                 {"user_id": user_id, "name": name})
        if len(data) > MAX_BODY:
            return error_page(502, "Too large", "The page is too large for the relay.")
        return status, headers, data

    async def handle(self, message: discord.Message):
        async with self.semaphore:
            try:
                header = json.loads(message.content[len(PREFIX) + 5:])
                if not isinstance(header, dict) or "n" not in header:
                    raise ValueError("no nonce")
            except ValueError:
                self.to_delete[message.id] = time.time()
                return
            started = time.monotonic()
            try:
                status, headers, data = await self._answer(message, header)
            except Exception:
                log.exception("Panel relay request failed")
                status, headers, data = error_page(500, "Error", "The bot hit an error handling this request.")
            reply = {"n": header["n"], "s": status, "c": headers.get("content-type", ""), "l": headers.get("location", "")}
            webhook = self.relay_webhook()
            try:
                if webhook is None:
                    raise RuntimeError("relay webhook missing")
                files = [discord.File(io.BytesIO(data), filename="body.bin")] if data else []
                await webhook.edit_message(message.id, content=f"{PREFIX} res {json.dumps(reply)}",
                                           attachments=files, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                log.exception("Panel relay: couldn't send the answer")
            log.info("Panel relay %s %s -> %s (%.2fs)", header.get("m"), str(header.get("p"))[:80], status,
                     time.monotonic() - started)
            self.to_delete[message.id] = time.time() + DELETE_AFTER

    def _touch(self, user_id: int) -> None:
        state = load_state()
        record = state["keys"].get(str(user_id))
        if record and time.time() - (record.get("last_used") or 0) > 60:
            record["last_used"] = int(time.time())
            save_state(state)

    # -------- Cleanup -------------------------------------------------------

    @tasks.loop(seconds=15)
    async def cleanup_loop(self):
        relay = self.relay()
        if not relay:
            return
        channel = self.bot.get_channel(relay["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        now = time.time()
        if not self.purged:
            # Leftovers from before a restart.
            self.purged = True
            try:
                async for msg in channel.history(limit=200):
                    self.to_delete.setdefault(msg.id, now)
            except discord.HTTPException:
                pass
        due = [mid for mid, when in self.to_delete.items() if when <= now]
        for start in range(0, len(due), 100):
            chunk = due[start:start + 100]
            for mid in chunk:
                mark_bot_delete(mid)
                self.to_delete.pop(mid, None)
            try:
                await channel.delete_messages([discord.Object(mid) for mid in chunk], reason="Panel relay cleanup")
            except discord.HTTPException:
                for mid in chunk:  # e.g. one of them was already gone
                    try:
                        await channel.get_partial_message(mid).delete()
                    except discord.HTTPException:
                        pass
        for nonce, seen in list(self.nonces.items()):
            if now - seen > NONCE_TTL:
                self.nonces.pop(nonce, None)

    @cleanup_loop.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # -------- Commands ------------------------------------------------------

    @group.command(name="get", description="DM yourself a file that opens the web panel on your PC.")
    @app_commands.describe(system="Windows (.cmd, nothing to install) or Python (.py, any system with Python 3)")
    @app_commands.choices(system=[
        app_commands.Choice(name="Windows", value="windows"),
        app_commands.Choice(name="Python (Mac / Linux / Windows)", value="python"),
    ])
    async def get(self, interaction: discord.Interaction, system: str = "windows"):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if not self.bot.intents.message_content:
            await interaction.response.send_message(
                "The launcher needs the Message Content intent: set MESSAGE_CONTENT_INTENT=true (and enable it "
                "in the Discord Developer Portal), then restart the bot.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel, webhook, _ = await self.ensure_relay(interaction.guild)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Couldn't set up the relay channel: {exc}", ephemeral=True)
            return
        state = load_state()
        replaced = str(interaction.user.id) in state["keys"]
        key_id, secret = secrets.token_hex(8), base64.b64encode(secrets.token_bytes(32)).decode()
        state["keys"][str(interaction.user.id)] = {
            "key_id": key_id, "secret": secret, "name": interaction.user.display_name,
            "created": int(time.time()), "last_used": None, "system": system,
        }
        save_state(state)
        file_name, data = build_launcher(system, f"{DISCORD_API}/webhooks/{webhook.id}/{webhook.token}",
                                         key_id, secret, interaction.guild.name)
        if system == "windows":
            how = ("1. Download the file (Discord may warn about .cmd files - that's normal).\n"
                   "2. Double-click it. If Windows shows *\"Windows protected your PC\"*, click "
                   "**More info -> Run anyway**.\n"
                   "3. Your browser opens the panel. It runs as long as the black window is open.")
        else:
            how = ("1. Download the file.\n"
                   "2. Run it: `python3 open_bot_panel.py` (Windows: double-click or `py open_bot_panel.py`).\n"
                   "3. Your browser opens the panel. It runs as long as the script is running.")
        embed = discord.Embed(
            title="\U0001F5A5️ Your panel launcher",
            description=(f"Opens the **{interaction.guild.name}** web panel on your PC - from anywhere, "
                         "nothing to set up. It talks to the bot through Discord.\n\n" + how),
            color=0x5865F2,
        )
        embed.add_field(name="Keep it private",
                        value="This file is your personal admin key for the panel. Don't share it. "
                              "Lost it? `/panel-launcher revoke` makes it stop working.", inline=False)
        if replaced:
            embed.add_field(name="Note", value="Your previous launcher file no longer works.", inline=False)
        embed.set_footer(text="Works only while you have the Administrator permission.")
        try:
            await interaction.user.send(embed=embed, file=discord.File(io.BytesIO(data), filename=file_name))
            await interaction.followup.send("\U0001F4E8 Sent you the launcher file by DM.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("Your DMs are closed, so here it is (only you can see this):",
                                            embed=embed, file=discord.File(io.BytesIO(data), filename=file_name),
                                            ephemeral=True)
        log.info("Panel launcher file (%s) created for %s", system, interaction.user)

    @group.command(name="status", description="Show the relay and the active launcher files.")
    async def status(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        state = load_state()
        relay = state["relay"]
        embed = discord.Embed(title="Panel launcher", color=0x5865F2)
        channel = self.bot.get_channel(relay["channel_id"]) if relay else None
        embed.add_field(name="Relay channel", value=channel.mention if channel else "not set up yet (created by `get`)",
                        inline=False)
        lines = []
        for uid, record in state["keys"].items():
            used = f"<t:{record['last_used']}:R>" if record.get("last_used") else "never"
            lines.append(f"<@{uid}> - {record.get('system', 'windows')}, created <t:{record['created']}:d>, last used {used}")
        embed.add_field(name=f"Active launcher files ({len(lines)})", value="\n".join(lines)[:1024] or "none", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="revoke", description="Make a launcher file stop working (yours by default).")
    @app_commands.describe(member="Whose launcher file to revoke (default: yours)")
    async def revoke(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        target = member or interaction.user
        state = load_state()
        if state["keys"].pop(str(target.id), None) is None:
            await interaction.response.send_message(f"{target.mention} has no launcher file.", ephemeral=True)
            return
        save_state(state)
        log.info("Panel launcher file of %s revoked by %s", target, interaction.user)
        await interaction.response.send_message(f"\U0001F512 The launcher file of {target.mention} no longer works.",
                                                ephemeral=True)

    @group.command(name="reset", description="New relay webhook and revoke ALL launcher files.")
    async def reset(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = load_state()
        count = len(state["keys"])
        state["keys"] = {}
        save_state(state)
        try:
            await self.ensure_relay(interaction.guild, reset=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Keys revoked, but the webhook couldn't be renewed: {exc}", ephemeral=True)
            return
        log.info("Panel launcher reset by %s (%d files revoked)", interaction.user, count)
        await interaction.followup.send(f"\U0001F512 Relay renewed, {count} launcher file(s) revoked. "
                                        "Get a new one with `/panel-launcher get`.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(PanelLauncher(bot))
