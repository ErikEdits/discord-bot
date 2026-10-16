"""Weekly server report: every Sunday (config: weekday/hour) the owner gets a DM.

    Members     now / joined / left, top inviters
    Activity    messages (vs. the week before), busiest day, top channels, most active
                members, voice time
    Mods        downloads per mod this week (Modrinth + CurseForge), followers
    Moderation  warnings, timeouts, kicks, bans, reports
    Support     tickets opened/closed, ratings, crash analyses, problems many people had
    Minecraft   most active players on the server (from the MC logs)
    Bot         uptime

Only numbers the bot already collects are used. Weekly differences (messages per
member, voice time, downloads, invites) come from a snapshot taken at each report
in data/weekly_report.json, so the first report after installing shows "from next
week" for those.

    /weekly-report preview   send the report to yourself now (administrator)
Config: SERVER_TEMPLATE["weekly_report"].
"""

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.weekly_report")

STATE_FILE = DATA_DIR / "weekly_report.json"
STARTED = time.time()
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _config() -> dict:
    return SERVER_TEMPLATE.get("weekly_report", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_config().get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def _delta(now: int, before: int | None) -> str:
    if before is None:
        return ""
    d = now - before
    return f" ({'+' if d >= 0 else ''}{d:,})"


def _pct(now: int, before: int) -> str:
    if not before:
        return ""
    p = (now - before) * 100 / before
    return f" ({'+' if p >= 0 else ''}{p:.0f}% vs. last week)"


def _ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _lines(items: list[str], empty: str = "-") -> str:
    text = "\n".join(items) or empty
    return text if len(text) <= 1024 else text[:1020] + "..."


class WeeklyReport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # -------- Collecting ------------------------------------------------------

    @staticmethod
    def _week_days(end: datetime) -> tuple[list[str], list[str]]:
        """ISO dates of the 7 days up to `end` (local) and of the 7 days before."""
        today = end.date()
        this = [(today - timedelta(days=i)).isoformat() for i in range(7)]
        last = [(today - timedelta(days=i)).isoformat() for i in range(7, 14)]
        return this, last

    async def build(self, guild: discord.Guild, state: dict, snapshot_out: dict) -> list[discord.Embed]:
        now_local = datetime.now(_tz())
        start_ts = (now_local - timedelta(days=7)).timestamp()
        this_days, last_days = self._week_days(now_local)
        gs = load_json(DATA_DIR / "stats.json").get("guilds", {}).get(str(guild.id), {})
        days = gs.get("days", {})
        week = [days.get(d, {}) for d in this_days]
        prev = [days.get(d, {}) for d in last_days]
        snap = state.get("snapshots", {}).get(str(guild.id), {})
        first = not snap

        def total(rows, key):
            return sum(int(r.get(key) or 0) for r in rows)

        title_range = f"{(now_local - timedelta(days=6)).strftime('%d.%m.')} - {now_local.strftime('%d.%m.%Y')}"
        main = discord.Embed(title=f"\U0001F4CA Weekly report - {guild.name}", description=title_range, color=0x5865F2,
                             timestamp=datetime.now(timezone.utc))

        # Members
        members_then = (prev[0].get("members") if prev else None) or \
            next((d.get("members") for d in reversed(week) if d.get("members")), None)
        joins, leaves = total(week, "joins"), total(week, "leaves")
        member_lines = [f"Members now: **{guild.member_count:,}**" + (_delta(guild.member_count, members_then)
                                                                       if members_then else ""),
                        f"Joined: **{joins}** - Left: **{leaves}** - Net: **{joins - leaves:+d}**"]
        inv = load_json(DATA_DIR / "invites.json").get(str(guild.id), {})
        inviters = Counter(info.get("inviter") for info in inv.get("members", {}).values()
                           if info.get("inviter") and (info.get("ts") or 0) >= start_ts)
        if inviters:
            member_lines.append("Top inviters: " + ", ".join(f"<@{uid}> ({n})" for uid, n in inviters.most_common(3)))
        main.add_field(name="\U0001F465 Members", value=_lines(member_lines), inline=False)

        # Activity
        msgs, msgs_prev = total(week, "messages"), total(prev, "messages")
        busiest = max(zip(this_days, week), key=lambda x: x[1].get("messages", 0), default=None)
        act = [f"Messages: **{msgs:,}**{_pct(msgs, msgs_prev)}"]
        if busiest and busiest[1].get("messages"):
            day = datetime.fromisoformat(busiest[0])
            act.append(f"Busiest day: **{WEEKDAYS[day.weekday()]}** ({busiest[1]['messages']:,} messages)")
        channels = Counter()
        for d in week:
            channels.update({cid: n for cid, n in (d.get("channels") or {}).items()})
        names = gs.get("channel_names", {})
        if channels:
            act.append("Top channels: " + ", ".join(
                f"<#{cid}> ({n:,})" if guild.get_channel(int(cid)) else f"#{names.get(cid, cid)} ({n:,})"
                for cid, n in channels.most_common(3)))
        levels = load_json(DATA_DIR / "levels.json").get(str(guild.id), {})
        now_levels = {uid: [int(e.get("messages", 0)), int(e.get("voice_minutes", 0))] for uid, e in levels.items()}
        snapshot_out["levels"] = now_levels
        if not first:
            old = snap.get("levels", {})
            talk = Counter({uid: v[0] - old.get(uid, [0, 0])[0] for uid, v in now_levels.items()})
            voice = Counter({uid: v[1] - old.get(uid, [0, 0])[1] for uid, v in now_levels.items()})
            talk, voice = +talk, +voice
            if talk:
                act.append("Most active: " + ", ".join(f"<@{u}> ({n:,})" for u, n in talk.most_common(5)))
            vtotal = sum(voice.values())
            if vtotal:
                act.append(f"Voice: **{vtotal // 60} h {vtotal % 60} min** - top: "
                           + ", ".join(f"<@{u}> ({n // 60} h {n % 60} min)" for u, n in voice.most_common(3)))
        else:
            act.append("_Most active members and voice time: from next week on (first report)._")
        main.add_field(name="\U0001F4AC Activity", value=_lines(act), inline=False)

        # Mods
        mods_lines = []
        try:
            from cogs.mod_stats import _fetch_projects
            projects = await _fetch_projects() or []
        except Exception:
            projects = []
        if projects:
            old = snap.get("mods", {})
            now_mods = {}
            rows, week_total, all_total = [], 0, 0
            for p in sorted(projects, key=lambda p: -(int(p.get("downloads", 0)) + int(p.get("cf_downloads", 0)))):
                dl = int(p.get("downloads", 0)) + int(p.get("cf_downloads", 0))
                now_mods[p["id"]] = {"downloads": dl, "followers": int(p.get("followers", 0))}
                all_total += dl
                before = old.get(p["id"])
                gained = dl - before["downloads"] if before else None
                if gained is not None:
                    week_total += gained
                rows.append(f"**{p.get('title')}**: {dl:,}" + (f" (+{gained:,})" if gained else "")
                            + (f" - {int(p.get('followers', 0)) - before['followers']:+d} followers"
                               if before and int(p.get("followers", 0)) != before["followers"] else ""))
            snapshot_out["mods"] = now_mods
            mods_lines.append(f"Downloads total: **{all_total:,}**" + (f" - this week **+{week_total:,}**" if old else ""))
            mods_lines += rows[:10]
            if not old:
                mods_lines.append("_Downloads per week: from next week on (first report)._")
        else:
            mods_lines.append("Modrinth not reachable right now.")
        main.add_field(name="\U0001F4E6 Mods (Modrinth + CurseForge)", value=_lines(mods_lines), inline=False)

        second = discord.Embed(color=0x5865F2)
        # Moderation
        warns = sum(1 for w in load_json(DATA_DIR / "warnings.json").get(str(guild.id), {}).values()
                    for e in w if (_ts(e.get("timestamp")) or 0) >= start_ts)
        actions = Counter(e.get("action") for entries in load_json(DATA_DIR / "modlog.json").get(str(guild.id), {}).values()
                          for e in entries if (_ts(e.get("timestamp")) or 0) >= start_ts)
        mod_lines = [f"Warnings: **{warns}** - Timeouts: **{actions.get('timeout', 0)}** - Kicks: "
                     f"**{actions.get('kick', 0)}** - Bans: **{actions.get('ban', 0) + actions.get('tempban', 0)}**",
                     f"Reported messages: **{total(week, 'reports')}**"]
        second.add_field(name="\U0001F6E1 Moderation", value=_lines(mod_lines), inline=False)

        # Support
        ratings = [r["stars"] for r in gs.get("ratings", []) if r.get("ts", 0) >= start_ts]
        sup = [f"Tickets: **{total(week, 'tickets_opened')}** opened, **{total(week, 'tickets_closed')}** closed"
               + (f" - rating **{sum(ratings) / len(ratings):.1f}/5** ({len(ratings)}x)" if ratings else ""),
               f"Crash analyses: **{total(week, 'crash_analyses')}**"]
        problems = load_json(DATA_DIR / "crash_stats.json").get("problems", {})
        common = sorted(((len({e[1] for e in p["events"] if e[0] >= start_ts}), p.get("label", k))
                         for k, p in problems.items()), reverse=True)
        common = [(n, label) for n, label in common if n >= 2][:3]
        if common:
            sup.append("Common crash problems: " + "; ".join(f"{label} ({n} people)" for n, label in common))
        second.add_field(name="\U0001F3AB Support", value=_lines(sup), inline=False)

        # Minecraft server
        mc = self.bot.get_cog("McLogs")
        store = getattr(mc, "store", None)
        if store is not None:
            try:
                keep = float(SERVER_TEMPLATE.get("mc_logs", {}).get("retention_days", 4))
                since = int(max(start_ts, time.time() - keep * 86400))
                players = await asyncio.to_thread(store.grouped, "player", limit=5, start=since)
                events = await asyncio.to_thread(store.count, start=since)
                alerts = len(await asyncio.to_thread(store.alerts, start=since, limit=1000))
                mc_lines = [f"Logged actions in the last {min(7, keep):g} days: **{events:,}**"
                            + (f" - alerts: **{alerts}**" if alerts else "")
                            + (f" (the logs are kept {keep:g} days)" if keep < 7 else "")]
                if players:
                    mc_lines.append("Most active players: " + ", ".join(f"{p} ({n:,})" for p, n in players))
                second.add_field(name="⛏ Minecraft server", value=_lines(mc_lines), inline=False)
            except Exception:
                log.exception("MC part of the weekly report failed")

        # Bot
        up = int(time.time() - STARTED)
        second.add_field(name="\U0001F916 Bot", value=f"Running for **{up // 86400} d {up % 86400 // 3600} h** - "
                                                     f"{len(self.bot.cogs)} modules loaded", inline=False)
        if first:
            second.set_footer(text="First report - weekly differences start with the next one.")
        return [main, second]

    # -------- Sending ---------------------------------------------------------

    def _recipients(self, guild: discord.Guild) -> list[discord.Member]:
        people = {guild.owner} if guild.owner else set()
        wanted = {n.lower() for n in _config().get("send_to", [])}
        people |= {m for m in guild.members if m.name.lower() in wanted}
        return [p for p in people if p is not None]

    async def send_reports(self, only_to: discord.abc.User | None = None, save: bool = True) -> int:
        state = load_json(STATE_FILE)
        sent = 0
        for guild in self.bot.guilds:
            snapshot = {}
            try:
                embeds = await self.build(guild, state, snapshot)
            except Exception:
                log.exception("Building the weekly report for %s failed", guild.name)
                continue
            for person in ([only_to] if only_to else self._recipients(guild)):
                try:
                    await person.send(embeds=embeds)
                    sent += 1
                except discord.HTTPException:
                    log.warning("Couldn't DM the weekly report to %s", person)
            if save:
                state.setdefault("snapshots", {})[str(guild.id)] = snapshot
        if save:
            state["last_sent"] = datetime.now(_tz()).date().isoformat()
            save_json(STATE_FILE, state)
        return sent

    @tasks.loop(minutes=10)
    async def loop(self):
        if not _config().get("enabled", True):
            return
        now = datetime.now(_tz())
        state = load_json(STATE_FILE)
        if not state.get("snapshots"):
            # Take the first snapshot right away, so the first report already has weekly numbers.
            for guild in self.bot.guilds:
                snapshot = {}
                try:
                    await self.build(guild, state, snapshot)
                except Exception:
                    continue
                state.setdefault("snapshots", {})[str(guild.id)] = snapshot
            if state.get("snapshots"):
                state["snapshot_taken"] = now.date().isoformat()
                save_json(STATE_FILE, state)
            return
        if now.weekday() != int(_config().get("weekday", 6)) or now.hour < int(_config().get("hour", 18)):
            return
        if state.get("last_sent") == now.date().isoformat():
            return
        sent = await self.send_reports()
        log.info("Weekly report sent to %d people", sent)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    group = app_commands.Group(name="weekly-report", description="The weekly server report (administrator).",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @group.command(name="preview", description="Send the weekly report to yourself now (doesn't change next week's).")
    async def preview(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = await self.send_reports(only_to=interaction.user, save=False)
        await interaction.followup.send("\U0001F4E8 Sent you the report by DM." if sent
                                        else "Couldn't DM you - are your DMs open?", ephemeral=True)


async def setup(bot):
    await bot.add_cog(WeeklyReport(bot))
