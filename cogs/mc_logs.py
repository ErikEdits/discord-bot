"""Minecraft server logs: store, question answering and suspicious-activity DMs.

The EntityLagFix plugin posts the server's events through a webhook into a channel.
The bot reads that channel live (and catches up after a restart), keeps the events
for a few days in data/mc_logs.db (see cogs/mclog_core.py for how it stays small)
and answers questions about them.

    /mclog channel [channel]        set or show the log channel
    /mclog status                   what's stored, size, settings
    /mclog player name [from] [to]  what a player did (summary + timeline file)
    /mclog search ...               events by player, action, material, area and time
    /mclog ask question             question in plain language (German or English)
    /mclog import hours             read older messages of the channel (max. retention)
    /mclog report [period|days]     what came in + every line the bot didn't understand (.md file);
                                    with a period it reads the whole log chat of that time again
    /mclog trusted add|remove|list  players never reported (default ErikEdits, ColinTK)
    /mclog notify add|remove|list   who gets the suspicious-activity DMs
    /mclog cloudflare setup|guide|plugin|status|new-key|on|off
                                    receive the logs through a Cloudflare mailbox (no Discord limit)
    /mclog ai-key                   OpenRouter key for answers by a free AI model (optional)
    /mclog ai-model [model]         pick the free model

Without an AI key /mclog ask is answered by the bot itself (free): it reads players,
time, actions, materials and coordinates from the question and summarises the data.
With a key it asks a FREE OpenRouter model (only ids ending in ":free" whose price is
0 are ever used - checked against OpenRouter's model list before every question).
Suspicious events of untrusted players are sent by DM right away (see Detector).
When the channel has been (almost) quiet for an hour after activity - usually the
server went off - the owner gets the report file by DM: how many messages and lines
came in, signs of lost events (Discord's webhook limit, gaps) and every line the bot
didn't understand, so the parser can be improved.
All commands are administrator commands; the owner can unlock them with /grant.
"""

import asyncio
import io
import logging
import shutil
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, get_setting, is_admin, load_json, save_json, set_setting
from cogs.mclog_core import (Diag, Detector, LogStore, answer_question, fmt_event, fmt_pos, is_player_type,
                             known_type, parse_line, parse_question, parse_time_spec, player_summary, report_md,
                             report_text, split_lines, summary_text, understood_text, TITLE_RE)
from cogs.mclog_cloudflare import (GUIDE, JAVA_FILE, STEP_TEXT, CloudflareError, batch_lines, clean_token,
                                   fetch_batches, health, setup_mailbox)
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.mc_logs")

DB_FILE = DATA_DIR / "mc_logs.db"
STATE_FILE = DATA_DIR / "mc_logs.json"
KEY_SETTING = "openrouter_api_key"      # data/settings.json, never in backups
CF_SETTING = "mclog_cloudflare"         # data/settings.json: token, url, keys (never in backups)
CF_POLL_SECONDS = 5
CF_IDLE_AFTER = 120                       # no lines for this long -> poll only every CF_IDLE_POLL seconds
CF_IDLE_POLL = 30
CF_SILENT_SECONDS = 120                   # Cloudflare silent this long -> read the Discord channel again
DEDUP_SECONDS = 900                       # a line from Discord can be this much later than the same from Cloudflare
OPENROUTER = "https://openrouter.ai/api/v1"
ALERT_MAX_AGE = 15 * 60                   # don't alert about events older than this (catch-up)
CATCH_UP_MAX_MESSAGES = 40000
AI_CONTEXT_CHARS = 24000
PREFERRED_MODELS = ("deepseek", "llama-3.3-70b", "llama-4", "qwen3", "gemini", "mistral-small", "gpt-oss")
KNOWN_TITLES = {"EntityLagFix Server Log", "UptimeManager Server Log"}
TEXT_EXTENSIONS = (".txt", ".log", ".csv", ".json", ".yml", ".yaml")
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 7 * 1024 * 1024        # Discord file limit for bots is 8 MB (more with boosts)
INTERACTION_MINUTES = 14                  # slash command replies can be edited for 15 minutes
SCAN_SPEED = 5000                         # roughly messages per minute Discord lets the bot read
ACTION_CHOICES = ["BLOCK_BREAK", "BLOCK_PLACE", "PLAYER_COMMAND", "PLAYER_TELEPORT", "PLAYER_JOIN", "PLAYER_QUIT",
                  "PLAYER_DEATH", "PLAYER_KICK", "WORLD_CHANGE", "PLAYER_GAME_MODE_CHANGE"]


