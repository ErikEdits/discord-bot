"""Minecraft log store and analysis for cogs/mc_logs.py (no Discord code in here).

Parsing
    The EntityLagFix plugin posts embeds with one event per line, e.g.
        BLOCK_PLACE | ColinTK | GRASS_BLOCK @ Location{world=CraftWorld{name=world},x=-168.0,y=69.0,z=-157.0,...}
        ENTITY_SPAWN | ZOMBIE | world=world | Location{...}
        PLAYER_COMMAND | Steve | /gamemode spectator
    parse_line() turns a line into an Event. Unknown event types still work: the
    first field is the type, a following player name is recognised, Location{...}
    blocks give world and coordinates (the last one is the destination of a teleport).

Storage (SQLite, data/mc_logs.db) is built to stay small on free hosting:
    - repeated strings (event types, player names, materials, worlds) are stored once
      in "names" and referenced by number; coordinates are whole numbers
    - noisy events without a player (mob spawns, chunk loads, ...) are only counted
      per 10 minutes, type and 64x64 area ("counts" table)
    - movement-like events are sampled (one per player and N seconds)
    - everything older than retention_days is deleted, and a hard size limit
      (max_db_mb) deletes the oldest data first; freed space is returned to the disk
Questions
    parse_question() reads players, time ranges ("gestern zwischen 14 und 16 Uhr",
    "last 3 hours"), actions ("abgebaut", "commands") and materials from a free text
    question; the store answers it with summaries and timelines.
Suspicious activity
    Detector.feed() checks every new event: game mode changes, vanish, suspicious
    commands, mass mining / many ores, and teleports close to trusted players.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger("setup-bot.mc_logs")

LOC_RE = re.compile(
    r"Location\{world=(?:CraftWorld\{(?:name|key)=)?([^,}]*)\}?,\s*x=(-?[\d.]+(?:E-?\d+)?),\s*y=(-?[\d.]+(?:E-?\d+)?),"
    r"\s*z=(-?[\d.]+(?:E-?\d+)?)[^}]*\}"
)
XYZ_RE = re.compile(r"\bx=(-?[\d.]+)[,\s]+y=(-?[\d.]+)[,\s]+z=(-?[\d.]+)")
# "Name: text" (COMMAND, CHAT, CONSOLE) or "Name -> VALUE" (GAMEMODE) in the first field
NAME_PREFIX_RE = re.compile(r"^([A-Za-z0-9_@]{1,16})\s*(?::|->)\s*(.+)$")
TITLE_RE = re.compile(r"^[\w .-]{2,40} server logs?$", re.I)
WORLD_KEYS = {"minecraft:overworld": "world", "minecraft:the_nether": "world_nether",
              "minecraft:the_end": "world_the_end"}
# Short event names of the UptimeManager log -> the names the rest of the bot uses
TYPE_ALIASES = {"JOIN": "PLAYER_JOIN", "QUIT": "PLAYER_QUIT", "KICK": "PLAYER_KICK", "COMMAND": "PLAYER_COMMAND",
                "TELEPORT": "PLAYER_TELEPORT", "GAMEMODE": "PLAYER_GAME_MODE_CHANGE", "CHAT": "PLAYER_CHAT",
                "RESPAWN": "PLAYER_RESPAWN", "DROP": "PLAYER_DROP_ITEM", "DEATH": "PLAYER_DEATH",
                "PICKUP": "PLAYER_PICKUP_ITEM", "LOGIN": "PLAYER_LOGIN"}
TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,48}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")
KV_RE = re.compile(r"^[a-z_]+=", re.I)
ENTITY_LIKE_RE = re.compile(r"^[A-Z0-9_]+$")
JOIN_TYPES = ("PLAYER_JOIN", "PLAYER_QUIT", "PLAYER_LOGIN", "PLAYER_KICK")
# Events without a player are only counted, except these (rare and useful one by one).
KEEP_WITHOUT_PLAYER = ("SERVER_", "PLUGIN_", "PLAYER_", "WORLD_LOAD", "WORLD_UNLOAD", "CONSOLE")
MODES = ("SPECTATOR", "CREATIVE", "SURVIVAL", "ADVENTURE")
NO_PLAYER_PREFIXES = ("ENTITY_", "SERVER_", "PLUGIN_", "CHUNK_", "WEATHER_", "WORLD_LOAD", "WORLD_UNLOAD",
                      "WORLD_SAVE", "WORLD_INIT", "ITEM_SPAWN", "ITEM_DESPAWN", "CREATURE_", "SPAWNER_",
                      "LIGHTNING_", "STRUCTURE_", "PORTAL_CREATE", "BLOCK_FORM", "BLOCK_SPREAD", "BLOCK_GROW",
                      "BLOCK_FADE", "BLOCK_PHYSICS", "BLOCK_FROM_TO", "LEAVES_DECAY", "BLOCK_BURN", "BLOCK_IGNITE",
                      "REDSTONE_", "TIME_SKIP", "SPONGE_ABSORB", "DAMAGE", "CONSOLE")
TEXT_TYPES_SKIP = ("BLOCK_", "ENTITY_")
VANISH_COMMANDS = {"vanish", "v", "sv", "pv", "supervanish", "premiumvanish", "essentials:vanish", "ev", "evanish"}
GAMEMODE_COMMANDS = {"gamemode", "gm", "gmc", "gms", "gmsp", "gma", "gmt", "egamemode", "spectator", "creative"}
MODE_ALIASES = {"3": "SPECTATOR", "sp": "SPECTATOR", "spectator": "SPECTATOR", "1": "CREATIVE", "c": "CREATIVE",
                "creative": "CREATIVE", "0": "SURVIVAL", "s": "SURVIVAL", "survival": "SURVIVAL", "2": "ADVENTURE",
                "a": "ADVENTURE", "adventure": "ADVENTURE"}
GM_SHORTCUT = {"gmsp": "SPECTATOR", "gmc": "CREATIVE", "gms": "SURVIVAL", "gma": "ADVENTURE",
               "spectator": "SPECTATOR", "creative": "CREATIVE"}


@dataclass
class Event:
    ts: int
    type: str
    player: str | None = None
    obj: str | None = None
    world: str | None = None
    x: int | None = None
    y: int | None = None
    z: int | None = None
    x2: int | None = None
    y2: int | None = None
    z2: int | None = None
    txt: str | None = None
    raw: str = ""

    @property
    def dest(self):
        """Destination of a teleport, otherwise the location."""
        if self.x2 is not None:
            return self.x2, self.y2, self.z2
        return self.x, self.y, self.z


def _num(value: str) -> int:
    return int(math.floor(float(value)))


def message_lines(texts) -> list[str]:
    """All event lines of a Discord message (content, embed descriptions and fields)."""
    lines = []
    for text in texts:
        for line in (text or "").splitlines():
            line = line.strip().lstrip("•·-*>").strip().strip("`").strip()
            if "|" in line:
                lines.append(line)
    return lines


def split_lines(texts) -> tuple[list[str], list[str]]:
    """(event lines with "|", other non-empty lines) of a Discord message."""
    events, other = [], []
    for text in texts:
        for line in (text or "").splitlines():
            line = line.strip().lstrip("•·->").strip().strip("`*").strip()
            if "|" in line:
                events.append(line)
            elif line and not TITLE_RE.match(line):
                other.append(line)
    return events, other


def shape(text: str, limit: int = 160) -> str:
    """A line with coordinates and numbers blanked out, so the same kind of line is collected once."""
    text = re.sub(r"Location\{.*?\}(?:,[^|]*?\})?", "Location{…}", text)
    return re.sub(r"-?\d+(?:[.,]\d+)*", "#", text)[:limit]


def is_player_type(etype: str) -> bool:
    return not etype.startswith(NO_PLAYER_PREFIXES)


KNOWN_TYPES = {"INVENTORY_CLOSE", "ENDER_PEARL_LANDED", "ENDER_PEARL_REMOVE_ALL", "PLAYER_PICKUP_ITEM"}


def known_type(etype: str) -> bool:
    """Event types the bot knows what they mean (answers, alerts); others are only stored."""
    return (etype in TYPE_WORDS["en"] or etype in JOIN_TYPES or etype in KNOWN_TYPES or "GAME_MODE" in etype
            or "GAMEMODE" in etype or not is_player_type(etype))


def world_name(name: str | None) -> str | None:
    if not name:
        return None
    return WORLD_KEYS.get(name, name.split(":", 1)[1] if name.startswith("minecraft:") else name)


def parse_line(line: str, ts: int, known_players: set[str] | None = None) -> Event | None:
    parts = [p.strip() for p in line.split("|")]
    etype = parts[0].upper().replace(" ", "_")
    if not TYPE_RE.match(etype):
        return None
    etype = TYPE_ALIASES.get(etype, etype)
    ev = Event(ts=ts, type=etype, raw=line[:500])
    locs = LOC_RE.findall(line)
    if locs:
        ev.world = world_name(locs[0][0])
        ev.x, ev.y, ev.z = (_num(v) for v in locs[0][1:])
        if len(locs) > 1:
            w2 = world_name(locs[-1][0])
            ev.x2, ev.y2, ev.z2 = (_num(v) for v in locs[-1][1:])
            if w2 and w2 != ev.world:
                ev.world = w2  # teleport into another world: the destination world counts
    else:
        xyz = XYZ_RE.search(line)
        if xyz:
            ev.x, ev.y, ev.z = (_num(v) for v in xyz.groups())
    rest = parts[1:]
    if rest:
        m = NAME_PREFIX_RE.match(rest[0])
        if m and "Location{" not in rest[0]:
            rest = [m.group(1), m.group(2)] + rest[1:]
    if not ev.world:
        for p in rest:
            m = re.match(r"^world=([^\s|]+)", p)
            if m:
                ev.world = world_name(m.group(1))
                break
    known = known_players or set()
    first = rest[0].split(" @ ")[0].strip() if rest else ""
    player_type = not etype.startswith(NO_PLAYER_PREFIXES)
    # Entity types (ZOMBIE, ITEM, END_CRYSTAL) look like names - all-caps names only count as
    # players when they were seen joining the server.
    entity_like = bool(ENTITY_LIKE_RE.match(first))
    if first and NAME_RE.match(first) and not KV_RE.match(first) and (
            first in known or etype in JOIN_TYPES or (player_type and not entity_like)):
        ev.player = first
        rest = rest[1:]
    values, kvs = [], []
    for p in rest:
        clean = XYZ_RE.sub("", LOC_RE.sub("", p)).replace(" @ ", " ").strip(" @->→,;:")
        if clean:
            (kvs if KV_RE.match(clean) else values).append(clean)
    if ev.player is None:
        if first and not KV_RE.match(first) and "Location{" not in first:
            ev.obj = first[:64]
    elif values and not values[0].startswith("/") and (
            etype.startswith(("BLOCK_", "ENTITY_")) or ENTITY_LIKE_RE.match(values[0].split()[0])):
        ev.obj = values[0].split()[0][:64]   # material / block / item / inventory (CHEST, POPPY, ...)
    if "GAME_MODE" in etype or "GAMEMODE" in etype:
        found = [m for m in re.findall(r"[A-Z]+", line.upper()) if m in MODES]
        if found:
            ev.obj = found[-1]
    if etype == "WORLD_CHANGE":
        to = re.search(r"\bto=([\w-]+)", line)
        if to:
            ev.obj = to.group(1)[:64]
        elif values:
            ev.obj = values[-1].split()[-1][:64]
    if not etype.startswith(TEXT_TYPES_SKIP):
        command = next((v for v in values if v.startswith("/")), None)
        text = " | ".join(values + [k for k in kvs if not k.lower().startswith(("world=", "from=", "to="))])
        ev.txt = (command or text)[:200] or None
    return ev


def command_name(txt: str | None) -> str:
    """'/minecraft:gamemode spectator' -> 'gamemode', '//set stone' -> '/set'."""
    if not txt or not txt.startswith("/"):
        return ""
    word = txt[1:].split()[0].lower() if len(txt) > 1 else ""
    return word.split(":", 1)[1] if ":" in word and not word.startswith("/") else word


def command_mode(txt: str | None) -> str | None:
    name = command_name(txt)
    if name in GM_SHORTCUT:
        return GM_SHORTCUT[name]
    if name in ("gamemode", "gm", "egamemode"):
        args = txt.split()[1:]
        return MODE_ALIASES.get(args[0].lower()) if args else None
    return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS ev (
    ts INTEGER NOT NULL, type INTEGER NOT NULL, player INTEGER, obj INTEGER, world INTEGER,
    x INTEGER, y INTEGER, z INTEGER, x2 INTEGER, y2 INTEGER, z2 INTEGER, txt TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON ev(ts);
CREATE INDEX IF NOT EXISTS ev_player ON ev(player, ts) WHERE player IS NOT NULL;
CREATE TABLE IF NOT EXISTS counts (
    bucket INTEGER NOT NULL, type INTEGER NOT NULL, obj INTEGER NOT NULL, world INTEGER NOT NULL,
    rx INTEGER NOT NULL, rz INTEGER NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (bucket, type, obj, world, rx, rz)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (ts INTEGER NOT NULL, player TEXT, kind TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS ingest (
    minute INTEGER PRIMARY KEY, messages INTEGER NOT NULL, lines INTEGER NOT NULL, events INTEGER NOT NULL,
    unknown INTEGER NOT NULL, chars INTEGER NOT NULL, max_lines INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ingest_types (
    hour INTEGER NOT NULL, type TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (hour, type)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS unknown (
    kind TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL, first_ts INTEGER NOT NULL, last_ts INTEGER NOT NULL,
    example TEXT, PRIMARY KEY (kind, key)) WITHOUT ROWID;
"""
COUNT_BUCKET = 600
UNKNOWN_MAX_KEYS = 3000        # distinct unknown line shapes kept (the rest is only counted)


