#!/usr/bin/env python3
"""Bot panel launcher for "__SERVER_NAME__".

Run it (python3 open_bot_panel.py) and the bot's web panel opens in your browser.
It runs on this PC as long as this script runs and talks to the bot through
Discord - nothing to set up, works from anywhere. Needs Python 3.8+, nothing else.

This file contains your personal panel key. Don't share it; /panel-launcher revoke
makes it stop working. Made by /panel-launcher get (cogs/panel_launcher.py).
"""

import base64
import email.utils
import hashlib
import hmac
import json
import secrets
import socket
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

GATE = secrets.token_hex(24)   # browser cookie, so other websites can't use this local server
state = {"offset": 0.0, "port": 0}
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

    def handle_any(self):
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
        print(f"  {self.command} {self.path[:70]} -> {status} ({time.time() - started:.1f}s)")
        if cacheable and status == 200:
            static_cache[self.path] = (ctype, data)
        self.reply(status, ctype, data, {"Location": location} if location else None)


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
    server = start_server()
    url = f"http://127.0.0.1:{state['port']}/__launcher/start?t={GATE}"
    print(f"  Connected as {info.get('user')} ({info.get('server')}).\n")
    print(f"  Panel: {url}")
    print("  The panel runs as long as this window is open. Close it (or press Ctrl+C) to stop.\n")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    socket.setdefaulttimeout(60)
    main()
