#!/usr/bin/env python3
"""Bot panel launcher for "__SERVER_NAME__".

Run it (python3 open_bot_panel.py) and the bot's web panel opens in your browser.
It runs on this PC as long as this script runs and talks to the bot through
Discord - nothing to set up, works from anywhere. Needs Python 3.8+, nothing else.

Tailscale: if this PC is in a Tailscale network, the launcher asks at the start:
1 = panel only on this PC, 2 = others in your Tailscale network may watch it too
(read-only) at http://<your Tailscale IP>:<port>/ - send them that address.

This file contains your personal panel key. Don't share it; /panel-launcher revoke
makes it stop working. Made by /panel-launcher get (cogs/panel_launcher.py).
"""

import base64
import email.utils
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBHOOK = "__WEBHOOK_URL__"
KEY_ID = "__KEY_ID__"
SECRET = base64.b64decode("__SECRET__")
SERVER_NAME = "__SERVER_NAME__"

VERSION = 1
PREFIX = "panel-relay:1"
PORTS = range(8765, 8800)
USER_AGENT = "DiscordBot (https://github.com/ErikEdits/discord-bot, 1) PanelLauncher"
ANSWER_TIMEOUT = 45
MAX_BODY = 8 * 1024 * 1024

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
CHOICE_SECONDS = 10
GUEST_BLOCKED = ("/__launcher", "/logout", "/oauth", "/login")   # (guests can only look)

GATE = secrets.token_hex(24)   # browser cookie, so other websites can't use this local server
state = {"offset": 0.0, "port": 0, "guest_port": 0, "ts_ip": None, "ts_name": None, "user": ""}
static_cache: dict = {}
relay_slots = threading.Semaphore(3)


class RelayError(Exception):
    pass


# -------- Discord ----------------------------------------------------------

def discord_request(method, url, data=None, content_type=None, tries=8):
    """One Discord API call with rate-limit and retry handling. Returns (status, body)."""
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT})
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, headers, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, headers, body = exc.code, exc.headers, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            if attempt >= 3:
                raise RelayError(f"Can't reach Discord: {getattr(exc, 'reason', exc)}")
            time.sleep(0.5 * attempt)
            continue
        date = headers.get("Date") if headers else None
        if date:
            try:
                state["offset"] = email.utils.parsedate_to_datetime(date).timestamp() - time.time()
            except (TypeError, ValueError):
                pass
        if status == 429:
            try:
                wait = float(json.loads(body).get("retry_after", 1))
            except (ValueError, AttributeError):
                wait = 1.0
            time.sleep(min(max(wait, 0.05), 30) + 0.05)
            continue
        if status >= 500:
            time.sleep(0.5 * attempt)
            continue
        if headers and headers.get("X-RateLimit-Remaining") == "0":
            try:
                time.sleep(min(float(headers.get("X-RateLimit-Reset-After", "0")), 30))
            except ValueError:
                pass
        return status, body
    raise RelayError("Discord is busy (rate limited) - try again in a moment.")


def multipart(payload: dict, body: bytes):
    boundary = "----panel" + secrets.token_hex(12)
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"body.bin\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + body + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def relay(method: str, path: str, content_type: str, body: bytes):
    """Send one HTTP request to the bot through Discord. Returns (status, content type, location, body)."""
    nonce = secrets.token_hex(12)
    t = int(time.time() + state["offset"])
    body_hash = hashlib.sha256(body).hexdigest()
    signed = "\n".join([KEY_ID, nonce, str(t), method, path, content_type, body_hash]).encode()
    header = {"v": VERSION, "k": KEY_ID, "n": nonce, "t": t, "m": method, "p": path, "c": content_type,
              "b": body_hash, "s": hmac.new(SECRET, signed, hashlib.sha256).hexdigest()}
    content = f"{PREFIX} req {json.dumps(header, separators=(',', ':'))}"
    if len(content) > 2000:
        return 414, "text/plain", "", b"Address too long for the relay."
    payload = {"content": content, "flags": 4, "allowed_mentions": {"parse": []}}
    if body:
        payload["attachments"] = [{"id": 0, "filename": "body.bin"}]
        data, ctype = multipart(payload, body)
    else:
        data, ctype = json.dumps(payload).encode(), "application/json"
    status, raw = discord_request("POST", WEBHOOK + "?wait=true", data, ctype)
    if status in (401, 404):
        raise RelayError("The relay webhook is gone - get a new launcher file with /panel-launcher get.")
    if status != 200:
        raise RelayError(f"Discord refused the request (HTTP {status}): {raw[:200]!r}")
    message_id = json.loads(raw)["id"]
    deadline = time.time() + ANSWER_TIMEOUT
    delay = 0.3
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(delay + 0.1, 1.0)
        status, raw = discord_request("GET", f"{WEBHOOK}/messages/{message_id}")
        if status == 404:
            raise RelayError("The request disappeared from the relay channel.")
        if status != 200:
            continue
        msg = json.loads(raw)
        text = msg.get("content") or ""
        if not text.startswith(PREFIX + " res "):
            continue
        answer = json.loads(text[len(PREFIX) + 5:])
        if answer.get("n") != nonce:
            continue
        data = b""
        attachments = msg.get("attachments") or []
        if attachments:
            status, data = discord_request("GET", attachments[0]["url"])
            if status != 200:
                raise RelayError(f"Couldn't download the answer (HTTP {status}).")
        return int(answer.get("s") or 502), answer.get("c") or "", answer.get("l") or "", data
    raise RelayError("The bot didn't answer in time - is it online?")