def _config() -> dict:
    return SERVER_TEMPLATE.get("mc_logs", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_config().get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def _state() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("channel_id", None)
    data.setdefault("trusted", None)       # None = SERVER_TEMPLATE default
    data.setdefault("notify", None)        # None = owner + alert_users from the template
    data.setdefault("ai_model", None)
    data.setdefault("newest_msg_id", None)
    data.setdefault("oldest_msg_id", None)
    data.setdefault("reported_until", 0)
    data.setdefault("cf_after", 0)         # last mailbox batch the bot stored  # message time up to which the "not understood" report was sent
    return data


def _cf_settings() -> dict:
    value = get_setting(CF_SETTING) or {}
    return value if isinstance(value, dict) else {}


def _report_config() -> dict:
    cfg = {"enabled": True, "quiet_minutes": 60, "quiet_max_messages": 30, "min_messages": 20, "send_to": []}
    cfg.update(_config().get("report", {}))
    return cfg


def _trusted() -> list[str]:
    saved = _state()["trusted"]
    return list(saved) if saved is not None else list(_config().get("trusted_players", []))


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _when(ts: int | None) -> str:
    return f"<t:{int(ts)}:f>" if ts else "-"


def _is_free(model: dict) -> bool:
    if not str(model.get("id", "")).endswith(":free"):
        return False
    try:
        return all(float(v or 0) == 0 for v in (model.get("pricing") or {}).values())
    except (TypeError, ValueError):
        return False


class KeyModal(discord.ui.Modal, title="OpenRouter API key"):
    key = discord.ui.TextInput(label="API key (empty = remove)", required=False, max_length=200,
                               placeholder="sk-or-v1-...")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        value = self.key.value.strip()
        if not value:
            set_setting(KEY_SETTING, None)
            await interaction.response.send_message("AI key removed. `/mclog ask` now answers on its own (free).",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        info = await self.cog.check_key(value)
        if info.get("invalid"):
            await interaction.followup.send("OpenRouter says this key is invalid - nothing saved.", ephemeral=True)
            return
        set_setting(KEY_SETTING, value)
        log.info("OpenRouter key set by %s", interaction.user)
        model = await self.cog.pick_model()
        text = "✅ Key saved. `/mclog ask` now uses a **free** AI model"
        text += f": `{model}`." if model else " (no free model reachable right now - the bot answers itself until then)."
        if info.get("error"):
            text += f"\n(Couldn't check the key: {info['error']})"
        await interaction.followup.send(text + "\nOnly models ending in `:free` with price 0 are ever used.",
                                        ephemeral=True)


class CloudflareModal(discord.ui.Modal, title="Cloudflare API token"):
    token = discord.ui.TextInput(label="API token (see /mclog cloudflare guide)", max_length=200,
                                 placeholder="Paste the token you created on dash.cloudflare.com")
    account = discord.ui.TextInput(label="Account ID (optional)", required=False, max_length=64,
                                   placeholder="Only needed if the token sees several accounts")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.run_cf_setup(interaction, self.token.value, self.account.value)


def cf_guide_embeds() -> list[discord.Embed]:
    embed = discord.Embed(
        title="☁️ Minecraft logs through Cloudflare - how to get the API token",
        description=("Discord only lets a webhook post about **30 messages per minute** - too little for the server "
                     "logs. With Cloudflare the plugin sends its logs to a small free **mailbox** (a Cloudflare "
                     "Worker) and the bot picks them up every few seconds. The bot builds the mailbox itself - "
                     "it only needs an **API token** from you:"),
        color=0xF38020)
    for name, value in GUIDE:
        embed.add_field(name=name, value=value[:1024], inline=False)
    return [embed]


def cf_config_text(cfg: dict) -> str:
    return (f"cloudflare:\n"
            f"  enabled: true\n"
            f"  url: \"{cfg.get('url', '')}/ingest\"\n"
            f"  key: \"{cfg.get('ingest_key', '')}\"\n"
            f"  interval-seconds: 2\n")


class McLogs(commands.Cog):
    group = app_commands.Group(name="mclog", description="Minecraft server logs (administrator).",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)
    trusted_group = app_commands.Group(name="trusted", description="Players that are never reported.",
                                       parent=group)
    notify_group = app_commands.Group(name="notify", description="Who gets suspicious-activity DMs.", parent=group)
    cf_group = app_commands.Group(name="cloudflare", description="Receive the logs through Cloudflare (no Discord limit).",
                                  parent=group)

    def __init__(self, bot):
        self.bot = bot
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mclog-db")
        self.store: LogStore | None = None
        self.detector = Detector(_config(), _trusted())
        self.buffer: list = []
        self.diag = Diag()
        self.players: set[str] = set()
        self.importing = False
        self.scanning = 0           # chat scan running: messages read + 1
        self.cf_session: aiohttp.ClientSession | None = None
        self.cf_data_at = 0.0       # last time the mailbox had lines
        self.cf_ok_at = 0.0         # last successful poll
        self.cf_last_poll = 0.0
        self.cf_error: str | None = None
        self.cf_batches = 0
        self.discord_lines: dict[str, deque] = {}   # lines read from Discord while Cloudflare was silent
        self.caught_up = False
        self._models_cache: tuple[float, list] = (0, [])
        self._state_dirty = False
        self._newest_msg_id = _state()["newest_msg_id"]

    async def db(self, fn, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.pool, lambda: fn(*args, **kwargs))

    async def cog_load(self):
        cfg = _config()
        self.store = await self.db(LogStore, DB_FILE, float(cfg.get("retention_days", 4)),
                                   float(cfg.get("max_db_mb", 100)), set(cfg.get("count_only_types", [])),
                                   dict(cfg.get("sample_seconds", {})))
        self.players = await self.db(self.store.known_players)
        self.flush_loop.start()
        self.maintenance_loop.start()
        self.report_loop.start()
        self.cf_loop.start()

    async def cog_unload(self):
        self.flush_loop.cancel()
        self.maintenance_loop.cancel()
        self.report_loop.cancel()
        self.cf_loop.cancel()
        if self.cf_session is not None:
            await self.cf_session.close()
        await self.flush()
        if self.store is not None:
            await self.db(self.store.close)
        self.pool.shutdown(wait=False)

    # -------- ingest -------------------------------------------------------

    def _channel_id(self) -> int | None:
        return _state()["channel_id"]

    def analyse(self, message: discord.Message, diag: Diag, players: set[str],
                extra: list[str] | None = None) -> list:
        """Events of a message; statistics and not-understood lines go into `diag`."""
        ts = int(message.created_at.timestamp())
        texts = [message.content]
        for embed in message.embeds:
            texts.append(embed.description)
            texts += [f.value for f in embed.fields]
            parts = [("title", embed.title), ("footer", embed.footer.text if embed.footer else None),
                     ("author", embed.author.name if embed.author else None)]
            parts += [("field", f.name) for f in embed.fields]
            for what, text in parts:
                if text and text not in KNOWN_TITLES and not TITLE_RE.match(text):
                    diag.unknown_line(ts, "embed", text, f"{what}: {text[:120]}", count_line=False)
        texts += extra or []
        lines, other = split_lines(texts)
        diag.message(ts, len(lines) + len(other), sum(len(t or "") for t in texts))
        for line in other:
            diag.unknown_line(ts, "text", line)
        events = []
        for line in lines:
            ev = self.analyse_line(line, ts, diag, players)
            if ev is not None:
                events.append(ev)
        return events

    @staticmethod
    def analyse_line(line: str, ts: int, diag: Diag, players: set[str]):
        """One event line -> Event (or None); not-understood parts go into `diag`."""
        ev = parse_line(line, ts, players)
        if ev is None:
            diag.unknown_line(ts, "format", line)
            return None
        diag.event(ts, ev.type)
        if not known_type(ev.type):
            diag.unknown_line(ts, "type", line, ev.type, count_line=False)
        if ev.player is None and is_player_type(ev.type):
            first = line.split("|")[1].split(" @ ")[0].strip()
            diag.unknown_line(ts, "no_player", line, f"{ev.type} | {first[:40] or '(empty)'}", count_line=False)
        if "Location" in line and ev.x is None:
            diag.unknown_line(ts, "no_location", line, count_line=False)
        if ev.player:
            players.add(ev.player)
        return ev

    def ingest_message(self, message: discord.Message, live: bool = True, extra: list[str] | None = None) -> int:
        ts = int(message.created_at.timestamp())
        events = self.analyse(message, self.diag, self.players, extra)
        alert = live and time.time() - ts < ALERT_MAX_AGE
        remember = _cf_settings().get("enabled") and live
        for ev in events:
            if remember:  # Discord stands in for Cloudflare: don't count these lines again when they arrive there
                self.discord_lines.setdefault(ev.raw, deque()).append(ts)
            self.buffer.append(ev)
            if live:  # imports run newest -> oldest and would confuse the detector's state
                for found in self.detector.feed(ev, alert=alert):
                    asyncio.create_task(self.send_alert(found))
        if self._newest_msg_id is None or message.id > self._newest_msg_id:
            self._newest_msg_id = message.id
            self._state_dirty = True
        return len(events)

    async def attachment_texts(self, message: discord.Message, diag: Diag) -> list[str]:
        """Text of the text files attached to a message (other files are noted in `diag`)."""
        extra = []
        ts = int(message.created_at.timestamp())
        for att in message.attachments:
            name = att.filename.lower()
            ext = name.rsplit(".", 1)[-1] if "." in name else name
            readable = name.endswith(TEXT_EXTENSIONS) or (att.content_type or "").startswith("text/")
            if not readable or att.size > ATTACHMENT_MAX_BYTES:
                why = "too big" if readable else "not a text file"
                diag.unknown_line(ts, "attachment", att.filename, f"{ext} file, {why} - not read", count_line=False)
                continue
            try:
                extra.append((await att.read()).decode("utf-8", "replace"))
                diag.unknown_line(ts, "attachment", att.filename, f"{ext} file - read, its lines were used",
                                  count_line=False)
            except (discord.HTTPException, discord.NotFound):
                diag.unknown_line(ts, "attachment", att.filename, "file couldn't be downloaded", count_line=False)
        return extra

    async def ingest(self, message: discord.Message, live: bool = True) -> int:
        """ingest_message() plus the lines of text files attached to the message."""
        extra = await self.attachment_texts(message, self.diag) if message.attachments else None
        return self.ingest_message(message, live, extra)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.channel.id != self._channel_id():
            return
        if not (message.webhook_id or message.author.bot) or message.author.id == getattr(self.bot.user, "id", 0):
            return
        if self.cloudflare_active():
            return  # the logs come through Cloudflare; the channel is only the stand-in
        await self.ingest(message)

    # -------- Cloudflare mailbox -------------------------------------------

    def cloudflare_active(self) -> bool:
        """Cloudflare is set up and delivered lines within the last CF_SILENT_SECONDS."""
        return bool(_cf_settings().get("enabled")) and time.time() - self.cf_data_at < CF_SILENT_SECONDS

    def ingest_cf_batch(self, batch: dict) -> int:
        received = int(batch.get("received") or time.time() * 1000)
        body = batch.get("body") or ""
        items = batch_lines(body, received)
        diag = self.diag
        diag.message(received // 1000, len(items), len(body), cloudflare=True)
        now = time.time()
        n = 0
        for ts, line in items:
            if "|" not in line:
                if not TITLE_RE.match(line.strip("*` ")):
                    diag.unknown_line(ts, "text", line)
                continue
            seen = self.discord_lines.get(line[:500])
            if seen:  # already read from the Discord channel while Cloudflare was silent
                match = next((t for t in seen if -5 <= t - ts <= DEDUP_SECONDS), None)
                if match is not None:
                    seen.remove(match)
                    continue
            ev = self.analyse_line(line, ts, diag, self.players)
            if ev is None:
                continue
            n += 1
            self.buffer.append(ev)
            for found in self.detector.feed(ev, alert=now - ts < ALERT_MAX_AGE):
                asyncio.create_task(self.send_alert(found))
        return n

    async def poll_cloudflare(self) -> int:
        """Pick up new batches from the mailbox. Returns the number of events."""
        cfg = _cf_settings()
        if not cfg.get("enabled") or not cfg.get("url") or self.store is None:
            return 0
        now = time.time()
        if now - self.cf_data_at > CF_IDLE_AFTER and now - self.cf_last_poll < CF_IDLE_POLL:
            return 0  # nothing came for a while (server off): ask less often
        self.cf_last_poll = now
        total = 0
        if self.cf_session is None or self.cf_session.closed:
            self.cf_session = aiohttp.ClientSession()
        for _ in range(20):   # follow "more" a few times when there's a backlog
            after = int(_state()["cf_after"] or 0)
            try:
                data = await fetch_batches(self.cf_session, cfg["url"], cfg["bot_key"], after)
            except Exception as exc:
                if not self.cf_error:
                    log.warning("Cloudflare mailbox: %s", exc)
                self.cf_error = str(exc)[:200] or type(exc).__name__
                return total
            if self.cf_error:
                log.info("Cloudflare mailbox reachable again")
            self.cf_error = None
            self.cf_ok_at = time.time()
            batches = data.get("batches") or []
            if not batches:
                break
            for batch in batches:
                total += self.ingest_cf_batch(batch)
            await self.flush()  # stored before telling the mailbox it may delete them
            state = _state()
            state["cf_after"] = max(int(b["id"]) for b in batches)
            save_json(STATE_FILE, state)
            self.cf_data_at = time.time()
            self.cf_batches += len(batches)
            if not data.get("more"):
                break
        cutoff = time.time() - DEDUP_SECONDS - 60
        for line in [k for k, q in self.discord_lines.items() if not q or q[-1] < cutoff]:
            self.discord_lines.pop(line, None)
        return total

    @tasks.loop(seconds=CF_POLL_SECONDS)
    async def cf_loop(self):
        try:
            await self.poll_cloudflare()
        except Exception:
            log.exception("Cloudflare mailbox poll failed")

    @cf_loop.before_loop
    async def _before_cf(self):
        await self.bot.wait_until_ready()

    async def flush(self):
        if self.buffer and self.store is not None:
            batch, self.buffer = self.buffer, []
            try:
                await self.db(self.store.add, batch)
            except Exception:
                log.exception("Couldn't store %d Minecraft log events", len(batch))
        if self.diag and self.store is not None:
            diag, self.diag = self.diag, Diag()
            try:
                await self.db(self.store.add_diag, diag)
            except Exception:
                log.exception("Couldn't store the Minecraft log statistics")
        if self._state_dirty:
            self._state_dirty = False
            state = _state()
            state["newest_msg_id"] = self._newest_msg_id
            if state["oldest_msg_id"] is None:
                state["oldest_msg_id"] = self._newest_msg_id
            save_json(STATE_FILE, state)

    @tasks.loop(seconds=5)
    async def flush_loop(self):
        if not self.caught_up:
            self.caught_up = True
            asyncio.create_task(self.catch_up())
        await self.flush()

    @flush_loop.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    async def catch_up(self):
        """Read what was posted while the bot was offline."""
        channel = self.bot.get_channel(self._channel_id() or 0)
        newest = _state()["newest_msg_id"]
        if not isinstance(channel, discord.TextChannel) or newest is None or _cf_settings().get("enabled"):
            return  # (with Cloudflare the mailbox kept everything while the bot was offline)
        cutoff = discord.utils.utcnow() - timedelta(days=float(_config().get("retention_days", 4)))
        after = max(discord.Object(newest).created_at, cutoff)
        count = lines = 0
        try:
            async for message in channel.history(limit=CATCH_UP_MAX_MESSAGES, after=after, oldest_first=True):
                if message.webhook_id or message.author.bot:
                    lines += await self.ingest(message, live=True)
                count += 1
                if count % 500 == 0:
                    await self.flush()
        except discord.HTTPException:
            log.warning("Minecraft log catch-up stopped early (Discord error)")
        await self.flush()
        if count:
            log.info("Minecraft log catch-up: %d messages, %d events", count, lines)

    @tasks.loop(minutes=15)
    async def maintenance_loop(self):
        if self.store is None:
            return
        max_mb = float(_config().get("max_db_mb", 100))
        try:
            free_mb = shutil.disk_usage(DATA_DIR).free / 1_048_576
            if free_mb < 300:
                max_mb = min(max_mb, max(10.0, self.store.size_mb() * 0.7))
                log.warning("Low disk space (%d MB free) - shrinking the Minecraft log store", free_mb)
        except OSError:
            pass
        info = await self.db(self.store.prune, None, max_mb)
        if info["trimmed_to"]:
            log.info("Minecraft log store over %s MB - removed data before %s", max_mb,
                     datetime.fromtimestamp(info["trimmed_to"], _tz()).strftime("%d.%m. %H:%M"))

    @maintenance_loop.before_loop
    async def _before_maintenance(self):
        await self.bot.wait_until_ready()

    # -------- report of what came in / wasn't understood ---------------------

    async def build_report(self, start: int, end: int, until_unknown: int | None = None) -> tuple[bytes, str]:
        await self.flush()
        unknowns = await self.db(self.store.unknowns, until_unknown)
        text = await self.db(report_text, self.store, start, end, _tz(), unknowns)
        return text.encode("utf-8"), f"mc-log-report-{datetime.fromtimestamp(end, _tz()):%Y-%m-%d-%H%M}.md"

    def report_recipients(self, guild: discord.Guild) -> list[discord.Member]:
        names = _report_config().get("send_to") or []
        members = [m for m in (guild.get_member_named(n) for n in names) if m]
        if not members and guild.owner:
            members = [guild.owner]
        return [m for m in members if not m.bot]

    async def check_quiet(self, now: float | None = None) -> bool:
        """After activity, an (almost) quiet hour sends the report to the owner. Returns True if sent."""
        cfg = _report_config()
        if not cfg.get("enabled", True) or self.store is None:
            return False
        await self.flush()
        now = int(now or time.time())
        quiet = int(float(cfg.get("quiet_minutes", 60)) * 60)
        reported = int(_state()["reported_until"] or 0)
        total, first = await self.db(self.store.messages_between, reported + 1, now)
        if total < int(cfg.get("min_messages", 20)) or first is None or first > now - quiet:
            return False  # (almost) nothing new, or the activity started less than an hour ago
        recent, _ = await self.db(self.store.messages_between, now - quiet, now)
        if recent > int(cfg.get("quiet_max_messages", 30)):
            return False
        channel = self.bot.get_channel(self._channel_id() or 0)
        guild = getattr(channel, "guild", None) or (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is None:
            return False
        tz = _tz()
        data, filename = await self.build_report(max(first, reported + 1), now, now)
        text = (f"⛏️ **Minecraft log report** {datetime.fromtimestamp(first, tz):%d.%m. %H:%M} - "
                f"{datetime.fromtimestamp(now, tz):%H:%M}\nThe log channel has been quiet for "
                f"{quiet // 60} minutes. The file shows what came in, signs of lost events and every line I "
                f"didn't understand.")
        sent = False
        for member in self.report_recipients(guild):
            try:
                await member.send(text, file=discord.File(io.BytesIO(data), filename=filename))
                sent = True
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Couldn't DM the Minecraft log report to %s", member)
        state = _state()
        state["reported_until"] = now
        save_json(STATE_FILE, state)
        await self.db(self.store.clear_unknowns, now)
        log.info("Minecraft log report (%d messages since %s) %s", total, datetime.fromtimestamp(first, tz),
                 "sent" if sent else "not delivered")
        return sent

    @tasks.loop(minutes=5)
    async def report_loop(self):
        try:
            await self.check_quiet()
        except Exception:
            log.exception("Minecraft log report failed")

    @report_loop.before_loop
    async def _before_report(self):
        await self.bot.wait_until_ready()

    # -------- alerts -------------------------------------------------------

    def recipients(self, guild: discord.Guild) -> list[discord.Member]:
        ids = _state()["notify"]
        members = []
        if ids is None:
            if guild.owner:
                members.append(guild.owner)
            for name in _config().get("alert_users", []):
                m = guild.get_member_named(name)
                if m and m not in members:
                    members.append(m)
        else:
            members = [m for m in (guild.get_member(int(i)) for i in ids) if m]
        return [m for m in members if not m.bot]

    async def send_alert(self, alert):
        text = f"{alert.title} - {alert.detail}"
        if self.store is not None:
            try:
                await self.db(self.store.add_alert, alert.ts, alert.player, alert.kind, text)
            except Exception:
                log.exception("Couldn't store alert")
        channel = self.bot.get_channel(self._channel_id() or 0)
        guild = getattr(channel, "guild", None) or (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is None:
            return
        embed = discord.Embed(title=f"⚠️ Minecraft: {alert.title}", description=alert.detail, color=0xE67E22,
                              timestamp=datetime.fromtimestamp(alert.ts).astimezone())
        if alert.x is not None:
            embed.add_field(name="Where", value=f"`{fmt_pos(alert.world, alert.x, alert.y, alert.z)}`", inline=True)
        embed.add_field(name="Player", value=f"`{alert.player}`", inline=True)
        if alert.suppressed:
            embed.add_field(name="Also", value=f"{alert.suppressed} similar event(s) right before", inline=True)
        if alert.raw:
            embed.add_field(name="Log line", value=f"```{alert.raw[:900]}```", inline=False)
        embed.set_footer(text="/mclog player " + alert.player + " for everything they did")
        for member in self.recipients(guild):
            try:
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
        log.info("Minecraft alert: %s", text)

    # -------- answers ------------------------------------------------------

    def _range(self, since: str | None, until: str | None) -> tuple[int, int] | str:
        now = datetime.now(_tz())
        start = parse_time_spec(since, now) if since else now - timedelta(hours=24)
        end = parse_time_spec(until, now) if until else now
        if start is None or end is None:
            return "Time not understood. Examples: `14:00`, `gestern 14:00`, `01.10. 9:30`, `3h` (3 hours ago)."
        if end <= start:
            end = start + timedelta(hours=1) if until is None else end + timedelta(days=1)
        oldest = now - timedelta(days=float(_config().get("retention_days", 4)))
        return _ts(max(start, oldest)), _ts(min(end, now))

    def _timeline_file(self, events, name: str) -> discord.File | None:
        if not events:
            return None
        tz = _tz()
        text = "\n".join(fmt_event(e, tz) for e in events)
        return discord.File(io.BytesIO(text.encode("utf-8")), filename=name)

    async def local_answer(self, q) -> tuple[str, list]:
        """Answer from the stored data without AI. Returns (text, events for the file)."""
        text, flt = await self.db(answer_question, self.store, q, _tz())
        events = await self.db(self.store.events, 5000, False, **flt) if flt else []
        return text, events

    async def build_ai_context(self, q, question: str) -> str:
        store, tz = self.store, _tz()
        span = (f"{datetime.fromtimestamp(q.start, tz):%Y-%m-%d %H:%M} to "
                f"{datetime.fromtimestamp(q.end, tz):%Y-%m-%d %H:%M} ({tz.key})")
        out = [f"Time range: {span}", f"Known players: {', '.join(sorted(self.players)) or 'none'}",
               f"Trusted players (admins): {', '.join(_trusted())}"]
        players = q.players
        if not players:
            who = await self.db(store.grouped, "player", 8, start=q.start, end=q.end)
            out.append("Activity per player: " + ", ".join(f"{p} {c}" for p, c in who if p != "-"))
            players = [p for p, _ in who if p != "-"][:4]
        for p in players[:5]:
            s = await self.db(player_summary, store, p, q.start, q.end, tz)
            out.append(f"--- {p} ---\n{summary_text(s, tz)}")
        flt = dict(start=q.start, end=q.end, players=q.players or None, types=q.types or None,
                   obj_words=q.obj_words or None, near=q.near)
        found = await self.db(store.events, 300, True, **flt)
        if not (q.types or q.obj_words or q.near):
            found = [e for e in found if e.type not in ("BLOCK_PLACE", "BLOCK_BREAK")] + \
                    [e for e in found if e.type in ("BLOCK_PLACE", "BLOCK_BREAK")][:80]
        out.append("Matching events (newest first):\n" + "\n".join(fmt_event(e, tz) for e in found))
        alerts = await self.db(store.alerts, q.start, q.end, q.players or None, 20)
        if alerts:
            out.append("Suspicious activity reported: " + "; ".join(
                f"{datetime.fromtimestamp(ts, tz):%d.%m. %H:%M} {text}" for ts, _, _, text in alerts))
        if q.spawns:
            top = await self.db(store.counted, q.start, q.end, 10)
            out.append("Mob spawns (counted): " + ", ".join(f"{o} {n}" for _, o, n in top))
        text = "\n\n".join(out)
        return text[:AI_CONTEXT_CHARS]

    # -------- OpenRouter (optional, free models only) -----------------------

    async def free_models(self) -> list[dict]:
        cached_at, models = self._models_cache
        if models and time.time() - cached_at < 3600:
            return models
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{OPENROUTER}/models", timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
            models = [m for m in data.get("data", []) if _is_free(m)]
            self._models_cache = (time.time(), models)
        except Exception as exc:
            log.warning("Couldn't load the OpenRouter model list: %s", exc)
        return models

    async def pick_model(self) -> str | None:
        """The configured model if it's still free, otherwise the best free one."""
        models = await self.free_models()
        ids = {m["id"] for m in models}
        wanted = _state()["ai_model"]
        if wanted in ids:
            return wanted

        def score(m):
            name = m["id"].lower()
            pref = next((i for i, p in enumerate(PREFERRED_MODELS) if p in name), len(PREFERRED_MODELS))
            return pref, -(m.get("context_length") or 0)
        usable = [m for m in models if (m.get("context_length") or 0) >= 16000] or models
        return min(usable, key=score)["id"] if usable else None

    async def check_key(self, key: str) -> dict:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{OPENROUTER}/key", headers={"Authorization": f"Bearer {key}"},
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status in (401, 403):
                        return {"invalid": True}
                    return (await r.json(content_type=None)).get("data", {}) if r.status == 200 else {
                        "error": f"HTTP {r.status}"}
        except Exception as exc:
            return {"error": str(exc)[:100]}

    async def ai_answer(self, key: str, question: str, context: str) -> tuple[str | None, str | None]:
        """(answer, problem). Only free models are used."""
        model = await self.pick_model()
        if model is None:
            return None, "no free AI model reachable right now"
        messages = [
            {"role": "system", "content": (
                "You analyse Minecraft server logs for the server admins. Answer only from the log data below. "
                "Start with a direct one-sentence answer to the question, then explain it: per player, which "
                "blocks/items, when (times as given) and where (coordinates), and anything unusual. Use short "
                "Discord markdown (bold numbers, bullet points), max. 20 lines. If the data doesn't answer the "
                "question, say what is missing. Answer in the language of the question. Times are local server "
                "time. Mob names like ZOMBIE are not players.")},
            {"role": "user", "content": f"Log data:\n{context}\n\nQuestion: {question}"},
        ]
        body = {"model": model, "messages": messages, "max_tokens": 1200, "temperature": 0.2}
        headers = {"Authorization": f"Bearer {key}", "HTTP-Referer": "https://github.com/ErikEdits/discord-bot",
                   "X-Title": "ErikEdits Bot"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{OPENROUTER}/chat/completions", json=body, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=120)) as r:
                    data = await r.json(content_type=None)
                    status = r.status
        except Exception as exc:
            return None, f"OpenRouter not reachable ({type(exc).__name__})"
        if status == 401:
            return None, "the OpenRouter key is invalid (/mclog ai-key)"
        if status == 429:
            return None, "the free AI limit is used up for now (OpenRouter free models have a daily limit)"
        if status != 200 or "choices" not in data:
            message = (data.get("error") or {}).get("message", "") if isinstance(data, dict) else ""
            return None, f"OpenRouter error {status} {message[:120]}".strip()
        answer = (data["choices"][0].get("message") or {}).get("content") or ""
        return (answer.strip() or None), (None if answer.strip() else "the AI returned an empty answer")

    # -------- commands -----------------------------------------------------

    async def _guard(self, interaction: discord.Interaction, need_store: bool = True) -> bool:
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return False
        if need_store and self.store is None:
            await interaction.response.send_message("The log store isn't ready yet - try again in a moment.",
                                                    ephemeral=True)
            return False
        return True

    async def _player_autocomplete(self, interaction: discord.Interaction, current: str):
        names = sorted(self.players, key=str.lower)
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    async def _action_autocomplete(self, interaction: discord.Interaction, current: str):
        types = ACTION_CHOICES
        if self.store is not None:
            stats_types = [t for t, _ in (await self.db(self.store.grouped, "type", 25))]
            types = list(dict.fromkeys(ACTION_CHOICES + stats_types))
        return [app_commands.Choice(name=t, value=t) for t in types if current.upper() in t][:25]

    @group.command(name="channel", description="Set (or show) the channel with the Minecraft server logs.")
    @app_commands.describe(channel="The channel the server's log webhook posts in")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await self._guard(interaction, need_store=False):
            return
        state = _state()
        if channel is None:
            current = self.bot.get_channel(state["channel_id"] or 0)
            await interaction.response.send_message(
                f"Log channel: {current.mention if current else 'not set'} - change it with `/mclog channel #channel`.",
                ephemeral=True)
            return
        await self.flush()
        state = _state()
        state["channel_id"] = channel.id
        last = channel.last_message_id
        # Live reading starts after `last`; /mclog import reads `last` and older.
        state["newest_msg_id"], state["oldest_msg_id"] = last, (last + 1 if last else None)
        save_json(STATE_FILE, state)
        self._newest_msg_id = last
        log.info("Minecraft log channel set to #%s by %s", channel.name, interaction.user)
        await interaction.response.send_message(
            f"✅ Reading Minecraft logs from {channel.mention} from now on.\n"
            f"Older messages: `/mclog import hours:24` (up to {int(float(_config().get('retention_days', 4)) * 24)}h).",
            ephemeral=True)

    @group.command(name="status", description="What's stored, how big it is, and the settings.")
    async def status(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.flush()
        st = await self.db(self.store.stats)
        state, cfg = _state(), _config()
        channel = self.bot.get_channel(state["channel_id"] or 0)
        embed = discord.Embed(title="⛏️ Minecraft logs", color=0x2ECC71)
        embed.add_field(name="Channel", value=channel.mention if channel else "not set (`/mclog channel`)")
        embed.add_field(name="Stored", value=f"{st['events']:,} events\n{st['counted']:,} counted (mob spawns ...)")
        embed.add_field(name="Size", value=f"{st['size_mb']} / {cfg.get('max_db_mb', 100)} MB")
        embed.add_field(name="From - to", value=f"{_when(st['first'])}\n{_when(st['last'])}")
        embed.add_field(name="Keeps", value=f"{cfg.get('retention_days', 4)} days")
        key = get_setting(KEY_SETTING)
        model = await self.pick_model() if key else None
        embed.add_field(name="AI answers", value=f"on - `{model}`" if key and model else
                        "key set, no free model reachable" if key else "off (bot answers itself, free)")
        embed.add_field(name=f"Players ({len(st['players'])})", value=", ".join(st["players"])[:1024] or "-",
                        inline=False)
        embed.add_field(name="Event types", value=", ".join(f"{t} {c:,}" for t, c in st["types"][:14])[:1024] or "-",
                        inline=False)
        guild = interaction.guild
        embed.add_field(name="Trusted (never reported)", value=", ".join(_trusted()) or "-", inline=False)
        embed.add_field(name="Suspicious-activity DMs go to",
                        value=", ".join(m.mention for m in self.recipients(guild)) or "nobody", inline=False)
        embed.set_footer(text=f"{st['alerts']} suspicious event(s) stored")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="player", description="What a player did (summary + full timeline file).")
    @app_commands.describe(name="Minecraft name", since="From, e.g. 14:00, gestern 14:00, 3h (default: 24h ago)",
                           until="To, e.g. 16:00 (default: now)")
    @app_commands.rename(since="from", until="to")
    @app_commands.autocomplete(name=_player_autocomplete)
    async def player(self, interaction: discord.Interaction, name: str, since: str | None = None,
                     until: str | None = None):
        if not await self._guard(interaction):
            return
        rng = self._range(since, until)
        if isinstance(rng, str):
            await interaction.response.send_message(rng, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.flush()
        tz = _tz()
        s = await self.db(player_summary, self.store, name, *rng, tz)
        events = await self.db(self.store.events, 20000, False, players=[name], start=rng[0], end=rng[1])
        embed = discord.Embed(title=f"⛏️ {name}", description=summary_text(s, tz)[:4000], color=0x2ECC71)
        embed.set_footer(text=f"{datetime.fromtimestamp(rng[0], tz):%d.%m. %H:%M} - "
                              f"{datetime.fromtimestamp(rng[1], tz):%d.%m. %H:%M}")
        file = self._timeline_file(events, f"{name}-timeline.txt")
        await interaction.followup.send(embed=embed, ephemeral=True, **({"file": file} if file else {}))

    @group.command(name="search", description="Find events by player, action, material, area and time.")
    @app_commands.describe(player="Minecraft name", action="Event type, e.g. BLOCK_BREAK",
                           material="Part of a block/item/mob name, e.g. DIAMOND or TNT",
                           since="From (default: 24h ago)", until="To (default: now)",
                           x="Area center X", z="Area center Z", radius="Area radius (default 30)")
    @app_commands.rename(since="from", until="to")
    @app_commands.autocomplete(player=_player_autocomplete, action=_action_autocomplete)
    async def search(self, interaction: discord.Interaction, player: str | None = None, action: str | None = None,
                     material: str | None = None, since: str | None = None, until: str | None = None,
                     x: int | None = None, z: int | None = None, radius: app_commands.Range[int, 1, 2000] = 30):
        if not await self._guard(interaction):
            return
        rng = self._range(since, until)
        if isinstance(rng, str):
            await interaction.response.send_message(rng, ephemeral=True)
            return
        if (x is None) != (z is None):
            await interaction.response.send_message("Give both `x` and `z` for an area.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.flush()
        flt = dict(start=rng[0], end=rng[1], players=[player] if player else None,
                   types=[action.upper()] if action else None, obj_words=[material] if material else None,
                   near=(x, z, radius) if x is not None else None)
        total = await self.db(self.store.count, **flt)
        newest = await self.db(self.store.events, 20, True, **flt)
        who = await self.db(self.store.grouped, "player", 8, **flt)
        tz = _tz()
        lines = [f"**{total:,} event(s)** · {datetime.fromtimestamp(rng[0], tz):%d.%m. %H:%M} - "
                 f"{datetime.fromtimestamp(rng[1], tz):%d.%m. %H:%M}"]
        if who and not player:
            lines.append("By player: " + ", ".join(f"{p} {c}" for p, c in who if p != "-"))
        lines += [f"`{fmt_event(e, tz)[:180]}`" for e in newest]
        embed = discord.Embed(title="⛏️ Search", description="\n".join(lines)[:4000], color=0x2ECC71)
        events = await self.db(self.store.events, 20000, False, **flt) if total else []
        file = self._timeline_file(events, "search.txt")
        await interaction.followup.send(embed=embed, ephemeral=True, **({"file": file} if file else {}))

    @group.command(name="ask", description="Ask a question about the Minecraft logs, e.g. what did X do yesterday?")
    @app_commands.describe(question="e.g. Was hat ColinTK gestern zwischen 14 und 16 Uhr abgebaut?")
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 500]):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.flush()
        tz = _tz()
        q = parse_question(question, datetime.now(tz), self.players, float(_config().get("retention_days", 4)))
        key = get_setting(KEY_SETTING)
        note = None
        answer = None
        if key:
            context = await self.build_ai_context(q, question)
            answer, problem = await self.ai_answer(key, question, context)
            if answer is None:
                note = f"AI not available ({problem}) - answered by the bot itself."
        understood = understood_text(q, tz)
        if answer is not None:
            embed = discord.Embed(title="⛏️ " + question[:240], description=answer[:4000], color=0x9B59B6)
            embed.set_footer(text=f"{understood}\nFree AI model: {await self.pick_model()} · can still be wrong"[:2048])
            events = []
        else:
            text, events = await self.local_answer(q)
            embed = discord.Embed(title="⛏️ " + question[:240], description=text[:4000], color=0x2ECC71)
            embed.set_footer(text=f"{understood}\n{note or ('Vom Bot selbst ausgewertet (kostenlos)' if q.lang == 'de' else 'Answered by the bot itself (free)')}"[:2048])
        file = self._timeline_file(events, "events.txt")
        await interaction.followup.send(embed=embed, ephemeral=True, **({"file": file} if file else {}))

    @group.command(name="import", description="Read older messages of the log channel.")
    @app_commands.describe(hours="How many hours back (max. what's kept)")
    async def import_(self, interaction: discord.Interaction, hours: app_commands.Range[int, 1, 240]):
        if not await self._guard(interaction):
            return
        channel = self.bot.get_channel(self._channel_id() or 0)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Set the log channel first: `/mclog channel`.", ephemeral=True)
            return
        if self.importing:
            await interaction.response.send_message("An import is already running.", ephemeral=True)
            return
        hours = min(hours, int(float(_config().get("retention_days", 4)) * 24))
        await interaction.response.send_message(f"⏳ Importing the last {hours}h - this can take a few minutes. "
                                                f"I'll tell you when it's done.", ephemeral=True)
        self.importing = True
        try:
            state = _state()
            before = discord.Object(state["oldest_msg_id"]) if state["oldest_msg_id"] else None
            after = discord.utils.utcnow() - timedelta(hours=hours)
            count = lines = 0
            oldest = None
            async for message in channel.history(limit=None, after=after, before=before, oldest_first=False):
                if message.webhook_id or message.author.bot:
                    lines += await self.ingest(message, live=False)
                oldest = message.id
                count += 1
                if count % 1000 == 0:
                    await self.flush()
            await self.flush()
            if oldest:
                state = _state()
                state["oldest_msg_id"] = min(oldest, state["oldest_msg_id"] or oldest)
                save_json(STATE_FILE, state)
            self.players = await self.db(self.store.known_players)
            await interaction.followup.send(f"✅ Import done: {count:,} messages, {lines:,} events.", ephemeral=True)
            log.info("Minecraft log import (%dh) by %s: %d messages, %d events", hours, interaction.user, count, lines)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Import stopped: {exc}", ephemeral=True)
        finally:
            self.importing = False

    @group.command(name="report", description="What came in and every line the bot didn't understand (.md file).")
    @app_commands.describe(period="Read the whole log chat of this time again (takes a while, file comes by DM)",
                           days="...or any number of days to read again (instead of period)")
    @app_commands.choices(period=[app_commands.Choice(name="Last 24 hours", value=1),
                                  app_commands.Choice(name="Last week", value=7),
                                  app_commands.Choice(name="Last month", value=30)])
    async def report(self, interaction: discord.Interaction, period: app_commands.Choice[int] | None = None,
                     days: app_commands.Range[int, 1, 365] | None = None):
        if not await self._guard(interaction):
            return
        if period is not None or days is not None:
            await self.scan_report(interaction, days or period.value)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = int(time.time())
        oldest = now - int(float(_config().get("retention_days", 4)) * 86400)
        start = max(int(_state()["reported_until"] or 0) + 1, now - 86400, oldest)
        data, filename = await self.build_report(start, now)
        file = discord.File(io.BytesIO(data), filename=filename)
        await interaction.followup.send(
            "⛏️ Report since the last one (from the stored data). To read the whole chat again, pick a `period` "
            "or `days`.", file=file, ephemeral=True)

    async def scan_report(self, interaction: discord.Interaction, days: int) -> None:
        """Read the whole log channel of the last `days` days again and send the report by DM.
        Nothing is stored - it only collects statistics and not-understood lines."""
        channel = self.bot.get_channel(self._channel_id() or 0)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Set the log channel first: `/mclog channel`.", ephemeral=True)
            return
        if self.scanning:
            await interaction.response.send_message(
                f"A chat scan is already running ({self.scanning - 1:,} messages read so far) - wait for its file.",
                ephemeral=True)
            return
        await interaction.response.send_message(
            f"⏳ Reading the whole log chat of the last **{days} day(s)** again - this can take a while "
            f"(about a minute per {SCAN_SPEED:,} messages). I'll keep this message updated and send you the "
            "`.md` file **by DM** when it's done.", ephemeral=True)
        self.scanning = 1
        started = time.monotonic()
        last_update = started
        diag, players = Diag(), set(self.players)
        after = discord.utils.utcnow() - timedelta(days=days)
        count = retries = 0
        problem = None
        tz = _tz()
        try:
            while True:
                try:
                    async for message in channel.history(limit=None, after=after, oldest_first=True):
                        count += 1
                        self.scanning = count + 1
                        after = discord.Object(id=message.id)
                        if not (message.webhook_id or message.author.bot) or \
                                message.author.id == getattr(self.bot.user, "id", 0):
                            continue
                        extra = await self.attachment_texts(message, diag) if message.attachments else None
                        self.analyse(message, diag, players, extra)
                        if count % 100 == 0:
                            await asyncio.sleep(0)  # let the rest of the bot work in between
                        if time.monotonic() - last_update > 20 and time.monotonic() - started < INTERACTION_MINUTES * 60:
                            last_update = time.monotonic()
                            try:
                                await interaction.edit_original_response(
                                    content=f"⏳ Reading the log chat of the last **{days} day(s)** - "
                                            f"**{count:,} messages** so far (at "
                                            f"{message.created_at.astimezone(tz):%d.%m. %H:%M}).")
                            except discord.HTTPException:
                                pass
                    break
                except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    retries += 1
                    if retries > 5:
                        problem = f"Discord kept failing ({exc}) - the report only covers what was read."
                        break
                    log.warning("Minecraft chat scan: %s - retrying in %ds", exc, 5 * retries)
                    await asyncio.sleep(5 * retries)
        finally:
            self.scanning = 0
        took = int(time.monotonic() - started)
        now = int(time.time())
        source = (f"whole log chat read again - {count:,} messages in {took // 60} min {took % 60} s, nothing stored"
                  + (f". ⚠️ {problem}" if problem else ""))
        text = await asyncio.to_thread(report_md, diag.stats(), diag.unknowns(), now - days * 86400, now, tz, source)
        data = text.encode("utf-8")[:MAX_REPORT_BYTES].decode("utf-8", "ignore").encode("utf-8")
        filename = f"mc-log-scan-{days}d-{datetime.fromtimestamp(now, tz):%Y-%m-%d-%H%M}.md"
        unknown = len(diag.unknown)
        summary = (f"✅ **Chat scan done** ({days} day(s)): {count:,} messages, "
                   f"{diag.stats()['lines']:,} log lines, **{unknown:,} not (fully) understood thing(s)**."
                   + (f"\n⚠️ {problem}" if problem else ""))
        delivered = False
        try:
            await interaction.user.send(summary, file=discord.File(io.BytesIO(data), filename=filename))
            delivered = True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Couldn't DM the chat scan report to %s", interaction.user)
        if time.monotonic() - started < INTERACTION_MINUTES * 60:
            try:
                await interaction.followup.send(
                    summary + ("\nThe file is also in your DMs." if delivered else ""),
                    file=discord.File(io.BytesIO(data), filename=filename), ephemeral=True)
            except discord.HTTPException:
                pass
        log.info("Minecraft chat scan (%d days) by %s: %d messages, %d unknown, %ds", days, interaction.user,
                 count, unknown, took)

    # -------- /mclog cloudflare ---------------------------------------------

    @cf_group.command(name="guide", description="Step by step: where to get the Cloudflare API token.")
    async def cf_guide(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        await interaction.response.send_message(embeds=cf_guide_embeds(), ephemeral=True)

    @cf_group.command(name="setup", description="Enter the Cloudflare API token - the bot builds the log mailbox.")
    async def cf_setup(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        await interaction.response.send_modal(CloudflareModal(self))

    async def run_cf_setup(self, interaction: discord.Interaction, token: str, account_id: str | None) -> None:
        await interaction.response.send_message("☁️ Setting up the Cloudflare mailbox ...", ephemeral=True)
        done: list[str] = []

        async def progress(step):
            done.append(step)
            lines = [f"✅ {STEP_TEXT[s]}" for s in done[:-1]] + [f"⏳ {STEP_TEXT[step]} ..."]
            try:
                await interaction.edit_original_response(content="☁️ **Cloudflare setup**\n" + "\n".join(lines))
            except discord.HTTPException:
                pass

        old = _cf_settings()
        same_place = old.get("account_id") and old.get("account_id") == ((account_id or "").strip() or old["account_id"])
        keys = {k: old[k] for k in ("ingest_key", "bot_key") if old.get(k)} if same_place else None
        try:
            info = await setup_mailbox(token, account_id, progress, keys=keys)
        except CloudflareError as exc:
            lines = [f"✅ {STEP_TEXT[s]}" for s in done[:-1]]
            lines.append(f"❌ **{STEP_TEXT.get(exc.step, exc.step)}:** {exc.message}")
            if exc.hint:
                lines.append(f"\n💡 {exc.hint}")
            lines.append("\nNothing was saved. Step-by-step help: `/mclog cloudflare guide`")
            await interaction.edit_original_response(content="☁️ **Cloudflare setup failed**\n" + "\n".join(lines))
            log.warning("Cloudflare setup by %s failed at %s: %s", interaction.user, exc.step, exc.message)
            return
        settings = dict(info, token=clean_token(token), enabled=True)
        settings.pop("healthy", None)
        set_setting(CF_SETTING, settings)
        if not keys or info["db_id"] != old.get("db_id"):
            state = _state()
            state["cf_after"] = 0   # (ids start again in a new database)
            save_json(STATE_FILE, state)
        self.cf_data_at = 0.0
        self.cf_error = None
        log.info("Cloudflare mailbox set up by %s: %s", interaction.user, info["url"])
        embed = discord.Embed(title="✅ Cloudflare mailbox ready", color=0x2ECC71, description=(
            "The plugin can now send its logs here - the bot picks them up every few seconds.\n"
            + ("" if info["healthy"] else "⏳ The new address isn't reachable yet - that can take a few minutes, "
                                          "the bot keeps trying.\n")
            + "\n**Next:** `/mclog cloudflare plugin` gives you the Java file and these lines for the plugin's "
              "`config.yml`:"))
        embed.add_field(name="config.yml", value=f"```yaml\n{cf_config_text(settings)}```", inline=False)
        embed.add_field(name="Account", value=info["account_name"], inline=True)
        embed.add_field(name="Keys", value="kept" if keys else "new", inline=True)
        embed.set_footer(text="Keep the key secret - whoever has it can send fake logs. Running setup again keeps "
                              "the key; /mclog cloudflare new-key makes a new one.")
        await interaction.edit_original_response(content=None, embed=embed)

    @cf_group.command(name="plugin", description="Java file + config.yml lines for the Minecraft plugin.")
    async def cf_plugin(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        cfg = _cf_settings()
        if not cfg.get("url"):
            await interaction.response.send_message("Set up Cloudflare first: `/mclog cloudflare setup` "
                                                    "(help: `/mclog cloudflare guide`).", ephemeral=True)
            return
        text = (
            "**Plugin side** - 3 steps:\n"
            "1. Put `CloudflareLogSender.java` into your plugin (change the `package` line to your package).\n"
            "2. Add the lines from `config.yml` to the plugin's config.\n"
            "3. In `onEnable()` create the sender, call `cloudflare.log(line)` everywhere the plugin builds a log "
            "line for the Discord webhook, and `cloudflare.shutdown()` in `onDisable()` - the exact code is at the "
            "top of the Java file.\n"
            "Format of a line: the same as now, e.g. `BLOCK_BREAK | Steve | STONE @ Location{...}`. The sender adds "
            "the exact time and sends everything every 2 seconds in one batch.\n"
            "The Discord webhook can stay: if Cloudflare is down for 2 minutes the bot reads the channel again "
            "(lines are not counted twice).")
        files = [discord.File(io.BytesIO(JAVA_FILE.read_bytes()), filename="CloudflareLogSender.java"),
                 discord.File(io.BytesIO(cf_config_text(cfg).encode()), filename="config.yml")]
        await interaction.response.send_message(text, files=files, ephemeral=True)

    @cf_group.command(name="status", description="Is the Cloudflare mailbox working?")
    async def cf_status(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        cfg = _cf_settings()
        if not cfg.get("url"):
            await interaction.response.send_message("Cloudflare isn't set up - `/mclog cloudflare guide` shows how.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.cf_session is None or self.cf_session.closed:
            self.cf_session = aiohttp.ClientSession()
        try:
            h = await health(self.cf_session, cfg["url"])
        except Exception as exc:
            h = {"ok": False, "error": type(exc).__name__}
        now = time.time()
        if not cfg.get("enabled"):
            mode = "⏸️ off (`/mclog cloudflare on`) - the bot reads the Discord channel"
        elif self.cloudflare_active():
            mode = "✅ receiving - the Discord channel is only the stand-in"
        elif self.cf_error:
            mode = f"⚠️ mailbox not reachable ({self.cf_error}) - the bot reads the Discord channel"
        else:
            mode = "🟡 waiting for the plugin - nothing came in the last 2 minutes (Discord channel is read)"
        embed = discord.Embed(title="☁️ Cloudflare mailbox", color=0xF38020, description=mode)
        embed.add_field(name="Address", value=cfg["url"], inline=False)
        embed.add_field(name="Mailbox", value=(f"online, {h.get('pending', 0)} batch(es) waiting" if h.get("ok")
                                               else f"not reachable {h.get('error') or h.get('status') or ''}"))
        embed.add_field(name="Last lines", value=(f"<t:{int(self.cf_data_at)}:R>" if self.cf_data_at else
                                                  "none since the bot started"))
        embed.add_field(name="Last check", value=f"<t:{int(self.cf_ok_at)}:R>" if self.cf_ok_at else "-")
        if self.store is not None:
            st = await self.db(self.store.ingest_stats, int(now - 86400), int(now))
            embed.add_field(name="Last 24h", value=f"{st['cf']:,} Cloudflare batches · "
                                                   f"{st['messages'] - st['cf']:,} Discord messages")
        embed.set_footer(text=f"Account: {cfg.get('account_name', '?')} · checks every {CF_POLL_SECONDS}s "
                              f"(every {CF_IDLE_POLL}s when nothing comes)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @cf_group.command(name="new-key", description="Make a new plugin key (the old one stops working).")
    async def cf_new_key(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        cfg = _cf_settings()
        if not cfg.get("token"):
            await interaction.response.send_message("Set up Cloudflare first: `/mclog cloudflare setup`.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            info = await setup_mailbox(cfg["token"], cfg.get("account_id"), keys={"bot_key": cfg.get("bot_key")})
        except CloudflareError as exc:
            await interaction.followup.send(f"❌ {STEP_TEXT.get(exc.step, exc.step)}: {exc.message}"
                                            + (f"\n💡 {exc.hint}" if exc.hint else ""), ephemeral=True)
            return
        cfg.update(ingest_key=info["ingest_key"], url=info["url"])
        set_setting(CF_SETTING, cfg)
        await interaction.followup.send("✅ New key - put it into the plugin's config.yml:\n"
                                        f"```yaml\n{cf_config_text(cfg)}```", ephemeral=True)

    @cf_group.command(name="off", description="Stop picking up logs from Cloudflare (read the Discord channel).")
    async def cf_off(self, interaction: discord.Interaction):
        await self._cf_switch(interaction, False)

    @cf_group.command(name="on", description="Pick up the logs from Cloudflare again.")
    async def cf_on(self, interaction: discord.Interaction):
        await self._cf_switch(interaction, True)

    async def _cf_switch(self, interaction: discord.Interaction, on: bool) -> None:
        if not await self._guard(interaction, need_store=False):
            return
        cfg = _cf_settings()
        if not cfg.get("url"):
            await interaction.response.send_message("Set up Cloudflare first: `/mclog cloudflare setup`.",
                                                    ephemeral=True)
            return
        cfg["enabled"] = on
        set_setting(CF_SETTING, cfg)
        self.cf_data_at = 0.0
        await interaction.response.send_message(
            "✅ Cloudflare on - the bot picks up the logs from the mailbox." if on else
            "⏸️ Cloudflare off - the bot reads the Discord channel. The mailbox keeps what the plugin sends "
            "(7 days) until you turn it on again.", ephemeral=True)

    @trusted_group.command(name="add", description="Never report this Minecraft player.")
    async def trusted_add(self, interaction: discord.Interaction, name: str):
        if not await self._guard(interaction, need_store=False):
            return
        trusted = _trusted()
        if name.lower() not in {t.lower() for t in trusted}:
            trusted.append(name)
        self._save_trusted(trusted)
        await interaction.response.send_message(f"✅ `{name}` is trusted. Trusted: {', '.join(trusted)}", ephemeral=True)

    @trusted_group.command(name="remove", description="Report this Minecraft player again.")
    async def trusted_remove(self, interaction: discord.Interaction, name: str):
        if not await self._guard(interaction, need_store=False):
            return
        trusted = [t for t in _trusted() if t.lower() != name.lower()]
        self._save_trusted(trusted)
        await interaction.response.send_message(f"`{name}` is no longer trusted. Trusted: {', '.join(trusted) or '-'}",
                                                ephemeral=True)

    @trusted_group.command(name="list", description="Show the trusted players.")
    async def trusted_list(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        await interaction.response.send_message("Trusted (never reported, teleports close to them are): "
                                                + (", ".join(_trusted()) or "-"), ephemeral=True)

    def _save_trusted(self, trusted: list[str]) -> None:
        state = _state()
        state["trusted"] = trusted
        save_json(STATE_FILE, state)
        self.detector.set_trusted(trusted)

    def _notify_ids(self, guild: discord.Guild) -> list[int]:
        ids = _state()["notify"]
        return list(ids) if ids is not None else [m.id for m in self.recipients(guild)]

    @notify_group.command(name="add", description="Also send suspicious-activity DMs to this member.")
    async def notify_add(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction, need_store=False):
            return
        ids = self._notify_ids(interaction.guild)
        if member.id not in ids:
            ids.append(member.id)
        state = _state()
        state["notify"] = ids
        save_json(STATE_FILE, state)
        await interaction.response.send_message(
            "✅ DMs go to: " + ", ".join(f"<@{i}>" for i in ids), ephemeral=True)

    @notify_group.command(name="remove", description="Stop suspicious-activity DMs for this member.")
    async def notify_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction, need_store=False):
            return
        ids = [i for i in self._notify_ids(interaction.guild) if i != member.id]
        state = _state()
        state["notify"] = ids
        save_json(STATE_FILE, state)
        await interaction.response.send_message(
            "DMs go to: " + (", ".join(f"<@{i}>" for i in ids) or "nobody"), ephemeral=True)

    @notify_group.command(name="list", description="Who gets the suspicious-activity DMs.")
    async def notify_list(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        await interaction.response.send_message(
            "DMs go to: " + (", ".join(m.mention for m in self.recipients(interaction.guild)) or "nobody"),
            ephemeral=True)

    @group.command(name="ai-key", description="Set or remove the OpenRouter key for free AI answers (optional).")
    async def ai_key(self, interaction: discord.Interaction):
        if not await self._guard(interaction, need_store=False):
            return
        await interaction.response.send_modal(KeyModal(self))

    async def _model_autocomplete(self, interaction: discord.Interaction, current: str):
        models = await self.free_models()
        return [app_commands.Choice(name=m["id"][:100], value=m["id"][:100])
                for m in models if current.lower() in m["id"].lower()][:25]

    @group.command(name="ai-model", description="Pick the free AI model (only free models can be picked).")
    @app_commands.describe(model="A model ending in :free (empty = pick automatically)")
    @app_commands.autocomplete(model=_model_autocomplete)
    async def ai_model(self, interaction: discord.Interaction, model: str | None = None):
        if not await self._guard(interaction, need_store=False):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = _state()
        if model:
            free = {m["id"] for m in await self.free_models()}
            if model not in free:
                await interaction.followup.send(
                    f"`{model}` isn't a free model on OpenRouter right now - only free models are allowed.",
                    ephemeral=True)
                return
        state["ai_model"] = model or None
        save_json(STATE_FILE, state)
        chosen = await self.pick_model()
        await interaction.followup.send(f"AI model: `{chosen or 'none reachable'}`"
                                        + (" (picked automatically)" if not model else ""), ephemeral=True)


async def setup(bot):
    await bot.add_cog(McLogs(bot))
