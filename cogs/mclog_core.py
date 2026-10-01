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

import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

LOC_RE = re.compile(
    r"Location\{world=(?:CraftWorld\{name=)?([^,}]*)\}?,\s*x=(-?[\d.]+(?:E-?\d+)?),\s*y=(-?[\d.]+(?:E-?\d+)?),"
    r"\s*z=(-?[\d.]+(?:E-?\d+)?)[^}]*\}"
)
TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,48}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")
KV_RE = re.compile(r"^[a-z_]+=", re.I)
MODES = ("SPECTATOR", "CREATIVE", "SURVIVAL", "ADVENTURE")
NO_PLAYER_PREFIXES = ("ENTITY_", "SERVER_", "PLUGIN_", "CHUNK_", "WEATHER_", "WORLD_LOAD", "WORLD_UNLOAD",
                      "WORLD_SAVE", "WORLD_INIT", "ITEM_SPAWN", "ITEM_DESPAWN", "CREATURE_", "SPAWNER_",
                      "LIGHTNING_", "STRUCTURE_", "PORTAL_CREATE", "BLOCK_FORM", "BLOCK_SPREAD", "BLOCK_GROW",
                      "BLOCK_FADE", "BLOCK_PHYSICS", "BLOCK_FROM_TO", "LEAVES_DECAY", "BLOCK_BURN", "BLOCK_IGNITE",
                      "REDSTONE_", "TIME_SKIP", "SPONGE_ABSORB")
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


def parse_line(line: str, ts: int, known_players: set[str] | None = None) -> Event | None:
    parts = [p.strip() for p in line.split("|")]
    etype = parts[0].upper().replace(" ", "_")
    if not TYPE_RE.match(etype):
        return None
    ev = Event(ts=ts, type=etype, raw=line[:500])
    locs = LOC_RE.findall(line)
    if locs:
        ev.world = locs[0][0] or None
        ev.x, ev.y, ev.z = (_num(v) for v in locs[0][1:])
        if len(locs) > 1:
            w2 = locs[-1][0] or None
            ev.x2, ev.y2, ev.z2 = (_num(v) for v in locs[-1][1:])
            if w2 and w2 != ev.world:
                ev.world = w2  # teleport into another world: the destination world counts
    rest = parts[1:]
    if not ev.world:
        for p in rest:
            m = re.match(r"^world=([^\s|]+)", p)
            if m:
                ev.world = m.group(1)
                break
    known = known_players or set()
    first = rest[0].split(" @ ")[0].strip() if rest else ""
    player_type = not etype.startswith(NO_PLAYER_PREFIXES)
    if first and NAME_RE.match(first) and not KV_RE.match(first) and (player_type or first in known):
        ev.player = first
        rest = rest[1:]
    values, kvs = [], []
    for p in rest:
        clean = LOC_RE.sub("", p).replace(" @ ", " ").strip(" @->→,;:")
        if clean:
            (kvs if KV_RE.match(clean) else values).append(clean)
    if ev.player is None:
        if first and not KV_RE.match(first) and "Location{" not in first:
            ev.obj = first[:64]
    elif etype.startswith(("BLOCK_", "ENTITY_")) and values and not values[0].startswith("/"):
        ev.obj = values[0].split()[0][:64]
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
"""
COUNT_BUCKET = 600


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
                    if ev.type in self.count_only and ev.player is None:
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

    def counted(self, start=None, end=None, limit: int = 10) -> list[tuple[str, str, int]]:
        """Top counted (not stored) events: [(type, obj, n)]."""
        with self._lock:
            rows = self.db.execute(
                "SELECT type, obj, SUM(n) FROM counts WHERE bucket >= ? AND bucket < ? GROUP BY type, obj "
                "ORDER BY 3 DESC LIMIT ?", (int(start or 0), int(end or 2**40), limit)).fetchall()
        return [(self.name(t), self.name(o), n) for t, o, n in rows]

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
    (("abgebaut", "abbau", "zerstört", "zerstoert", "kaputt", "break", "broke", "broken", "mined", "mining",
      "abgebaut", "grief"), ["BLOCK_BREAK"]),
    (("platziert", "gebaut", "gesetzt", "baut", "place", "placed", "built", "build", "grief"), ["BLOCK_PLACE"]),
    (("befehl", "command", "cmd", "eingegeben"), ["PLAYER_COMMAND"]),
    (("gestorben", "tod", "tode", "death", "died", "starb", "stirbt"), ["PLAYER_DEATH"]),
    (("teleport", "tp"), ["PLAYER_TELEPORT"]),
    (("online", "gejoint", "beigetreten", "verlassen", "join", "joined", "left", "quit", "spielzeit", "playtime",
      "eingeloggt", "ausgeloggt"), ["PLAYER_JOIN", "PLAYER_QUIT", "PLAYER_KICK"]),
    (("welt", "nether", "dimension", "world"), ["WORLD_CHANGE"]),
    (("gekickt", "kick"), ["PLAYER_KICK"]),
    (("spielmodus", "gamemode", "spectator", "creative", "zuschauer"), ["PLAYER_GAME_MODE_CHANGE", "GAMEMODE_CHANGE"]),
]
MATERIAL_WORDS = {"diamant": "DIAMOND", "diamanten": "DIAMOND", "diamond": "DIAMOND", "eisen": "IRON",
                  "iron": "IRON", "gold": "GOLD", "erz": "_ORE", "erze": "_ORE", "ore": "_ORE", "ores": "_ORE",
                  "holz": "_LOG", "stamm": "_LOG", "bretter": "PLANKS", "stein": "STONE", "tnt": "TNT",
                  "lava": "LAVA", "wasser": "WATER", "truhe": "CHEST", "kiste": "CHEST", "chest": "CHEST",
                  "netherit": "ANCIENT_DEBRIS", "netherite": "ANCIENT_DEBRIS", "kohle": "COAL", "coal": "COAL",
                  "smaragd": "EMERALD", "emerald": "EMERALD", "redstone": "REDSTONE", "lapis": "LAPIS",
                  "kupfer": "COPPER", "copper": "COPPER", "glas": "GLASS", "glass": "GLASS", "erde": "DIRT",
                  "dirt": "DIRT", "obsidian": "OBSIDIAN", "spawner": "SPAWNER", "shulker": "SHULKER",
                  "bett": "_BED", "feuer": "FIRE", "fire": "FIRE"}
SUSPICIOUS_WORDS = ("verdächtig", "verdaechtig", "suspicious", "auffällig", "auffaellig", "cheat", "hack", "xray",
                    "x-ray", "vanish")
SPAWN_WORDS = ("mobs", "mob", "spawn", "gespawnt", "spawns", "monster")


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
    time_text: str = ""


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


def parse_question(text: str, now: datetime, known_players: set[str], retention_days: float = 4) -> Question:
    q = Question()
    low = text.lower()
    # players: known names, also with @ in front
    for name in sorted(known_players, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name.lower())}(?![A-Za-z0-9_])", low):
            q.players.append(name)
    for words, types in ACTIONS:
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            q.types += [t for t in types if t not in q.types]
    for word, material in MATERIAL_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", low) and material not in q.obj_words:
            q.obj_words.append(material)
    q.suspicious = any(w in low for w in SUSPICIOUS_WORDS)
    q.spawns = any(re.search(rf"\b{w}\b", low) for w in SPAWN_WORDS)
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
    except ValueError:
        pass
    oldest = now - timedelta(days=retention_days)
    q.start, q.end = int(max(start, oldest).timestamp()), int(min(end, now).timestamp())
    return q


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