# -------- Local web server -------------------------------------------------

def page(title: str, text: str) -> bytes:
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Bot Panel</title></head>"
            f"<body style='font-family:sans-serif;max-width:640px;margin:60px auto'><h2>{title}</h2>"
            f"<p>{text}</p></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "PanelLauncher"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def reply(self, status, ctype="", body=b"", headers=None):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def do_HEAD(self):
        self.handle_any()

    guest = False   # True on the Tailscale address: read-only, no gate cookie

    def guest_hosts(self):
        port = state["guest_port"]
        hosts = {f"{state['ts_ip']}:{port}"}
        if state["ts_name"]:
            hosts |= {f"{state['ts_name']}:{port}", f"{state['ts_name'].split('.')[0]}:{port}"}
        return hosts

    def handle_any(self):
        if self.guest:
            return self.handle_guest()
        port = state["port"]
        if self.headers.get("Host") not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            return self.reply(403, "text/plain", b"Forbidden")
        if self.path.startswith("/__launcher/start"):
            if self.path == f"/__launcher/start?t={GATE}":
                return self.reply(303, headers={"Location": "/",
                                                "Set-Cookie": f"panel_gate={GATE}; Path=/; HttpOnly; SameSite=Strict"})
            return self.reply(403, "text/html; charset=utf-8", page("Old link", "Use the link shown in the launcher window."))
        cookies = self.headers.get("Cookie") or ""
        if f"panel_gate={GATE}" not in [c.strip() for c in cookies.split(";")]:
            return self.reply(403, "text/html; charset=utf-8",
                              page("Open the panel from the launcher",
                                   "Use the link shown in the launcher window (or restart the launcher)."))
        self.forward()

    def handle_guest(self):
        """Someone in the Tailscale network: may look at every page, but not change anything."""
        host = (self.headers.get("Host") or "").lower().rstrip(".")
        if host not in {h.lower() for h in self.guest_hosts()}:
            return self.reply(403, "text/plain", b"Forbidden")
        if self.command not in ("GET", "HEAD"):
            print(f"  [guest {self.client_address[0]}] {self.command} {self.path[:60]} -> blocked (read-only)")
            return self.reply(403, "text/html; charset=utf-8",
                              page("Read-only", "You're watching the panel read-only - changes aren't possible."))
        if self.path.startswith(GUEST_BLOCKED):
            return self.reply(303, headers={"Location": "/"})
        self.forward()

    def forward(self):
        if self.path == "/favicon.ico":
            return self.reply(204)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.reply(413, "text/html; charset=utf-8", page("Too large", "Max. 8 MB through the relay."))
        body = self.rfile.read(length) if length else b""
        cacheable = self.command == "GET" and self.path.startswith("/static/")
        if cacheable and self.path in static_cache:
            return self.reply(200, *static_cache[self.path])
        started = time.time()
        try:
            with relay_slots:
                status, ctype, location, data = relay(self.command, self.path,
                                                      self.headers.get("Content-Type") or "", body)
        except RelayError as exc:
            print(f"  ! {self.command} {self.path}: {exc}")
            return self.reply(502, "text/html; charset=utf-8", page("Can't reach the bot", str(exc)))
        who = f"[guest {self.client_address[0]}] " if self.guest else ""
        print(f"  {who}{self.command} {self.path[:70]} -> {status} ({time.time() - started:.1f}s)")
        if cacheable and status == 200:
            static_cache[self.path] = (ctype, data)
        if self.guest and ctype.startswith("text/html"):
            data = data.replace(b"<body>", b"<body>" + guest_banner(), 1)
        self.reply(status, ctype, data, {"Location": location} if location else None)


class GuestHandler(Handler):
    guest = True


def guest_banner() -> bytes:
    who = state["user"].replace("&", "&amp;").replace("<", "&lt;")
    return ("<div style='background:#f0b132;color:#1e1f22;padding:8px 16px;font:600 14px sans-serif;"
            f"text-align:center'>&#128064; Read-only view of the bot panel, shared by {who}</div>"
            "<style>form[method=post],form[method=POST]{display:none!important}</style>").encode()


# -------- Tailscale ----------------------------------------------------------