class Diag:
    """What came in through the channel, collected between two flushes (see LogStore.add_diag)."""

    def __init__(self):
        self.minutes: dict[int, list[int]] = {}      # minute -> [messages, lines, events, unknown, chars, max_lines]
        self.types: Counter = Counter()              # (hour, type) -> lines
        self.unknown: dict[tuple[str, str], list] = {}   # (kind, key) -> [n, first_ts, last_ts, example]

    def __bool__(self):
        return bool(self.minutes or self.types or self.unknown)

    def message(self, ts: int, lines: int, chars: int) -> None:
        m = self.minutes.setdefault(ts // 60 * 60, [0, 0, 0, 0, 0, 0])
        m[0] += 1
        m[1] += lines
        m[4] += chars
        m[5] = max(m[5], lines)

    def event(self, ts: int, etype: str) -> None:
        self.minutes.setdefault(ts // 60 * 60, [0, 0, 0, 0, 0, 0])[2] += 1
        self.types[(ts // 3600 * 3600, etype)] += 1

    def unknown_line(self, ts: int, kind: str, line: str, key: str | None = None, count_line: bool = True) -> None:
        if count_line:
            self.minutes.setdefault(ts // 60 * 60, [0, 0, 0, 0, 0, 0])[3] += 1
        k = (kind, key if key is not None else shape(line))
        u = self.unknown.get(k)
        if u is None:
            if len(self.unknown) >= UNKNOWN_MAX_KEYS:
                k = (kind, "(more lines of this kind - not kept one by one)")
                u = self.unknown.setdefault(k, [0, ts, ts, []])
            else:
                self.unknown[k] = [1, ts, ts, [line[:500]]]
                return
        u[0] += 1
        u[1], u[2] = min(u[1], ts), max(u[2], ts)
        if len(u[3]) < 3 and line[:500] not in u[3]:
            u[3].append(line[:500])

    def stats(self) -> dict:
        """Same shape as LogStore.ingest_stats(), from what was collected in memory."""
        rows = sorted((m, *v) for m, v in self.minutes.items())
        types = Counter()
        for (_, etype), n in self.types.items():
            types[etype] += n
        total = {k: sum(r[i] for r in rows) for i, k in enumerate(("messages", "lines", "events", "unknown",
                                                                     "chars"), start=1)}
        return {"rows": rows, "types": types.most_common(), "since": rows[0][0] if rows else None, **total,
                "max_lines": max((r[6] for r in rows), default=0),
                "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None}

    def unknowns(self) -> list[tuple]:
        """Same shape as LogStore.unknowns() (with a list of up to 3 examples)."""
        return sorted(((k, key, n, first, last, ex) for (k, key), (n, first, last, ex) in self.unknown.items()),
                      key=lambda u: (u[0], -u[2]))


class LogStore:
    """All methods block; the cog runs them in a single worker thread."""

    def __init__(self, path, retention_days: float = 4, max_mb: float = 100,
                 count_only: set[str] | None = None, sample_seconds: dict | None = None):
        self.path = str(path)
        self.retention_days = retention_days
        self.max_mb = max_mb
        self.count_only = set(count_only or ())
        self.sample_seconds = dict(sample_seconds or {})
        self._last_sample: dict[tuple, int] = {}
        self._lock = threading.RLock()
        new = not os.path.exists(self.path)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if new:
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")  # only possible before the first table
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA cache_size=-4000")
        self.db.executescript(SCHEMA)
        self._ids: dict[str, int] = {n: i for i, n in self.db.execute("SELECT id, name FROM names")}
        self._names: dict[int, str] = {i: n for n, i in self._ids.items()}
        self._fix_misread_players()

    def _fix_misread_players(self) -> None:
        """Older versions stored mobs (ZOMBIE, ITEM, ...) as players. Turn those rows into
        counts and keep only real players (all-caps names only if they joined the server)."""
        rows = self.db.execute("SELECT DISTINCT player FROM ev WHERE player IS NOT NULL").fetchall()
        joined_types = [self._ids[t] for t in JOIN_TYPES if t in self._ids]
        bad = []
        for (pid,) in rows:
            name = self._names.get(pid, "")
            if not ENTITY_LIKE_RE.match(name):
                continue
            joined = joined_types and self.db.execute(
                f"SELECT 1 FROM ev WHERE player=? AND type IN ({','.join('?' * len(joined_types))}) LIMIT 1",
                [pid] + joined_types).fetchone()
            if not joined:
                bad.append(pid)
        if not bad:
            return
        marks = ",".join("?" * len(bad))
        self.db.execute("BEGIN")
        self.db.execute(
            f"INSERT INTO counts SELECT ts / {COUNT_BUCKET} * {COUNT_BUCKET}, type, player, COALESCE(world, ?), "
            f"COALESCE(x, 0) >> 6, COALESCE(z, 0) >> 6, COUNT(*) FROM ev WHERE player IN ({marks}) "
            f"GROUP BY 1, 2, 3, 4, 5, 6 ON CONFLICT(bucket,type,obj,world,rx,rz) DO UPDATE SET n = n + excluded.n",
            [self._id("?")] + bad)
        moved = self.db.execute(f"DELETE FROM ev WHERE player IN ({marks})", bad).rowcount
        self.db.execute("COMMIT")
        self.db.executescript("PRAGMA incremental_vacuum;")
        log.info("Moved %d mob events (stored as players by mistake) to counts", moved)

    def close(self):
        with self._lock:
            self.db.close()

    # -------- names --------
    def _id(self, name: str | None) -> int | None:
        if name is None:
            return None
        i = self._ids.get(name)
        if i is None:
            i = self.db.execute("INSERT OR IGNORE INTO names(name) VALUES (?)", (name,)).lastrowid
            if not i:
                i = self.db.execute("SELECT id FROM names WHERE name=?", (name,)).fetchone()[0]
            self._ids[name], self._names[i] = i, name
        return i

    def name(self, i: int | None) -> str | None:
        return None if i is None else self._names.get(i)

    def known_players(self) -> set[str]:
        with self._lock:
            rows = self.db.execute("SELECT DISTINCT player FROM ev WHERE player IS NOT NULL").fetchall()
        return {self._names[r[0]] for r in rows if r[0] in self._names}

    def ids_like(self, words: list[str]) -> list[int]:
        """Name ids containing one of the words (case-insensitive), e.g. 'diamond' -> DIAMOND_ORE, ..."""
        out = []
        for name, i in self._ids.items():
            if any(w.lower() in name.lower() for w in words):
                out.append(i)
        return out

    # -------- writing --------
    def add(self, events: list[Event]) -> int:
        """Store a batch of events. Returns how many were stored one by one."""
        rows, counts = [], Counter()
        with self._lock:
            self.db.execute("BEGIN")
            try:
                for ev in events:
                    if ev.player is None and (ev.type in self.count_only
                                              or not ev.type.startswith(KEEP_WITHOUT_PLAYER)):
                        rx = (ev.x if ev.x is not None else 0) // 64
                        rz = (ev.z if ev.z is not None else 0) // 64
                        counts[(ev.ts // COUNT_BUCKET * COUNT_BUCKET, self._id(ev.type), self._id(ev.obj or "?"),
                                self._id(ev.world or "?"), rx, rz)] += 1
                        continue
                    every = self.sample_seconds.get(ev.type)
                    if every and ev.player:
                        key = (ev.player, ev.type)
                        if ev.ts - self._last_sample.get(key, -10**12) < every:
                            continue
                        self._last_sample[key] = ev.ts
                    rows.append((ev.ts, self._id(ev.type), self._id(ev.player), self._id(ev.obj),
                                 self._id(ev.world), ev.x, ev.y, ev.z, ev.x2, ev.y2, ev.z2, ev.txt))
                if rows:
                    self.db.executemany("INSERT INTO ev VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                if counts:
                    self.db.executemany(
                        "INSERT INTO counts VALUES (?,?,?,?,?,?,?) ON CONFLICT(bucket,type,obj,world,rx,rz) "
                        "DO UPDATE SET n = n + excluded.n", [k + (n,) for k, n in counts.items()])
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return len(rows)

    def add_alert(self, ts: int, player: str, kind: str, text: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO alerts VALUES (?,?,?,?)", (ts, player, kind, text[:300]))

    def add_diag(self, diag: Diag) -> None:
        if not diag:
            return
        with self._lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany(
                    "INSERT INTO ingest VALUES (?,?,?,?,?,?,?) ON CONFLICT(minute) DO UPDATE SET "
                    "messages = messages + excluded.messages, lines = lines + excluded.lines, "
                    "events = events + excluded.events, unknown = unknown + excluded.unknown, "
                    "chars = chars + excluded.chars, max_lines = MAX(max_lines, excluded.max_lines)",
                    [(m, *v) for m, v in diag.minutes.items()])
                self.db.executemany(
                    "INSERT INTO ingest_types VALUES (?,?,?) ON CONFLICT(hour, type) DO UPDATE SET n = n + excluded.n",
                    [(h, t, n) for (h, t), n in diag.types.items()])
                stored = self.db.execute("SELECT COUNT(*) FROM unknown").fetchone()[0]
                for (kind, key), (n, first, last, examples) in diag.unknown.items():
                    example = examples[0] if examples else None
                    exists = self.db.execute("SELECT 1 FROM unknown WHERE kind=? AND key=?", (kind, key)).fetchone()
                    if not exists and stored >= UNKNOWN_MAX_KEYS:
                        key, example = "(more lines of this kind - not kept one by one)", None
                    elif not exists:
                        stored += 1
                    self.db.execute(
                        "INSERT INTO unknown VALUES (?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
                        "n = n + excluded.n, first_ts = MIN(first_ts, excluded.first_ts), "
                        "last_ts = MAX(last_ts, excluded.last_ts)", (kind, key, n, first, last, example))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def ingest_stats(self, start: int, end: int) -> dict:
        """Messages/lines that came in between start and end (per minute rows + totals)."""
        with self._lock:
            rows = self.db.execute("SELECT minute, messages, lines, events, unknown, chars, max_lines FROM ingest "
                                   "WHERE minute >= ? AND minute <= ? ORDER BY minute",
                                   (start // 60 * 60, end)).fetchall()
            types = self.db.execute("SELECT type, SUM(n) FROM ingest_types WHERE hour >= ? AND hour <= ? "
                                    "GROUP BY type ORDER BY 2 DESC", (start // 3600 * 3600, end)).fetchall()
            since = self.db.execute("SELECT MIN(minute) FROM ingest").fetchone()[0]
        total = {k: sum(r[i] for r in rows) for i, k in enumerate(("messages", "lines", "events", "unknown",
                                                                     "chars"), start=1)}
        total["max_lines"] = max((r[6] for r in rows), default=0)
        return {"rows": rows, "types": types, "since": since, **total,
                "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None}

    def messages_between(self, start: int, end: int) -> tuple[int, int | None]:
        """(messages, first minute with a message) between start and end."""
        with self._lock:
            n, first = self.db.execute("SELECT COALESCE(SUM(messages), 0), MIN(minute) FROM ingest "
                                       "WHERE minute >= ? AND minute <= ?", (start // 60 * 60, end)).fetchone()
        return n, first

    def unknowns(self, until: int | None = None) -> list[tuple]:
        """[(kind, key, n, first_ts, last_ts, example)], most frequent first."""
        with self._lock:
            sql, args = "SELECT kind, key, n, first_ts, last_ts, example FROM unknown", []
            if until is not None:
                sql, args = sql + " WHERE first_ts <= ?", [until]
            return self.db.execute(sql + " ORDER BY kind, n DESC", args).fetchall()

    def clear_unknowns(self, until: int) -> int:
        """Forget unknown lines that were reported (last seen before `until`)."""
        with self._lock:
            return self.db.execute("DELETE FROM unknown WHERE last_ts <= ?", (until,)).rowcount

    # -------- size control --------
    def size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total / 1_048_576

    def used_mb(self) -> float:
        with self._lock:
            page = self.db.execute("PRAGMA page_size").fetchone()[0]
            count = self.db.execute("PRAGMA page_count").fetchone()[0]
            free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        return (count - free) * page / 1_048_576

    def prune(self, now: float | None = None, max_mb: float | None = None) -> dict:
        """Delete what's older than the retention, then the oldest data until under the size limit."""
        now = now or time.time()
        max_mb = max_mb or self.max_mb
        cutoff = int(now - self.retention_days * 86400)
        deleted = 0
        with self._lock:
            deleted += self.db.execute("DELETE FROM ev WHERE ts < ?", (cutoff,)).rowcount
            self.db.execute("DELETE FROM counts WHERE bucket < ?", (cutoff,))
            self.db.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM ingest WHERE minute < ?", (cutoff,))
            self.db.execute("DELETE FROM ingest_types WHERE hour < ?", (cutoff - 3600,))
            self.db.execute("DELETE FROM unknown WHERE last_ts < ?", (cutoff,))
            trimmed_to = None
            for _ in range(20):
                if self.used_mb() <= max_mb * 0.9:
                    break
                total = self.db.execute("SELECT COUNT(*) FROM ev").fetchone()[0]
                if total < 1000:
                    break
                row = self.db.execute("SELECT ts FROM ev ORDER BY ts LIMIT 1 OFFSET ?", (total // 8,)).fetchone()
                if row is None:
                    break
                trimmed_to = row[0]
                deleted += self.db.execute("DELETE FROM ev WHERE ts < ?", (trimmed_to,)).rowcount
                self.db.execute("DELETE FROM counts WHERE bucket < ?", (trimmed_to,))
            self.db.executescript("PRAGMA incremental_vacuum;")  # (plain execute only runs one step)
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            for key in [k for k, v in self._last_sample.items() if v < cutoff]:
                self._last_sample.pop(key, None)
        return {"deleted": deleted, "trimmed_to": trimmed_to, "size_mb": round(self.size_mb(), 2)}

    def clear(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM ev")
            self.db.execute("DELETE FROM counts")
            self.db.execute("DELETE FROM alerts")
            for table in ("ingest", "ingest_types", "unknown"):
                self.db.execute(f"DELETE FROM {table}")
            self.db.executescript("PRAGMA incremental_vacuum;")  # (plain execute only runs one step)
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    # -------- reading --------
    def stats(self) -> dict:
        with self._lock:
            n, first, last = self.db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM ev").fetchone()
            counted = self.db.execute("SELECT COALESCE(SUM(n), 0) FROM counts").fetchone()[0]
            types = self.db.execute("SELECT type, COUNT(*) FROM ev GROUP BY type ORDER BY 2 DESC").fetchall()
            alerts = self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        return {"events": n, "first": first, "last": last, "counted": counted, "alerts": alerts,
                "types": [(self.name(t), c) for t, c in types], "size_mb": round(self.size_mb(), 2),
                "players": sorted(self.known_players(), key=str.lower)}

    def _where(self, players=None, types=None, start=None, end=None, obj_words=None, near=None,
               type_words=None):
        sql, args = [], []
        if start is not None:
            sql.append("ts >= ?"); args.append(int(start))
        if end is not None:
            sql.append("ts <= ?"); args.append(int(end))
        if players:
            ids = [self._ids[p] for p in self._resolve_players(players) if p in self._ids]
            if not ids:
                return None
            sql.append(f"player IN ({','.join('?' * len(ids))})"); args += ids
        if types or type_words:
            ids = [self._ids[t] for t in (types or []) if t in self._ids]
            ids += self.ids_like(type_words) if type_words else []
            if not ids:
                return None
            sql.append(f"type IN ({','.join('?' * len(ids))})"); args += ids
        if obj_words:
            ids = self.ids_like(obj_words)
            if not ids:
                return None
            sql.append(f"obj IN ({','.join('?' * len(ids))})"); args += ids
        if near:
            x, z, r = near  # (x, z, radius)
            sql.append("((x BETWEEN ? AND ? AND z BETWEEN ? AND ?) OR (x2 BETWEEN ? AND ? AND z2 BETWEEN ? AND ?))")
            args += [x - r, x + r, z - r, z + r] * 2
        return (" WHERE " + " AND ".join(sql)) if sql else "", args

    def _resolve_players(self, players) -> list[str]:
        """Case-insensitive player names -> stored spelling."""
        lower = {n.lower(): n for n in self._ids}
        return [lower.get(p.lower(), p) for p in players]

    def events(self, limit: int = 2000, newest_first: bool = True, **filters) -> list[Event]:
        with self._lock:
            where = self._where(**filters)
            if where is None:
                return []
            clause, args = where
            rows = self.db.execute(
                f"SELECT ts,type,player,obj,world,x,y,z,x2,y2,z2,txt FROM ev{clause} "
                f"ORDER BY ts {'DESC' if newest_first else 'ASC'}, rowid {'DESC' if newest_first else 'ASC'} LIMIT ?",
                args + [limit]).fetchall()
        n = self.name
        return [Event(ts=r[0], type=n(r[1]), player=n(r[2]), obj=n(r[3]), world=n(r[4]), x=r[5], y=r[6], z=r[7],
                      x2=r[8], y2=r[9], z2=r[10], txt=r[11]) for r in rows]

    def count(self, **filters) -> int:
        with self._lock:
            where = self._where(**filters)
            if where is None:
                return 0
            clause, args = where
            return self.db.execute(f"SELECT COUNT(*) FROM ev{clause}", args).fetchone()[0]

    def grouped(self, column: str, limit: int = 10, **filters) -> list[tuple[str, int]]:
        assert column in ("type", "obj", "player", "world")
        with self._lock:
            where = self._where(**filters)
            if where is None:
                return []
            clause, args = where
            rows = self.db.execute(f"SELECT {column}, COUNT(*) FROM ev{clause} GROUP BY {column} "
                                   f"ORDER BY 2 DESC LIMIT ?", args + [limit]).fetchall()
        return [(self.name(k) or "-", c) for k, c in rows]

    def histogram(self, bucket: int = 3600, **filters) -> list[tuple[int, int]]:
        """[(bucket start ts, count)] sorted by time."""
        with self._lock:
            where = self._where(**filters)
            if where is None:
                return []
            clause, args = where
            return self.db.execute(f"SELECT ts / {int(bucket)} * {int(bucket)}, COUNT(*) FROM ev{clause} "
                                   f"GROUP BY 1 ORDER BY 1", args).fetchall()

    def regions(self, limit: int = 3, size: int = 64, **filters) -> list[tuple[str, int, int, int]]:
        """Busiest areas: [(world, center x, center z, count)]."""
        with self._lock:
            where = self._where(**filters)
            if where is None:
                return []
            clause, args = where
            clause = (clause + " AND" if clause else " WHERE") + " x IS NOT NULL"
            rows = self.db.execute(
                f"SELECT world, (x - (x < 0) * {size - 1}) / {size}, (z - (z < 0) * {size - 1}) / {size}, "
                f"COUNT(*) FROM ev{clause} GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT ?", args + [limit]).fetchall()
        return [(self.name(w) or "?", rx * size + size // 2, rz * size + size // 2, n) for w, rx, rz, n in rows]

    def counted(self, start=None, end=None, limit: int = 10, type_word: str | None = None) -> list[tuple[str, str, int]]:
        """Top counted (not stored) events by name: [("", obj, n)]. type_word e.g. "SPAWN"."""
        sql = "SELECT obj, SUM(n) FROM counts WHERE bucket >= ? AND bucket <= ?"
        args = [int(start or 0) // COUNT_BUCKET * COUNT_BUCKET, int(end or 2**40)]
        if type_word:
            ids = self.ids_like([type_word])
            if not ids:
                return []
            sql += f" AND type IN ({','.join('?' * len(ids))})"
            args += ids
        with self._lock:
            rows = self.db.execute(sql + " GROUP BY obj ORDER BY 2 DESC LIMIT ?", args + [limit]).fetchall()
        return [("", self.name(o), n) for o, n in rows]

    def counted_total(self, start=None, end=None, type_word: str | None = None) -> int:
        return sum(n for _, _, n in self.counted(start, end, 10**6, type_word))

    def alerts(self, start=None, end=None, players=None, limit: int = 50) -> list[tuple]:
        sql, args = "SELECT ts, player, kind, text FROM alerts WHERE ts >= ? AND ts <= ?", [int(start or 0),
                                                                                        int(end or 2**40)]
        if players:
            sql += f" AND lower(player) IN ({','.join('?' * len(players))})"
            args += [p.lower() for p in players]
        with self._lock:
            return self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def fmt_pos(world, x, y, z) -> str:
    if x is None:
        return world or "?"
    return f"{world or '?'} {x} {y} {z}"


def fmt_event(ev: Event, tz) -> str:
    t = datetime.fromtimestamp(ev.ts, tz).strftime("%d.%m. %H:%M:%S")
    who = f"{ev.player} " if ev.player else ""
    what = ev.obj or ""
    extra = f" {ev.txt}" if ev.txt and ev.txt != ev.obj else ""
    where = ""
    if ev.x is not None:
        where = f" @ {fmt_pos(ev.world, ev.x, ev.y, ev.z)}"
        if ev.x2 is not None:
            where += f" -> {ev.x2} {ev.y2} {ev.z2}"
    elif ev.world:
        where = f" @ {ev.world}"
    return f"{t}  {ev.type:<16} {who}{what}{extra}{where}".rstrip()


def sessions(events_asc: list[Event], start: int, end: int) -> list[tuple[int, int | None]]:
    """(join, quit) pairs from PLAYER_JOIN / PLAYER_QUIT / PLAYER_KICK events."""
    out, opened = [], None
    for ev in events_asc:
        if ev.type == "PLAYER_JOIN":
            if opened is not None:
                out.append((opened, ev.ts))
            opened = ev.ts
        elif ev.type in ("PLAYER_QUIT", "PLAYER_KICK"):
            out.append((opened if opened is not None else start, ev.ts))
            opened = None
    if opened is not None:
        out.append((opened, None))
    return out


def player_summary(store: LogStore, player: str, start: int, end: int, tz) -> dict:
    flt = dict(players=[player], start=start, end=end)
    by_type = dict(store.grouped("type", limit=50, **flt))
    asc = store.events(limit=20000, newest_first=False, **flt)
    sess = sessions([e for e in asc if e.type in ("PLAYER_JOIN", "PLAYER_QUIT", "PLAYER_KICK")], start, end)
    online = sum(((q or min(end, int(time.time()))) - j) for j, q in sess)
    regions = Counter(((e.world, e.x // 64, e.z // 64) for e in asc if e.x is not None))
    area = None
    if regions:
        (w, rx, rz), _ = regions.most_common(1)[0]
        area = f"{w} around x {rx * 64 + 32}, z {rz * 64 + 32}"
    return {
        "player": player,
        "total": sum(by_type.values()),
        "by_type": by_type,
        "placed": store.grouped("obj", limit=8, types=["BLOCK_PLACE"], **flt),
        "broken": store.grouped("obj", limit=8, types=["BLOCK_BREAK"], **flt),
        "commands": [(e.ts, e.txt) for e in asc if e.type == "PLAYER_COMMAND"][-25:],
        "deaths": [(e.ts, e.txt or e.obj) for e in asc if e.type == "PLAYER_DEATH"][-10:],
        "teleports": [(e.ts, e.world, *e.dest) for e in asc if "TELEPORT" in e.type][-10:],
        "worlds": [e.obj for e in asc if e.type == "WORLD_CHANGE"][-10:],
        "modes": [(e.ts, e.obj) for e in asc if "GAME_MODE" in e.type or "GAMEMODE" in e.type][-10:],
        "sessions": sess,
        "online_seconds": online,
        "first": asc[0].ts if asc else None,
        "last": asc[-1].ts if asc else None,
        "area": area,
        "alerts": store.alerts(start, end, players=[player], limit=10),
    }


def duration_text(seconds: int) -> str:
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m}min" if h else f"{m}min"


def summary_text(s: dict, tz) -> str:
    """Plain text summary of a player (used for Discord embeds and as AI context)."""
    t = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%d.%m. %H:%M")  # noqa: E731
    if not s["total"]:
        return f"{s['player']}: no events in this time range."
    lines = [f"Events: {s['total']} ({', '.join(f'{k} {v}' for k, v in list(s['by_type'].items())[:8])})"]
    if s["first"]:
        lines.append(f"Active: {t(s['first'])} - {t(s['last'])}")
    if s["sessions"]:
        sess = ", ".join(f"{t(j)}-{t(q)[-5:] if q else 'still online'}" for j, q in s["sessions"][-6:])
        lines.append(f"Online: {duration_text(s['online_seconds'])} ({sess})")
    if s["area"]:
        lines.append(f"Mostly at: {s['area']}")
    if s["placed"]:
        lines.append("Placed: " + ", ".join(f"{n}x {o}" for o, n in s["placed"]))
    if s["broken"]:
        lines.append("Broke: " + ", ".join(f"{n}x {o}" for o, n in s["broken"]))
    if s["commands"]:
        lines.append("Commands: " + "; ".join(f"{t(ts)[-5:]} {c}" for ts, c in s["commands"][-12:]))
    if s["modes"]:
        lines.append("Game mode: " + ", ".join(f"{t(ts)[-5:]} {m}" for ts, m in s["modes"]))
    if s["teleports"]:
        lines.append("Teleports: " + "; ".join(f"{t(ts)[-5:]} -> {fmt_pos(w, x, y, z)}"
                                               for ts, w, x, y, z in s["teleports"][-6:]))
    if s["worlds"]:
        lines.append("Worlds: " + " -> ".join(s["worlds"][-6:]))
    if s["deaths"]:
        lines.append("Deaths: " + "; ".join(f"{t(ts)[-5:]} {d}" for ts, d in s["deaths"][-5:]))
    if s["alerts"]:
        lines.append("Suspicious: " + "; ".join(f"{t(ts)[-5:]} {text}" for ts, _, _, text in s["alerts"][:5]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Questions in plain language (German or English)
# ---------------------------------------------------------------------------

ACTIONS = [
    (("abgebaut", "abbauen", "abbau", "zerstört", "zerstoert", "kaputt", "kaputtgemacht", "break", "broke", "broken",
      "mined", "mining", "gemined", "gefarmt", "grief", "gegrieft"), ["BLOCK_BREAK"]),
    (("platziert", "platzieren", "gebaut", "bauen", "gesetzt", "baut", "place", "placed", "built", "build",
      "grief", "gegrieft"), ["BLOCK_PLACE"]),
    (("befehl", "befehle", "command", "commands", "cmd", "eingegeben"), ["PLAYER_COMMAND"]),
    (("gestorben", "sterben", "tod", "tode", "death", "deaths", "died", "starb", "stirbt"), ["PLAYER_DEATH"]),
    (("teleport", "teleports", "teleportiert", "tp"), ["PLAYER_TELEPORT"]),
    (("online", "gejoint", "beigetreten", "verlassen", "join", "joined", "left", "quit", "spielzeit", "playtime",
      "eingeloggt", "ausgeloggt", "gespielt", "drauf"), ["PLAYER_JOIN", "PLAYER_QUIT", "PLAYER_KICK"]),
    (("welt", "welten", "nether", "dimension", "world"), ["WORLD_CHANGE"]),
    (("gekickt", "kick", "kicked"), ["PLAYER_KICK"]),
    (("spielmodus", "gamemode", "spectator", "creative", "zuschauer", "kreativ"),
     ["PLAYER_GAME_MODE_CHANGE", "GAMEMODE_CHANGE"]),
    (("geöffnet", "geoeffnet", "aufgemacht", "öffnen", "oeffnen", "inventar", "inventare", "opened", "inventory",
      "inventories"), ["INVENTORY_OPEN"]),
    (("gedroppt", "gedropt", "droppen", "weggeworfen", "drop", "drops", "dropped"), ["PLAYER_DROP_ITEM"]),
    (("chat", "gechattet", "geschrieben", "schrieb", "chatted", "chats"), ["PLAYER_CHAT"]),
    (("enderperle", "enderperlen", "perle", "perlen", "pearl", "pearls"), ["ENDER_PEARL_THROW"]),
    (("respawnt", "respawn", "respawns", "wiederbelebt"), ["PLAYER_RESPAWN"]),
    (("konsole", "console", "commandblock", "befehlsblock"), ["CONSOLE"]),
    (("geworfen", "werfen", "thrown", "throw"), []),   # known words, no own action ("Enderperlen geworfen")
]
ACTION_VOCAB = {w: types for words, types in ACTIONS for w in words}
BLOCK_WORDS = ("blöcke", "bloecke", "block", "blocks", "blöcken")
MATERIAL_WORDS = {"diamant": "DIAMOND", "diamanten": "DIAMOND", "diamond": "DIAMOND", "diamonds": "DIAMOND",
                  "eisen": "IRON", "iron": "IRON", "gold": "GOLD", "erz": "_ORE", "erze": "_ORE", "ore": "_ORE",
                  "ores": "_ORE", "holz": "_LOG", "stamm": "_LOG", "bretter": "PLANKS", "stein": "STONE",
                  "tnt": "TNT", "lava": "LAVA", "wasser": "WATER", "truhe": "CHEST", "truhen": "CHEST", "kisten": "CHEST",
                  "kiste": "CHEST", "chest": "CHEST", "netherit": "ANCIENT_DEBRIS", "netherite": "ANCIENT_DEBRIS",
                  "kohle": "COAL", "coal": "COAL", "smaragd": "EMERALD", "emerald": "EMERALD",
                  "redstone": "REDSTONE", "lapis": "LAPIS", "kupfer": "COPPER", "copper": "COPPER",
                  "glas": "GLASS", "glass": "GLASS", "erde": "DIRT", "dirt": "DIRT", "obsidian": "OBSIDIAN",
                  "spawner": "SPAWNER", "shulker": "SHULKER", "bett": "_BED", "feuer": "FIRE", "fire": "FIRE",
                  "sand": "SAND", "wolle": "WOOL", "wool": "WOOL", "beton": "CONCRETE", "concrete": "CONCRETE"}
SUSPICIOUS_WORDS = ("verdächtig", "verdaechtig", "suspicious", "auffällig", "auffaellig", "cheat", "hack", "xray",
                    "x-ray", "vanish", "gemeldet", "alarm")
SPAWN_WORDS = ("mobs", "mob", "spawn", "gespawnt", "spawns", "monster", "tiere", "zombies", "creeper")
GERMAN_WORDS = {"wer", "was", "wie", "wann", "wo", "hat", "haben", "wurde", "wurden", "heute", "gestern", "der", "die",
                "das", "und", "viele", "wieviel", "insgesamt", "gemacht", "ist", "sind", "seit", "zwischen", "uhr",
                "bei", "von", "bis", "alle", "welche", "gab", "es", "letzten", "spieler", "mit", "im", "am", "ein"}
STOP_WORDS = GERMAN_WORDS | {"the", "did", "what", "who", "how", "many", "much", "when", "where", "was", "and", "in",
                             "on", "at", "a", "an", "of", "to", "do", "does", "have", "has", "total", "today",
                             "yesterday", "last", "hours", "player", "players", "stunden", "minuten", "tage",
                             "insgesamt", "gesamt", "alles", "bitte", "mir", "zeig", "sag", "server"}


@dataclass
class Question:
    players: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    obj_words: list[str] = field(default_factory=list)
    start: int = 0
    end: int = 0
    near: tuple | None = None
    suspicious: bool = False
    spawns: bool = False
    blocks: bool = False               # "Blöcke" without a verb: placed AND broken
    intent: str = "what"               # what / count / who / when / where / size
    lang: str = "en"
    time_text: str = ""
    default_time: bool = True
    fuzzy: list[str] = field(default_factory=list)   # "abgaubt -> abgebaut"


def _clock(h: str, m: str | None) -> tuple[int, int]:
    return int(h), int(m or 0)


def parse_time_spec(text: str, now: datetime) -> datetime | None:
    """'14:00', '14 uhr', 'gestern 14:00', '01.10. 14:00', '2h' (= 2 hours ago), 'heute', 'gestern'."""
    t = text.strip().lower()
    if not t:
        return None
    m = re.fullmatch(r"(\d+)\s*(m|min|minuten|minutes|h|std|stunden|hours|d|tag|tage|days)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        return now - (timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit in "hs" else timedelta(days=n))
    day = now.date()
    if t.startswith(("gestern", "yesterday")):
        day -= timedelta(days=1)
        t = t.split(None, 1)[1] if " " in t else ""
    elif t.startswith(("vorgestern",)):
        day -= timedelta(days=2)
        t = t.split(None, 1)[1] if " " in t else ""
    elif t.startswith(("heute", "today")):
        t = t.split(None, 1)[1] if " " in t else ""
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})?\s*", t)
    if m:
        year = int(m.group(3)) if m.group(3) else now.year
        year += 2000 if year < 100 else 0
        try:
            day = day.replace(year=year, month=int(m.group(2)), day=int(m.group(1)))
        except ValueError:
            return None
        t = t[m.end():]
    t = t.replace("uhr", "").strip()
    if not t:
        return datetime(day.year, day.month, day.day, tzinfo=now.tzinfo)
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", t)
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return None
    h, mi = _clock(m.group(1), m.group(2))
    result = datetime(day.year, day.month, day.day, h, mi, tzinfo=now.tzinfo)
    if result > now and day == now.date() and not text.strip().lower().startswith(("heute", "today")):
        result -= timedelta(days=1)  # "14:00" late at night means today, at 9 in the morning yesterday
    return result


def detect_lang(text: str) -> str:
    low = text.lower()
    tokens = set(re.findall(r"[a-zäöüß]+", low))
    return "de" if (tokens & GERMAN_WORDS) or re.search(r"[äöüß]", low) else "en"


def _match_players(tokens: list[str], low: str, known: set[str], fuzzy: list[str]) -> list[str]:
    found = []
    for name in sorted(known, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])", low):
            found.append(name)
    lower = {n.lower(): n for n in known}
    for tok in tokens:
        if len(tok) < 4 or tok in STOP_WORDS or any(tok == f.lower() for f in found):
            continue
        hit = [n for n in lower if n.startswith(tok)] if len(tok) >= 4 else []
        if len(hit) != 1:
            hit = difflib.get_close_matches(tok, list(lower), n=1, cutoff=0.8)
        if hit and lower[hit[0]] not in found:
            found.append(lower[hit[0]])
            if hit[0] != tok:
                fuzzy.append(f"{tok} → {lower[hit[0]]}")
    return found


def parse_question(text: str, now: datetime, known_players: set[str], retention_days: float = 4) -> Question:
    q = Question(lang=detect_lang(text))
    low = text.lower()
    tokens = re.findall(r"[a-zäöüß0-9_]+", low)
    q.players = _match_players(tokens, low, known_players, q.fuzzy)
    player_tokens = {p.lower() for p in q.players}
    for tok in tokens:
        if tok in STOP_WORDS or tok in player_tokens:
            continue
        types = ACTION_VOCAB.get(tok)
        if types is None and len(tok) >= 5:
            close = difflib.get_close_matches(tok, [w for w in ACTION_VOCAB if len(w) >= 5], n=1, cutoff=0.78)
            if close:
                types = ACTION_VOCAB[close[0]]
                q.fuzzy.append(f"{tok} → {close[0]}")
        if types:
            q.types += [t for t in types if t not in q.types]
        material = MATERIAL_WORDS.get(tok)
        if material and material not in q.obj_words:
            q.obj_words.append(material)
    if any(w in tokens for w in BLOCK_WORDS) and not q.types:
        q.blocks = True
        q.types = ["BLOCK_PLACE", "BLOCK_BREAK"]
    q.suspicious = any(w in low for w in SUSPICIOUS_WORDS)
    q.spawns = any(re.search(rf"\b{w}\b", low) for w in SPAWN_WORDS)
    if re.search(r"\b(wie gro(ß|ss)|grö(ß|ss)e|groesse|speicherplatz|how big|how large|size|megabytes?|"
                 r"kilobytes?|mb|kb|datenmenge|how much data)\b", low):
        q.intent = "size"
    elif re.search(r"\b(wie ?viele?|wieviel|anzahl|how many|how much|insgesamt|total|gesamt)\b", low):
        q.intent = "count"
    elif re.search(r"\b(wer|who|welche[rn]? spieler|which player)\b", low):
        q.intent = "who"
    elif re.search(r"\b(wann|when|um wie ?viel uhr)\b", low):
        q.intent = "when"
    elif re.search(r"\b(wo|where|an welche[rm] (stelle|ort))\b", low):
        q.intent = "where"
    m = re.search(r"(?:bei|near|um|at|nähe|naehe|koordinaten|coords?)\s*(?:x\s*=?\s*)?(-?\d+)[\s,/]+"
                  r"(?:y\s*=?\s*)?(-?\d+)(?:[\s,/]+(?:z\s*=?\s*)?(-?\d+))?", low)
    if m:
        x, a, b = int(m.group(1)), int(m.group(2)), m.group(3)
        q.near = (x, int(b), 32) if b is not None else (x, a, 32)

    # time range
    start, end = now - timedelta(hours=24), now
    day_offset = None
    if re.search(r"\bvorgestern\b", low):
        day_offset = 2
    elif re.search(r"\b(gestern|yesterday)\b", low):
        day_offset = 1
    elif re.search(r"\b(heute|today)\b", low):
        day_offset = 0
    base = now.date() - timedelta(days=day_offset or 0)
    hhmm = r"(\d{1,2})(?:[:.](\d{2}))?"
    rng = re.search(rf"(?:zwischen|between|von|from)\s+{hhmm}\s*(?:uhr)?\s*(?:und|and|bis|to|-)\s*{hhmm}", low)
    since = re.search(rf"(?:seit|since|ab|after)\s+{hhmm}\s*(?:uhr)?", low)
    at = re.search(rf"(?:um|at|gegen|around)\s+{hhmm}\s*uhr", low) or re.search(rf"\b(?:um|at|gegen)\s+{hhmm}\b(?!\s*[\d,/-])", low)
    last = re.search(r"(?:letzte[nrs]?|vergangene[nrs]?|last|past)\s+(\d+)?\s*(minute|min|stunde|std|hour|tag|day|woche|week)", low)
    date = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?", low)
    if date:
        try:
            base = base.replace(month=int(date.group(2)), day=int(date.group(1)))
            day_offset = day_offset if day_offset is not None else -1
        except ValueError:
            pass

    def at_time(h, mi):
        return datetime(base.year, base.month, base.day, int(h), int(mi or 0), tzinfo=now.tzinfo)

    q.default_time = False
    try:
        if rng:
            start, end = at_time(rng.group(1), rng.group(2)), at_time(rng.group(3), rng.group(4))
            if end <= start:
                end += timedelta(days=1)
            if day_offset is None and start > now:
                start, end = start - timedelta(days=1), end - timedelta(days=1)
            q.time_text = "range"
        elif since:
            start = at_time(since.group(1), since.group(2))
            if day_offset is None and start > now:
                start -= timedelta(days=1)
            end = now if day_offset in (None, 0) else datetime(base.year, base.month, base.day, tzinfo=now.tzinfo) + timedelta(days=1)
            q.time_text = "since"
        elif at:
            start = at_time(at.group(1), at.group(2)) - timedelta(minutes=30)
            end = start + timedelta(hours=1)
            if day_offset is None and start > now:
                start, end = start - timedelta(days=1), end - timedelta(days=1)
            q.time_text = "at"
        elif last:
            n = int(last.group(1) or 1)
            unit = last.group(2)
            delta = (timedelta(minutes=n) if unit.startswith("min") else timedelta(days=n) if unit in ("tag", "day")
                     else timedelta(weeks=n) if unit in ("woche", "week") else timedelta(hours=n))
            start, end = now - delta, now
            q.time_text = "last"
        elif day_offset is not None:
            start = datetime(base.year, base.month, base.day, tzinfo=now.tzinfo)
            end = min(now, start + timedelta(days=1))
            q.time_text = "day"
        elif re.search(r"\b(insgesamt|gesamt|total|alle zeit|überhaupt|ueberhaupt|ever|all time)\b", low):
            start = now - timedelta(days=retention_days)
            q.time_text = "all"
        else:
            q.default_time = True
    except ValueError:
        q.default_time = True
    oldest = now - timedelta(days=retention_days)
    q.start, q.end = int(max(start, oldest).timestamp()), int(min(end, now).timestamp())
    return q


# ---------------------------------------------------------------------------
# Answers in plain language
# ---------------------------------------------------------------------------

TYPE_WORDS = {
    "de": {"BLOCK_BREAK": ("Blöcke abgebaut", "abgebaut", "done"), "BLOCK_PLACE": ("Blöcke platziert", "platziert", "done"),
           "PLAYER_COMMAND": ("Befehle ausgeführt", "Befehle", "done"), "PLAYER_DEATH": ("Tode", "Tode", "noun"),
           "PLAYER_TELEPORT": ("Teleports", "Teleports", "noun"), "PLAYER_JOIN": ("Logins", "online", "noun"),
           "PLAYER_QUIT": ("Logouts", "Logouts", "noun"), "PLAYER_KICK": ("Kicks", "Kicks", "noun"),
           "WORLD_CHANGE": ("Weltwechsel", "Weltwechsel", "noun"),
           "PLAYER_GAME_MODE_CHANGE": ("Spielmodus-Wechsel", "Spielmodus", "noun"),
           "INVENTORY_OPEN": ("Inventare/Kisten geöffnet", "geöffnet", "done"),
           "PLAYER_DROP_ITEM": ("Items gedroppt", "gedroppt", "done"),
           "PLAYER_CHAT": ("Chat-Nachrichten", "Chat", "noun"), "PLAYER_RESPAWN": ("Respawns", "Respawns", "noun"),
           "ENDER_PEARL_THROW": ("Enderperlen geworfen", "Enderperlen", "done"),
           "CONSOLE": ("Konsolen-Befehle", "Konsole", "noun")},
    "en": {"BLOCK_BREAK": ("blocks broken", "broken", "done"), "BLOCK_PLACE": ("blocks placed", "placed", "done"),
           "PLAYER_COMMAND": ("commands run", "commands", "done"), "PLAYER_DEATH": ("deaths", "deaths", "noun"),
           "PLAYER_TELEPORT": ("teleports", "teleports", "noun"), "PLAYER_JOIN": ("logins", "online", "noun"),
           "PLAYER_QUIT": ("logouts", "logouts", "noun"), "PLAYER_KICK": ("kicks", "kicks", "noun"),
           "WORLD_CHANGE": ("world changes", "world changes", "noun"),
           "PLAYER_GAME_MODE_CHANGE": ("game mode changes", "game mode", "noun"),
           "INVENTORY_OPEN": ("inventories/chests opened", "opened", "done"),
           "PLAYER_DROP_ITEM": ("items dropped", "dropped", "done"),
           "PLAYER_CHAT": ("chat messages", "chat", "noun"), "PLAYER_RESPAWN": ("respawns", "respawns", "noun"),
           "ENDER_PEARL_THROW": ("ender pearls thrown", "ender pearls", "done"),
           "CONSOLE": ("console commands", "console", "noun")},
}
SINGULAR = {"Tode": "Tod", "Teleports": "Teleport", "Logins": "Login", "Logouts": "Logout", "Kicks": "Kick",
            "Befehle ausgeführt": "Befehl ausgeführt", "Blöcke abgebaut": "Block abgebaut",
            "Blöcke platziert": "Block platziert", "deaths": "death", "teleports": "teleport", "logins": "login",
            "logouts": "logout", "kicks": "kick", "commands run": "command run", "blocks broken": "block broken",
            "blocks placed": "block placed", "world changes": "world change", "game mode changes": "game mode change",
            "Inventare/Kisten geöffnet": "Inventar/Kiste geöffnet", "Items gedroppt": "Item gedroppt",
            "Chat-Nachrichten": "Chat-Nachricht", "Respawns": "Respawn", "Enderperlen geworfen": "Enderperle geworfen",
            "Konsolen-Befehle": "Konsolen-Befehl", "inventories/chests opened": "inventory/chest opened",
            "items dropped": "item dropped", "chat messages": "chat message", "respawns": "respawn",
            "ender pearls thrown": "ender pearl thrown", "console commands": "console command"}
PLURALS = {"de": {"cmds": ("Befehl", "Befehle"), "deaths": ("Tod", "Tode"), "reports": ("Meldung", "Meldungen")},
           "en": {"cmds": ("command", "commands"), "deaths": ("death", "deaths"), "reports": ("report", "reports")}}


def _pl(n: int, key: str, lang: str) -> str:
    one, many = PLURALS[lang][key]
    return f"{_n(n, lang)} {one if n == 1 else many}"


T = {
    "de": {
        "span_default": "in den letzten 24 Stunden", "span": "zwischen {a} und {b}",
        "report_words": ("Meldung", "Meldungen"),
        "none": "Dazu habe ich {span} **nichts gefunden**.",
        "none_hint": "Gespeichert sind Logs von {first} bis {last}. Probier einen anderen Zeitraum, z.B. „gestern“ "
                     "oder „letzte 3 Tage“.",
        "empty_store": "Es sind noch keine Logs gespeichert - ist der Log-Kanal gesetzt (`/mclog channel`)?",
        "total": "{span_cap} wurden insgesamt **{n} {what}**.",
        "total_noun": "{span_cap} gab es insgesamt **{n} {what}**.",
        "total_one": "{span_cap} wurde insgesamt **{n} {what}**.",
        "total_noun_one": "{span_cap} gab es insgesamt **{n} {what}**.",
        "total_p": "**{p}** hat {span} **{n} {what}**.",
        "total_p_noun": "Bei **{p}** gab es {span} **{n} {what}**.",
        "online_head": "{span_cap} waren **{n} Spieler** online:", "online_none": "{span_cap} war **niemand** online.",
        "online_line": "**{p}** – {d} ({sess})", "online_before": "schon vorher eingeloggt",
        "online_active": "**{p}** – aktiv, aber kein Login/Logout im Zeitraum",
        "mobs_total": "{span_cap} wurden **{n} Mobs gespawnt** (gezählt):",
        "entities_total": "{span_cap} gab es **{n} Mob-/Entity-Ereignisse** (gezählt):",
        "mobs_none": "{span_cap} wurden **keine Mobs** gezählt.",
        "material_what": "{m}-Blöcke {verb}",
        "blocks_total": "{span_cap} wurden **{placed} Blöcke platziert** und **{broken} abgebaut**.",
        "blocks_p": "**{p}** hat {span} **{placed} Blöcke platziert** und **{broken} abgebaut**.",
        "by_player": "**Nach Spielern:**", "by_material": "**Am häufigsten:**", "busiest": "**Aktivste Zeit:** {h} Uhr ({n})",
        "first_last": "**Erstes / letztes Mal:** {a} · {b}", "where": "**Wo:**", "when": "**Wann (letzte):**",
        "top_who": "Am meisten: **{p}** mit {n} ({pct} %).",
        "overview": "{span_cap} waren **{n} Spieler** aktiv.",
        "overview_none": "{span_cap} war **niemand** aktiv.",
        "overview_blocks": "Insgesamt **{placed} Blöcke platziert**, **{broken} abgebaut**, {cmds}, {deaths}.",
        "per_player": "**Pro Spieler:**",
        "pp_line": "**{p}** – {parts}",
        "online": "online {d}", "placed": "{n} platziert", "broken": "{n} abgebaut",
        "mobs": "**Mobs (gezählt):** {list}", "alerts_none": "Verdächtiges: **nichts gemeldet** ✅",
        "alerts": "⚠️ **{n} verdächtige {w}:**",
        "story_head": "**{p}** war {span} aktiv (erste Aktion {a}, letzte {b}).",
        "story_none": "Von **{p}** gibt es {span} **keine Einträge**.",
        "s_online": "🕒 **Online:** {d} ({sess})", "s_placed": "🧱 **Platziert:** {n} Blöcke – vor allem {top}",
        "s_broken": "⛏️ **Abgebaut:** {n} Blöcke – vor allem {top}", "s_cmds": "⌨️ **Befehle ({n}):** {list}",
        "s_tp": "🌀 **Teleports:** {n} (zuletzt nach `{dest}`)", "s_worlds": "🌍 **Weltwechsel:** {list}",
        "s_deaths": "💀 **Tode ({n}):** {list}", "s_modes": "🎮 **Spielmodus:** {list}",
        "s_area": "📍 **Meistens:** {area}", "still": "noch online",
        "understood": "Verstanden: {parts}", "u_players": "Spieler {v}", "u_all_players": "alle Spieler",
        "u_action": "Aktion {v}", "u_material": "Material {v}", "u_near": "bei x {x}, z {z}",
        "u_time": "Zeitraum {v}", "u_fuzzy": "Tippfehler erkannt: {v}",
        "size_in": "{span_cap} kamen **{msgs} Nachrichten** mit **{lines} Log-Zeilen** an – das sind etwa "
                   "**{size}** Text.",
        "size_understood": "Davon hat der Bot **{n} verstanden** ({pct} %).",
        "size_unknown": "**{n} Zeilen** hat er nicht verstanden – die stehen im Bericht (`/mclog report`).",
        "size_none": "Für diesen Zeitraum gibt es noch keine Nachrichten-Statistik (die zählt der Bot seit {since}).",
        "size_none_new": "Für diesen Zeitraum gibt es noch keine Nachrichten-Statistik (die zählt der Bot erst seit "
                         "dem letzten Update).",
        "size_stored": "Gespeichert davon: **{stored} einzelne Einträge** und **{counted} nur gezählte** "
                       "(Mob-Spawns usw.).",
        "size_db": "Die ganze Log-Datenbank ist gerade **{db} MB** groß (Limit {max} MB, sie behält {days} Tage).",
        "busiest_min": "Meiste Nachrichten in einer Minute: **{n}** ({h} Uhr).",
        "limit_warn": "⚠️ In **{n} Minute(n)** war der Kanal am Discord-Limit (~30 Nachrichten pro Minute) – da "
                      "kann das Plugin Ereignisse verloren haben. Details: `/mclog report`.",
    },
    "en": {
        "span_default": "in the last 24 hours", "span": "between {a} and {b}",
        "report_words": ("report", "reports"),
        "none": "I found **nothing** for that {span}.",
        "none_hint": "Logs are stored from {first} to {last}. Try another time range, e.g. “yesterday” or "
                     "“last 3 days”.",
        "empty_store": "No logs stored yet - is the log channel set (`/mclog channel`)?",
        "total": "{span_cap}, **{n} {what}** in total.",
        "total_noun": "{span_cap}, there were **{n} {what}** in total.",
        "total_one": "{span_cap}, **{n} {what}** in total.",
        "total_noun_one": "{span_cap}, there was **{n} {what}** in total.",
        "total_p": "**{p}**: **{n} {what}** {span}.",
        "total_p_noun": "**{p}**: **{n} {what}** {span}.",
        "online_head": "{span_cap}, **{n} players** were online:", "online_none": "{span_cap}, **nobody** was online.",
        "online_line": "**{p}** – {d} ({sess})", "online_before": "logged in before",
        "online_active": "**{p}** – active, but no login/logout in this time",
        "mobs_total": "{span_cap}, **{n} mob spawns** were counted:",
        "entities_total": "{span_cap}, **{n} mob/entity events** were counted:",
        "mobs_none": "{span_cap}, **no mobs** were counted.",
        "material_what": "{m} blocks {verb}",
        "blocks_total": "{span_cap}, **{placed} blocks were placed** and **{broken} broken**.",
        "blocks_p": "**{p}** placed **{placed} blocks** and broke **{broken}** {span}.",
        "by_player": "**By player:**", "by_material": "**Most common:**", "busiest": "**Busiest hour:** {h}:00 ({n})",
        "first_last": "**First / last time:** {a} · {b}", "where": "**Where:**", "when": "**When (latest):**",
        "top_who": "Most: **{p}** with {n} ({pct}%).",
        "overview": "{span_cap}, **{n} players** were active.",
        "overview_none": "{span_cap}, **nobody** was active.",
        "overview_blocks": "In total **{placed} blocks placed**, **{broken} broken**, {cmds}, {deaths}.",
        "per_player": "**Per player:**",
        "pp_line": "**{p}** – {parts}",
        "online": "online {d}", "placed": "{n} placed", "broken": "{n} broken",
        "mobs": "**Mobs (counted):** {list}", "alerts_none": "Suspicious: **nothing reported** ✅",
        "alerts": "⚠️ **{n} suspicious {w}:**",
        "story_head": "**{p}** was active {span} (first action {a}, last {b}).",
        "story_none": "There are **no entries** for **{p}** {span}.",
        "s_online": "🕒 **Online:** {d} ({sess})", "s_placed": "🧱 **Placed:** {n} blocks – mostly {top}",
        "s_broken": "⛏️ **Broke:** {n} blocks – mostly {top}", "s_cmds": "⌨️ **Commands ({n}):** {list}",
        "s_tp": "🌀 **Teleports:** {n} (last to `{dest}`)", "s_worlds": "🌍 **World changes:** {list}",
        "s_deaths": "💀 **Deaths ({n}):** {list}", "s_modes": "🎮 **Game mode:** {list}",
        "s_area": "📍 **Mostly at:** {area}", "still": "still online",
        "understood": "Understood: {parts}", "u_players": "player {v}", "u_all_players": "all players",
        "u_action": "action {v}", "u_material": "material {v}", "u_near": "near x {x}, z {z}",
        "u_time": "time {v}", "u_fuzzy": "typo fixed: {v}",
        "size_in": "{span_cap}, **{msgs} messages** with **{lines} log lines** came in – about **{size}** of text.",
        "size_understood": "The bot **understood {n}** of them ({pct}%).",
        "size_unknown": "**{n} lines** weren't understood – they're in the report (`/mclog report`).",
        "size_none": "There are no message statistics for this time yet (the bot counts them since {since}).",
        "size_none_new": "There are no message statistics for this time yet (the bot counts them since the last "
                         "update).",
        "size_stored": "Stored from that: **{stored} single entries** and **{counted} only counted** "
                       "(mob spawns etc.).",
        "size_db": "The whole log database is **{db} MB** right now (limit {max} MB, it keeps {days} days).",
        "busiest_min": "Most messages in one minute: **{n}** ({h}).",
        "limit_warn": "⚠️ In **{n} minute(s)** the channel was at Discord's limit (~30 messages per minute) – the "
                      "plugin may have lost events then. Details: `/mclog report`.",
    },
}


def _n(n: int, lang: str) -> str:
    text = f"{n:,}"
    return text.replace(",", ".") if lang == "de" else text


def _span(q: Question, tz) -> tuple[str, str]:
    L = T[q.lang]
    if q.default_time:
        text = L["span_default"]
    else:
        a, b = datetime.fromtimestamp(q.start, tz), datetime.fromtimestamp(q.end, tz)
        fmt = "%H:%M" if a.date() == b.date() else "%d.%m. %H:%M"
        text = L["span"].format(a=a.strftime("%d.%m. %H:%M"), b=b.strftime(fmt))
    return text, text[:1].upper() + text[1:]


def understood_text(q: Question, tz) -> str:
    L = T[q.lang]
    parts = [L["u_players"].format(v=", ".join(q.players)) if q.players else L["u_all_players"]]
    if q.types:
        parts.append(L["u_action"].format(v=", ".join(TYPE_WORDS[q.lang].get(t, (t, t))[1] for t in q.types
                                                       if t not in ("PLAYER_QUIT", "PLAYER_KICK", "GAMEMODE_CHANGE"))))
    if q.obj_words:
        parts.append(L["u_material"].format(v=", ".join(w.strip("_") for w in q.obj_words)))
    if q.near:
        parts.append(L["u_near"].format(x=q.near[0], z=q.near[1]))
    a, b = datetime.fromtimestamp(q.start, tz), datetime.fromtimestamp(q.end, tz)
    parts.append(L["u_time"].format(v=f"{a:%d.%m. %H:%M} – {b:%d.%m. %H:%M}"))
    if q.fuzzy:
        parts.append(L["u_fuzzy"].format(v=", ".join(q.fuzzy)))
    return L["understood"].format(parts=" · ".join(parts))


def _top(pairs, lang, limit=5) -> str:
    return ", ".join(f"{_n(c, lang)}× {o}" for o, c in pairs[:limit] if o != "-")


def _player_story(store: LogStore, q: Question, p: str, tz) -> str:
    L = T[q.lang]
    span, _ = _span(q, tz)
    s = player_summary(store, p, q.start, q.end, tz)
    if not s["total"]:
        return L["story_none"].format(p=p, span=span)
    t = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M" if q.end - q.start <= 86400 else "%d.%m. %H:%M")  # noqa: E731
    lines = [L["story_head"].format(p=p, span=span, a=t(s["first"]), b=t(s["last"]))]
    if s["sessions"]:
        sess = ", ".join(f"{t(j)}–{t(e) if e else L['still']}" for j, e in s["sessions"][-4:])
        lines.append(L["s_online"].format(d=duration_text(s["online_seconds"]), sess=sess))
    placed, broken = s["by_type"].get("BLOCK_PLACE", 0), s["by_type"].get("BLOCK_BREAK", 0)
    if placed:
        lines.append(L["s_placed"].format(n=_n(placed, q.lang), top=_top(s["placed"], q.lang, 4)))
    if broken:
        lines.append(L["s_broken"].format(n=_n(broken, q.lang), top=_top(s["broken"], q.lang, 4)))
    if s["commands"]:
        lines.append(L["s_cmds"].format(n=len(s["commands"]), list=", ".join(
            f"{t(ts)} `{c[:40]}`" for ts, c in s["commands"][-6:])))
    if s["teleports"]:
        ts, w, x, y, z = s["teleports"][-1]
        lines.append(L["s_tp"].format(n=len(s["teleports"]), dest=fmt_pos(w, x, y, z)))
    if s["worlds"]:
        lines.append(L["s_worlds"].format(list=" → ".join(s["worlds"][-5:])))
    if s["modes"]:
        lines.append(L["s_modes"].format(list=", ".join(f"{t(ts)} {m}" for ts, m in s["modes"][-4:])))
    if s["deaths"]:
        lines.append(L["s_deaths"].format(n=len(s["deaths"]), list="; ".join(
            f"{t(ts)} {d}" for ts, d in s["deaths"][-3:])))
    if s["area"]:
        lines.append(L["s_area"].format(area=s["area"]))
    if s["alerts"]:
        lines.append(L["alerts"].format(n=len(s["alerts"]), w=L["report_words"][len(s["alerts"]) != 1]) + "\n" + "\n".join(
            f"• {t(ts)} {text}" for ts, _, _, text in s["alerts"][:4]))
    else:
        lines.append(L["alerts_none"])
    return "\n".join(lines)


def _alerts_block(L, alerts, t) -> str:
    return L["alerts"].format(n=len(alerts), w=L["report_words"][len(alerts) != 1]) + "\n" + "\n".join(
        f"• `{t(ts)}` {text}" for ts, _, _, text in alerts)


def answer_question(store: LogStore, q: Question, tz) -> tuple[str, dict | None]:
    """Plain-language answer from the stored data. Returns (text, filter for the event file or None)."""
    L, W = T[q.lang], TYPE_WORDS[q.lang]
    span, span_cap = _span(q, tz)
    t = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%d.%m. %H:%M")  # noqa: E731
    hm = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M" if q.end - q.start <= 86400 else "%d.%m. %H:%M")  # noqa: E731
    stats = store.stats()
    if not stats["events"] and not stats["counted"]:
        return L["empty_store"], None
    parts: list[str] = []
    base = dict(start=q.start, end=q.end)
    block_types = {"BLOCK_PLACE", "BLOCK_BREAK"}
    online_q = "PLAYER_JOIN" in q.types and not (set(q.types) & block_types) and not q.obj_words and not q.near
    filtered = bool(q.types or q.obj_words or q.near) and not online_q
    flt = dict(base, players=q.players or None, types=q.types or None, obj_words=q.obj_words or None, near=q.near)

    traffic = store.ingest_stats(q.start, q.end)
    limit_minutes = traffic_problems(traffic)["limit_minutes"] if traffic["rows"] else 0
    limit_note = L["limit_warn"].format(n=limit_minutes) if limit_minutes else ""

    if q.intent == "size":
        if traffic["messages"]:
            parts.append(L["size_in"].format(span_cap=span_cap, msgs=_n(traffic["messages"], q.lang),
                                             lines=_n(traffic["lines"], q.lang),
                                             size=size_text(traffic["chars"], q.lang)))
            pct = round(100 * traffic["events"] / traffic["lines"]) if traffic["lines"] else 100
            pct = min(pct, 99) if traffic["unknown"] else pct
            line = L["size_understood"].format(n=_n(traffic["events"], q.lang), pct=pct)
            if traffic["unknown"]:
                line += " " + L["size_unknown"].format(n=_n(traffic["unknown"], q.lang))
            parts.append(line)
            busiest = max(traffic["rows"], key=lambda r: r[1])
            parts.append(L["busiest_min"].format(n=busiest[1], h=hm(busiest[0])))
        else:
            parts.append(L["size_none"].format(since=t(traffic["since"])) if traffic["since"] else L["size_none_new"])
        stored = store.count(**base)
        counted = store.counted_total(q.start, q.end)
        parts.append(L["size_stored"].format(stored=_n(stored, q.lang), counted=_n(counted, q.lang)))
        db = f"{stats['size_mb']:.1f}".replace(".", "," if q.lang == "de" else ".")
        parts.append(L["size_db"].format(db=db, max=f"{store.max_mb:g}", days=f"{store.retention_days:g}"))
        if limit_note:
            parts.append(limit_note)
        return "\n\n".join(parts), None

    if q.suspicious:
        alerts = store.alerts(q.start, q.end, q.players or None, 15)
        parts.append(_alerts_block(L, alerts, t) if alerts else L["alerts_none"])

    # --- who was online -------------------------------------------------------------
    if online_q:
        names = q.players or [p for p, _ in store.grouped("player", 30, **base) if p != "-"]
        lines = []
        for p in names:
            evs = store.events(500, False, players=[p], types=list(JOIN_TYPES), **base)
            sess = sessions(evs, q.start, q.end)
            if sess:
                total = sum(((e or min(q.end, int(time.time()))) - j) for j, e in sess)
                text = ", ".join(f"{hm(j) if evs and j >= evs[0].ts else L['online_before']}–{hm(e) if e else L['still']}"
                                 for j, e in sess[-4:])
                lines.append((total, "• " + L["online_line"].format(p=p, d=duration_text(total), sess=text)))
            else:
                lines.append((0, "• " + L["online_active"].format(p=p)))
        lines.sort(key=lambda x: -x[0])
        parts.append((L["online_head"].format(span_cap=span_cap, n=len(lines)) + "\n" + "\n".join(l for _, l in lines))
                     if lines else L["online_none"].format(span_cap=span_cap))
        return "\n\n".join(parts), dict(base, players=q.players or None, types=list(JOIN_TYPES))

    # --- mobs ---------------------------------------------------------------------------
    if q.spawns and not q.players and not filtered:
        spawns = store.counted(q.start, q.end, 10, "SPAWN")
        key = "mobs_total"
        if not spawns:
            spawns, key = store.counted(q.start, q.end, 10), "entities_total"
        if spawns:
            total = store.counted_total(q.start, q.end, "SPAWN" if key == "mobs_total" else None)
            parts.append(L[key].format(span_cap=span_cap, n=_n(total, q.lang)) + "\n" +
                         ", ".join(f"**{_n(n, q.lang)}** {o}" for _, o, n in spawns))
        else:
            parts.append(L["mobs_none"].format(span_cap=span_cap))
        return "\n\n".join(parts), None

    # --- counts / who / when / where for an action ---------------------------------------
    if filtered:
        if q.blocks:
            for p in (q.players or [None]):
                f = dict(flt, players=[p] if p else None)
                placed = store.count(**dict(f, types=["BLOCK_PLACE"]))
                broken = store.count(**dict(f, types=["BLOCK_BREAK"]))
                key = "blocks_p" if p else "blocks_total"
                parts.append(L[key].format(p=p, span=span, span_cap=span_cap, placed=_n(placed, q.lang),
                                           broken=_n(broken, q.lang)))
        main = next((x for x in q.types if x in W), q.types[0] if q.types else None)
        phrase, short, kind = W.get(main, (main or "", main or "", "noun")) if main else (
            "Einträge" if q.lang == "de" else "events", "", "noun")
        if q.obj_words and kind == "done" and main in block_types:
            phrase = L["material_what"].format(m=" / ".join(w.strip("_") for w in q.obj_words), verb=short)
        total = store.count(**flt)
        if not q.blocks:
            if q.players:
                for p in q.players:
                    n = store.count(**dict(flt, players=[p]))
                    parts.append(L["total_p" if kind == "done" else "total_p_noun"].format(
                        p=p, span=span, n=_n(n, q.lang), what=SINGULAR.get(phrase, phrase) if n == 1 else phrase))
            else:
                key = "total" if kind == "done" else "total_noun"
                if total == 1:
                    key += "_one"
                parts.append(L[key].format(span_cap=span_cap, n=_n(total, q.lang),
                                           what=SINGULAR.get(phrase, phrase) if total == 1 else phrase))
        if not total:
            first, last = stats["first"], stats["last"]
            parts.append(L["none"].format(span=span) + (" " + L["none_hint"].format(first=t(first), last=t(last))
                                                       if first else ""))
            return "\n\n".join(parts), None
        details = []
        if len(q.players) != 1:
            who = [(p, c) for p, c in store.grouped("player", 6, **flt) if p != "-"]
            if who:
                top_p, top_n = who[0]
                details.append(L["top_who"].format(p=top_p, n=_n(top_n, q.lang), pct=round(100 * top_n / total)))
                if len(who) > 1:
                    details.append(L["by_player"] + " " + ", ".join(f"{p} {_n(c, q.lang)}" for p, c in who))
        mats = [(o, c) for o, c in store.grouped("obj", 6, **flt) if o != "-"]
        if mats:
            details.append(L["by_material"] + " " + _top(mats, q.lang, 6))
        hist = store.histogram(3600, **flt)
        if len(hist) > 1:
            h_ts, h_n = max(hist, key=lambda r: r[1])
            details.append(L["busiest"].format(h=datetime.fromtimestamp(h_ts, tz).strftime("%d.%m. %H"),
                                               n=_n(h_n, q.lang)))
        oldest = store.events(1, False, **flt)
        newest = store.events(8 if q.intent in ("when", "where") else 1, True, **flt)
        if oldest and newest:
            details.append(L["first_last"].format(a=t(oldest[0].ts), b=t(newest[0].ts)))
        regions = store.regions(3, **flt)
        if regions and (q.intent == "where" or q.near or set(q.types) & block_types):
            details.append(L["where"] + " " + "; ".join(f"{w} x {x}, z {z} ({_n(n, q.lang)})" for w, x, z, n in regions))
        if q.intent in ("when", "where") and newest:
            details.append(L["when"] + "\n" + "\n".join(f"`{fmt_event(e, tz)[:110]}`" for e in newest[:8]))
        parts.append("\n".join(details))
        parts.append(limit_note)
        return "\n\n".join(p for p in parts if p), flt

    # --- what did a player do -------------------------------------------------------------
    if q.players:
        for p in q.players[:3]:
            parts.append(_player_story(store, q, p, tz))
        return "\n\n".join(parts), dict(base, players=q.players)

    if q.suspicious:
        return "\n\n".join(parts), None

    # --- overview of everyone ---------------------------------------------------------------
    who = [(p, c) for p, c in store.grouped("player", 25, **base) if p != "-"]
    parts.append((L["overview"] if who else L["overview_none"]).format(span_cap=span_cap, n=len(who)))
    if who:
        by_type = dict(store.grouped("type", 50, **base))
        parts.append(L["overview_blocks"].format(placed=_n(by_type.get("BLOCK_PLACE", 0), q.lang),
                                                 broken=_n(by_type.get("BLOCK_BREAK", 0), q.lang),
                                                 cmds=_pl(by_type.get("PLAYER_COMMAND", 0), "cmds", q.lang),
                                                 deaths=_pl(by_type.get("PLAYER_DEATH", 0), "deaths", q.lang)))
        lines = [L["per_player"]]
        for p, _ in who:
            pt = dict(store.grouped("type", 50, players=[p], **base))
            evs = store.events(500, False, players=[p], types=list(JOIN_TYPES), **base)
            sess = sessions(evs, q.start, q.end)
            online = sum(((e or min(q.end, int(time.time()))) - j) for j, e in sess)
            bits = [L["online"].format(d=duration_text(online))] if sess else []
            for key, typ in (("placed", "BLOCK_PLACE"), ("broken", "BLOCK_BREAK")):
                if pt.get(typ):
                    bits.append(L[key].format(n=_n(pt[typ], q.lang)))
            for key, typ in (("cmds", "PLAYER_COMMAND"), ("deaths", "PLAYER_DEATH")):
                if pt.get(typ):
                    bits.append(_pl(pt[typ], key, q.lang))
            lines.append("• " + L["pp_line"].format(p=p, parts=" · ".join(bits) or "–"))
        parts.append("\n".join(lines))
    else:
        mobs = store.counted(q.start, q.end, 6)
        if mobs:
            parts.append(L["mobs"].format(list=", ".join(f"{_n(n, q.lang)} {o}" for _, o, n in mobs)))
    alerts = store.alerts(q.start, q.end, None, 5)
    parts.append(_alerts_block(L, alerts, t) if alerts else L["alerts_none"])
    if limit_note:
        parts.append(limit_note)
    return "\n\n".join(parts), None


# ---------------------------------------------------------------------------
# What came in: report of the channel traffic and the lines the bot didn't understand
# ---------------------------------------------------------------------------

RATE_LIMIT_MESSAGES = 28   # Discord lets a webhook post about 30 messages per minute into a channel
UNKNOWN_KINDS = {
    "format": ("Line format not understood", "lines with \"|\" whose first field is no EVENT_TYPE - these are lost"),
    "type": ("Event types the bot has no meaning for yet", "stored and searchable, but /mclog ask has no words "
                                                           "for them and they aren't checked for suspicious things"),
    "no_player": ("No player recognised", "player events without a known player name - only counted, "
                                          "not shown per player"),
    "no_location": ("Coordinates not understood", "\"Location\" in the line, but in an unknown format"),
    "text": ("Text without \"|\"", "lines in the log messages that aren't events"),
    "embed": ("Embed title / footer / field name", "parts of the embeds around the events"),
    "attachment": ("Attachments", "files posted in the log channel"),
}


def size_text(chars: int, lang: str = "en") -> str:
    if chars >= 1_048_576:
        text = f"{chars / 1_048_576:.1f} MB"
        return text.replace(".", ",") if lang == "de" else text
    return f"{max(1, round(chars / 1024)) if chars else 0} KB"


def traffic_problems(st: dict) -> dict:
    """Signs that events got lost before they reached the bot."""
    rows = st["rows"]
    limit = [r for r in rows if r[1] >= RATE_LIMIT_MESSAGES]
    busiest = max(rows, key=lambda r: r[1]) if rows else None
    gaps = []
    for a, b in zip(rows, rows[1:]):
        missing = (b[0] - a[0]) // 60 - 1
        if missing >= 3:
            gaps.append((a[0] + 60, b[0] - 60, missing))
    noisy = [(t, n) for t, n in st["types"]
             if not is_player_type(t) and not t.startswith(("SERVER_", "PLUGIN_", "CONSOLE"))]
    noise = sum(n for _, n in noisy)
    lines = sum(n for _, n in st["types"])
    return {"limit_minutes": len(limit), "busiest": busiest, "gaps": gaps,
            "noise_pct": round(100 * noise / lines) if lines else 0,
            "noise_top": [(t, round(100 * n / lines)) for t, n in sorted(noisy, key=lambda x: -x[1])[:3]]}


SERVER_OFF_MINUTES = 120   # longer pauses are counted as "server off", not as lost messages


def _code(text: str) -> str:
    return "`" + str(text).replace("`", "'") + "`"


def report_text(store: "LogStore", start: int, end: int, tz, unknowns: list[tuple], source: str = "") -> str:
    """Markdown report from the stored statistics (see report_md)."""
    return report_md(store.ingest_stats(start, end), unknowns, start, end, tz, source)


def report_md(st: dict, unknowns: list[tuple], start: int, end: int, tz, source: str = "") -> str:
    """Markdown report for the admin (sent as a .md file): what came in, signs of lost
    events and everything the bot didn't (fully) understand."""
    long = end - start > 86400
    t = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%d.%m. %H:%M")  # noqa: E731
    hm = (lambda ts: t(ts)) if long else (lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M"))  # noqa: E731
    out = ["# ⛏️ Minecraft log report", "",
           f"**Time:** {t(start)} – {t(end)} ({getattr(tz, 'key', tz)})  "]
    if source:
        out.append(f"**Source:** {source}  ")
    out.append("")
    if not st["messages"]:
        out.append("_No log messages came in during this time._")
    else:
        pct = 100 * st["events"] / st["lines"] if st["lines"] else 100
        out += ["## What came in", "", "| | |", "|---|---:|",
                f"| Messages | {st['messages']:,} |",
                f"| Log lines | {st['lines']:,} |",
                f"| Lines per message | avg {st['lines'] / st['messages']:.1f}, max {st['max_lines']} |",
                f"| Understood | {st['events']:,} ({pct:.1f} %) |",
                f"| Not understood | {st['unknown']:,} |",
                f"| Text received | {size_text(st['chars'])} |",
                f"| First / last message | {t(st['first'])} / {t(st['last'])} |", ""]
        pr = traffic_problems(st)
        gaps = [g for g in pr["gaps"] if g[2] < SERVER_OFF_MINUTES]
        off = [g for g in pr["gaps"] if g[2] >= SERVER_OFF_MINUTES]
        out += ["## Possible gaps", "_Why numbers can be lower than what really happened._", ""]
        found = False
        if pr["busiest"]:
            out.append(f"- **Busiest minute:** {hm(pr['busiest'][0])} with {pr['busiest'][1]} messages.")
        if pr["limit_minutes"]:
            found = True
            out += [f"- ⚠️ **In {pr['limit_minutes']:,} minute(s) the channel got {RATE_LIMIT_MESSAGES}+ messages.** "
                    "That's Discord's limit for webhooks (about 30 messages per minute per channel): the plugin "
                    "has to wait or drops events then.",
                    "  - Fix in the plugin: put more lines into one message. One message can carry 10 embeds with "
                    f"4096 characters each (about 250 lines) - right now it's at most {st['max_lines']}."]
        if pr["noise_pct"] >= 50:
            found = True
            top = ", ".join(f"{ty} {pct} %" for ty, pct in pr["noise_top"])
            out.append(f"- ⚠️ **{pr['noise_pct']} % of all lines are mob/entity/world events** ({top}). "
                       "They use up the webhook limit - turning them off in the plugin (or logging them only for "
                       "players) leaves room for the player events.")
        if gaps:
            found = True
            out.append(f"- **Minutes without any message ({len(gaps)}x):** " + ", ".join(
                f"{hm(a)}–{hm(b)} ({n} min)" for a, b, n in sorted(gaps, key=lambda g: -g[2])[:10]))
            out.append("  - Normal if nobody did anything - otherwise messages are missing.")
        if off:
            out.append(f"- **Longer pauses (probably server off):** {len(off)}x, e.g. " + ", ".join(
                f"{t(a)}–{t(b)}" for a, b, _ in sorted(off, key=lambda g: -g[2])[:5]))
        if not found:
            out.append("- ✅ No signs of lost messages on the Discord side.")
        out.append("")
        if st["types"]:
            out += ["## Lines per event type", "", "| Event type | Lines | Bot knows it |", "|---|---:|:---:|"]
            out += [f"| {ty} | {n:,} | {'✅' if known_type(ty) else '❓'} |" for ty, n in st["types"]]
            out.append("")
    out += [f"## Not (fully) understood - {len(unknowns)} different thing(s)", ""]
    if not unknowns:
        out.append("✅ Nothing - every line was understood.")
    for kind in list(UNKNOWN_KINDS) + sorted({u[0] for u in unknowns} - set(UNKNOWN_KINDS)):
        items = [u for u in unknowns if u[0] == kind]
        if not items:
            continue
        title, explain = UNKNOWN_KINDS.get(kind, (kind, ""))
        out += [f"### {title} ({len(items)})", f"_{explain}_", ""]
        for _, key, n, first, last, examples in items:
            examples = [examples] if isinstance(examples, str) else list(examples or [])
            out.append(f"- **{n:,}×** {_code(key)} – first {t(first)}, last {t(last)}")
            for ex in examples:
                if ex != key:
                    out.append(f"  - Example: {_code(ex)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Suspicious activity
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    ts: int
    player: str
    kind: str
    title: str
    detail: str
    world: str | None = None
    x: int | None = None
    y: int | None = None
    z: int | None = None
    raw: str = ""
    suppressed: int = 0


class Detector:
    def __init__(self, config: dict, trusted):
        self.config = config
        self.set_trusted(trusted)
        self.mode: dict[str, str] = {}
        self.pos: dict[str, tuple] = {}            # player -> (world, x, y, z, ts)
        self.breaks: dict[str, deque] = defaultdict(deque)
        self.ores: dict[str, deque] = defaultdict(deque)
        self.last_alert: dict[tuple, int] = {}
        self.suppressed: Counter = Counter()
        self.gamemode_events_seen = False

    def set_trusted(self, trusted) -> None:
        self.trusted_names = {t.lower(): t for t in trusted}
        self.trusted = set(self.trusted_names)

    def _cfg(self, key, default):
        return self.config.get(key, default)

    def _alert(self, ev: Event, kind: str, title: str, detail: str, cooldown: int | None = None) -> Alert | None:
        key = (ev.player.lower(), kind)
        cooldown = cooldown if cooldown is not None else int(self._cfg("alert_cooldown_seconds", 120))
        if ev.ts - self.last_alert.get(key, -10**12) < cooldown:
            self.suppressed[key] += 1
            return None
        self.last_alert[key] = ev.ts
        x, y, z = ev.dest
        alert = Alert(ev.ts, ev.player, kind, title, detail, ev.world, x, y, z, ev.raw, self.suppressed.pop(key, 0))
        return alert

    def feed(self, ev: Event, alert: bool = True) -> list[Alert]:
        out: list[Alert] = []
        p = ev.player
        if p is None:
            return out
        lp = p.lower()
        trusted = lp in self.trusted
        x, y, z = ev.dest
        # positions (block events are within reach of the player, good enough)
        if x is not None:
            self.pos[lp] = (ev.world, x, y, z, ev.ts)
        if ev.type == "PLAYER_QUIT" or ev.type == "PLAYER_KICK":
            self.pos.pop(lp, None)
        if "GAME_MODE" in ev.type or "GAMEMODE" in ev.type:
            self.gamemode_events_seen = True
            if ev.obj in MODES:
                self.mode[lp] = ev.obj
                if alert and not trusted and ev.obj in ("SPECTATOR", "CREATIVE"):
                    out.append(self._alert(ev, "gamemode", f"{p} switched to {ev.obj}",
                                           f"Game mode is now **{ev.obj}**.", cooldown=10))
        if trusted or not alert:
            return [a for a in out if a]
        if ev.type == "PLAYER_COMMAND" and ev.txt:
            name = command_name(ev.txt)
            mode = command_mode(ev.txt)
            if mode and not self.gamemode_events_seen:
                self.mode[lp] = mode  # no game mode events in the log -> trust the command
            if name in VANISH_COMMANDS or "vanish" in name:
                out.append(self._alert(ev, "vanish", f"{p} used vanish", f"`{ev.txt[:150]}`"))
            elif name in {c.lower() for c in self._cfg("suspicious_commands", [])} or name in GAMEMODE_COMMANDS:
                out.append(self._alert(ev, f"command:{name}", f"{p} ran /{name}", f"`{ev.txt[:150]}`"))
        if "VANISH" in ev.type or "INVISIBILITY" in ev.raw.upper():
            out.append(self._alert(ev, "vanish", f"{p} went invisible / vanish", f"`{ev.raw[:150]}`"))
        if ev.type == "BLOCK_BREAK":
            window = int(self._cfg("mass_break_minutes", 5)) * 60
            dq = self.breaks[lp]
            dq.append(ev.ts)
            while dq and dq[0] < ev.ts - window:
                dq.popleft()
            limit = int(self._cfg("mass_break_blocks", 300))
            if len(dq) >= limit:
                out.append(self._alert(ev, "mass_break", f"{p} broke {len(dq)} blocks in "
                                       f"{window // 60} minutes", "Possible griefing or a mining cheat.",
                                       cooldown=900))
            if ev.obj and ev.obj.upper() in {o.upper() for o in self._cfg("ores", [])}:
                owin = int(self._cfg("ore_alert_minutes", 10)) * 60
                oq = self.ores[lp]
                oq.append(ev.ts)
                while oq and oq[0] < ev.ts - owin:
                    oq.popleft()
                if len(oq) >= int(self._cfg("ore_alert_count", 20)):
                    out.append(self._alert(ev, "ores", f"{p} mined {len(oq)} valuable ores in {owin // 60} minutes",
                                           "Possible X-ray.", cooldown=900))
        # close to a trusted player: teleports, or anything while in spectator mode
        is_teleport = "TELEPORT" in ev.type
        spectating = self.mode.get(lp) == "SPECTATOR"
        if x is not None and (is_teleport or spectating):
            radius = float(self._cfg("teleport_alert_radius", 64))
            max_age = float(self._cfg("position_max_age_minutes", 10)) * 60
            for other, (w, ox, oy, oz, ots) in list(self.pos.items()):
                if other not in self.trusted or ev.ts - ots > max_age or (w and ev.world and w != ev.world):
                    continue
                dist = math.dist((x, y or 0, z), (ox, oy or 0, oz))
                if dist <= radius:
                    name = self.trusted_names.get(other, other)
                    how = "teleported" if is_teleport else "is spectating"
                    mode_note = f" (game mode: {self.mode[lp]})" if lp in self.mode else ""
                    out.append(self._alert(ev, f"near:{other}", f"{p} {how} close to {name}",
                                           f"{int(dist)} blocks away from {name}{mode_note}."))
        return [a for a in out if a]