def tailscale_exe():
    found = shutil.which("tailscale")
    for path in (r"C:\Program Files\Tailscale\tailscale.exe",
                 "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if not found and os.path.exists(path):
            found = path
    return found


def tailscale_info():
    """(IPv4, MagicDNS name) of this PC in Tailscale, or (None, None)."""
    if os.environ.get("PANEL_LAUNCHER_TAILSCALE_IP"):
        return os.environ["PANEL_LAUNCHER_TAILSCALE_IP"], None
    ip = name = None
    try:   # the route to Tailscale's own address 100.100.100.100 leaves through the Tailscale adapter
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("100.100.100.100", 53))
        candidate = probe.getsockname()[0]
        probe.close()
        if ipaddress.ip_address(candidate) in TAILSCALE_NET:
            ip = candidate
    except (OSError, ValueError):
        pass
    exe = tailscale_exe()
    if exe:
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5).stdout
            me = json.loads(out).get("Self") or {}
            name = (me.get("DNSName") or "").rstrip(".") or None
            ip = ip or next((x for x in me.get("TailscaleIPs") or [] if "." in x), None)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return ip, (name if ip else None)


def read_choice(seconds: int) -> str:
    """'1' or '2' from the keyboard; '1' after `seconds` or without a keyboard."""
    if os.environ.get("PANEL_LAUNCHER_MODE") in ("1", "2"):
        return os.environ["PANEL_LAUNCHER_MODE"]
    if not sys.stdin or not sys.stdin.isatty():
        return "1"
    end = time.time() + seconds
    try:
        if os.name == "nt":
            import msvcrt
            while time.time() < end:
                if msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if key in "12":
                        return key
                    if key in "\r\n":
                        return "1"
                time.sleep(0.05)
            return "1"
        import select
        while time.time() < end:
            ready, _, _ = select.select([sys.stdin], [], [], end - time.time())
            if ready:
                key = sys.stdin.readline().strip()
                return key if key in ("1", "2") else "1"
    except (OSError, ValueError, ImportError):
        pass
    return "1"


def ask_share(ip: str, name) -> bool:
    print(f"  Tailscale found: {ip}" + (f" ({name})" if name else ""))
    print("    1 = panel only on this PC")
    print("    2 = others in your Tailscale network may watch it too (read-only)")
    print(f"  Choose 1 or 2 (in {CHOICE_SECONDS} s it starts with 1): ", end="", flush=True)
    choice = read_choice(CHOICE_SECONDS)
    print(choice)
    return choice == "2"


def start_guest_server():
    """Second server on the Tailscale address (same port if free)."""
    for port in [state["port"]] + [p for p in PORTS if p != state["port"]]:
        try:
            server = ThreadingHTTPServer((state["ts_ip"], port), GuestHandler)
        except OSError:
            continue
        server.daemon_threads = True
        state["guest_port"] = port
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
        return server
    return None


def start_server():
    for port in PORTS:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
        server.daemon_threads = True
        state["port"] = port
        return server
    raise RelayError(f"No free port between {PORTS[0]} and {PORTS[-1]}.")


def fail(text: str):
    print("\n  " + text)
    if sys.stdin and sys.stdin.isatty():
        input("\n  Press Enter to close.")
    sys.exit(1)


def main():
    print(f"\n  Bot panel launcher - {SERVER_NAME}\n  Connecting to the bot through Discord...")
    try:
        status, raw = discord_request("GET", WEBHOOK)
        if status in (401, 404):
            fail("This launcher file no longer works (the relay was reset).\n"
                 "  Get a new one in Discord with /panel-launcher get.")
        status, _, _, data = relay("GET", "/__relay/ping", "", b"")
    except RelayError as exc:
        fail(str(exc))
    if status != 200:
        text = data.decode("utf-8", "replace")
        reason = text.split("<p>", 1)[-1].split("</p>", 1)[0] if "<p>" in text else text[:200]
        fail(f"The bot refused the launcher: {reason.replace('<code>', '').replace('</code>', '')}")
    info = json.loads(data)
    state["user"] = str(info.get("user") or "")
    print(f"  Connected as {info.get('user')} ({info.get('server')}).\n")
    ts_ip, ts_name = tailscale_info()
    share = bool(ts_ip) and ask_share(ts_ip, ts_name)
    server = start_server()
    guest_server = None
    if share:
        state["ts_ip"], state["ts_name"] = ts_ip, ts_name
        guest_server = start_guest_server()
        if guest_server is None:
            print(f"  ! Couldn't open the panel on {ts_ip} - only on this PC.")
    url = f"http://127.0.0.1:{state['port']}/__launcher/start?t={GATE}"
    print(f"\n  Panel: {url}")
    if guest_server is not None:
        port = state["guest_port"]
        print(f"  For others (read-only): http://{ts_ip}:{port}/"
              + (f"  or  http://{ts_name}:{port}/" if ts_name else ""))
        print("    Send them this address. They need Tailscale and access to this PC in your Tailscale network.")
        print("    If Windows asks about the firewall: allow access (private networks).")
    print("  The panel runs as long as this window is open. Close it (or press Ctrl+C) to stop.\n")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if guest_server is not None:
            guest_server.server_close()


if __name__ == "__main__":
    socket.setdefaulttimeout(60)
    main()
