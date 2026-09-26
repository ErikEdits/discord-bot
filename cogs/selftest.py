"""/selftest - end-to-end self test of the whole bot (administrator).

What it checks:
  1. Bot         logged in, latency, intents, every extension loaded, slash
                 commands synced with Discord, background loops running
  2. Server      every template role, category and channel exists, the bot has
                 the permissions it needs (server-wide and per channel), its
                 role is above all roles it hands out, panels are posted
  3. Discord     real actions in a hidden test category: send/edit/react,
                 embed with image, buttons, thread, pin, slowmode, permission
                 overwrites, temporary role, webhook, voice channel, bulk
                 delete, audit log, invites, scheduled-post and roadmap output
  4. Features    the logic of each feature with test data (crash analyzer,
                 link filter, time parsing, levels, roadmap, FAQ matching,
                 auto slowmode, counting, backups, images, embed builder, ...)
  5. Storage     data folder writable, every data file readable, free disk space
  6. Services    Modrinth, GitHub (auto-update), configured webhooks, web panel,
                 panel launcher (relay channel, webhook, a relayed panel request)

Everything the test creates is prefixed "bot-selftest" and deleted at the end
(also leftovers from an interrupted earlier run). Nothing is posted in real
channels and nobody is pinged. If something fails, the full log file is sent to
you by DM (or attached to the reply if your DMs are closed).
"""

import asyncio
import io
import json
import logging
import os
import platform
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import (DATA_DIR, SELFTEST_PREFIX, get_setting, is_admin, is_selftest_name, post_to_webhook,
                         webhook_url)
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.selftest")

PREFIX = SELFTEST_PREFIX
CHECK_TIMEOUT = 45
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
ICON = {PASS: "✅", WARN: "⚠️", FAIL: "❌", SKIP: "⏭️"}

# Server-wide permissions the features rely on (Administrator covers all of them).
NEEDED_PERMISSIONS = [
    "view_channel", "send_messages", "embed_links", "attach_files", "add_reactions", "read_message_history",
    "manage_channels", "manage_roles", "manage_messages", "manage_webhooks", "manage_threads",
    "create_public_threads", "send_messages_in_threads", "manage_guild", "view_audit_log",
    "moderate_members", "kick_members", "ban_members", "move_members", "connect", "use_external_emojis",
]


class Result:
    def __init__(self, group: str, name: str):
        self.group, self.name = group, name
        self.status, self.detail, self.trace, self.seconds = PASS, "", "", 0.0


class Skip(Exception):
    """Raised by a check that doesn't apply (feature off, not configured)."""


class Warning_(Exception):
    """Raised by a check for a problem that isn't fatal."""


class SelfTestRun:
    def __init__(self, bot, guild: discord.Guild, user):
        self.bot, self.guild, self.user = bot, guild, user
        self.results: list[Result] = []
        self.started = time.time()
        self.ctx: dict = {}  # objects created by the Discord tests, for later checks + cleanup

    async def check(self, group: str, name: str, func) -> Result:
        r = Result(group, name)
        t0 = time.monotonic()
        try:
            detail = await asyncio.wait_for(func(), timeout=CHECK_TIMEOUT)
            r.detail = str(detail or "")
        except Skip as e:
            r.status, r.detail = SKIP, str(e)
        except Warning_ as e:
            r.status, r.detail = WARN, str(e)
        except asyncio.TimeoutError:
            r.status, r.detail = FAIL, f"timed out after {CHECK_TIMEOUT}s"
        except Exception as e:
            r.status, r.detail = FAIL, f"{type(e).__name__}: {e}"
            r.trace = traceback.format_exc()
            log.warning("Self-test failed: %s / %s: %s", group, name, r.detail)
        r.seconds = time.monotonic() - t0
        self.results.append(r)
        return r

    def counts(self) -> dict:
        out = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for r in self.results:
            out[r.status] += 1
        return out


# ---------------------------------------------------------------------------
# 1. Bot
# ---------------------------------------------------------------------------

async def bot_checks(run: SelfTestRun):
    bot, g = run.bot, "Bot"
    main = sys.modules.get("__main__")

    async def logged_in():
        if not bot.is_ready() or bot.user is None:
            raise RuntimeError("bot is not ready")
        return f"{bot.user} in {len(bot.guilds)} server(s)"

    async def latency():
        ms = bot.latency * 1000
        if ms > 1500:
            raise Warning_(f"high gateway latency: {ms:.0f} ms")
        return f"{ms:.0f} ms"

    async def intents():
        missing = [n for n in ("members", "message_content") if not getattr(bot.intents, n)]
        if missing:
            raise RuntimeError("intents off: " + ", ".join(missing) + " (enable in the Developer Portal + .env)")
        return "members + message content on"

    async def extensions():
        expected = list(getattr(main, "EXTENSIONS", []) or [])
        failed = list(getattr(main, "FAILED_EXTENSIONS", []) or [])
        missing = [e for e in expected if e not in bot.extensions]
        if failed or missing:
            raise RuntimeError("not loaded: " + ", ".join(sorted(set(failed + missing))))
        return f"{len(bot.extensions)} loaded"

    async def commands_synced():
        local = {c.name for c in bot.tree.get_commands()}
        remote = {c.name for c in await bot.tree.fetch_commands(guild=run.guild)}
        missing, extra = local - remote, remote - local
        if missing:
            raise RuntimeError("not registered on Discord: " + ", ".join(sorted(missing)) + " (restart the bot)")
        if extra:
            raise Warning_("registered but not in the bot anymore: " + ", ".join(sorted(extra)))
        return f"{len(local)} commands registered"

    async def loops():
        stopped = []
        total = 0
        for cog_name, cog in bot.cogs.items():
            for attr in dir(type(cog)):
                loop = getattr(cog, attr, None)
                if isinstance(loop, tasks.Loop):
                    total += 1
                    if not loop.is_running():
                        stopped.append(f"{cog_name}.{attr}")
        if stopped:
            raise Warning_("not running (may be switched off in the config): " + ", ".join(stopped))
        return f"{total} background loops running"

    for name, func in (("Logged in", logged_in), ("Gateway latency", latency), ("Intents", intents),
                       ("Extensions loaded", extensions), ("Slash commands synced", commands_synced),
                       ("Background loops", loops)):
        await run.check(g, name, func)


# ---------------------------------------------------------------------------
# 2. Server setup
# ---------------------------------------------------------------------------

def _handed_out_roles() -> set[str]:
    names = {"Member", "Muted", SERVER_TEMPLATE.get("beta", {}).get("role", "Beta Tester")}
    names |= {str(v) for v in (SERVER_TEMPLATE.get("levels", {}).get("level_roles") or {}).values()}
    try:
        from cogs.role_panel import get_panel_config
        names |= {b["role"] for b in get_panel_config().get("buttons", [])}
    except Exception:
        pass
    return names


async def server_checks(run: SelfTestRun):
    guild, me, g = run.guild, run.guild.me, "Server"

    async def roles():
        missing = [r["name"] for r in SERVER_TEMPLATE["roles"] if not discord.utils.get(guild.roles, name=r["name"])]
        if missing:
            raise RuntimeError("missing roles: " + ", ".join(missing) + " (run /update)")
        return f"{len(SERVER_TEMPLATE['roles'])} roles"

    async def channels():
        missing = []
        for cat in SERVER_TEMPLATE["categories"]:
            if not discord.utils.get(guild.categories, name=cat["name"]):
                missing.append(f"category {cat['name']}")
            for ch in cat.get("channels", []):
                if not discord.utils.get(guild.channels, name=ch["name"]):
                    missing.append(ch["name"])
        if missing:
            raise RuntimeError("missing: " + ", ".join(missing) + " (run /update)")
        return "all categories and channels exist"

    async def permissions():
        perms = me.guild_permissions
        if perms.administrator:
            return "Administrator"
        missing = [p for p in NEEDED_PERMISSIONS if not getattr(perms, p, False)]
        if missing:
            raise RuntimeError("missing: " + ", ".join(missing))
        return "all needed permissions"

    async def channel_access():
        problems = []
        for key, name in CHANNELS.items():
            ch = discord.utils.get(guild.channels, name=name)
            if ch is None:
                continue
            p = ch.permissions_for(me)
            need = ["view_channel", "connect"] if isinstance(ch, discord.VoiceChannel) else \
                ["view_channel", "send_messages", "embed_links", "read_message_history"]
            if isinstance(ch, discord.ForumChannel):
                need = ["view_channel", "send_messages"]
            lacking = [n for n in need if not getattr(p, n, False)]
            if lacking:
                problems.append(f"#{name}: {', '.join(lacking)}")
        if problems:
            raise RuntimeError("; ".join(problems))
        return "bot can use every channel"

    async def hierarchy():
        top = me.top_role
        too_high = sorted(n for n in _handed_out_roles()
                          if (r := discord.utils.get(guild.roles, name=n)) is not None and r >= top)
        if too_high:
            raise RuntimeError("drag the bot's role above: " + ", ".join(too_high))
        return f"bot role '{top.name}' is above all roles it hands out"

    async def panels():
        keys = ["rules", "welcome", "roles", "tickets", "mod_downloads", "crash_analyzer", "beta_program"]
        missing = []
        for key in keys:
            ch = discord.utils.get(guild.text_channels, name=CHANNELS.get(key, ""))
            if ch is None:
                continue
            found = False
            async for msg in ch.history(limit=30):
                if msg.author.id == run.bot.user.id:
                    found = True
                    break
            if not found:
                missing.append(ch.name)
        if missing:
            raise Warning_("no panel/message from the bot in: " + ", ".join(missing) + " (run /update)")
        return "all panels posted"

    for name, func in (("Roles exist", roles), ("Channels exist", channels), ("Server permissions", permissions),
                       ("Channel access", channel_access), ("Role hierarchy", hierarchy), ("Panels posted", panels)):
        await run.check(g, name, func)


# ---------------------------------------------------------------------------
# 3. Real Discord actions in a hidden test category
# ---------------------------------------------------------------------------

async def cleanup_leftovers(guild: discord.Guild) -> int:
    """Delete everything a previous (interrupted) self-test left behind."""
    removed = 0
    for ch in list(guild.channels):
        if is_selftest_name(ch.name) and not isinstance(ch, discord.CategoryChannel):
            try:
                await ch.delete(reason="Self-test cleanup")
                removed += 1
            except discord.HTTPException:
                pass
    for cat in list(guild.categories):
        if is_selftest_name(cat.name):
            try:
                await cat.delete(reason="Self-test cleanup")
                removed += 1
            except discord.HTTPException:
                pass
    for role in list(guild.roles):
        if is_selftest_name(role.name):
            try:
                await role.delete(reason="Self-test cleanup")
                removed += 1
            except discord.HTTPException:
                pass
    return removed


async def discord_checks(run: SelfTestRun):
    guild, me, g, ctx = run.guild, run.guild.me, "Discord actions", run.ctx

    def need(key):
        if key not in ctx:
            raise Skip("skipped - an earlier step failed")
        return ctx[key]

    async def create_category():
        ctx["category"] = await guild.create_category(PREFIX, overwrites={
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                            manage_messages=True, read_message_history=True, connect=True),
        }, reason="Self-test")
        return "hidden category created"

    async def create_channel():
        ctx["channel"] = await guild.create_text_channel(PREFIX, category=need("category"), reason="Self-test")
        return f"#{ctx['channel'].name}"

    async def send_edit_react():
        ch = need("channel")
        msg = await ch.send("Self-test message")
        await msg.edit(content="Self-test message (edited)")
        fetched = await ch.fetch_message(msg.id)
        if fetched.content != "Self-test message (edited)":
            raise RuntimeError("edit didn't stick")
        await msg.add_reaction("✅")
        await msg.remove_reaction("✅", me)
        ctx["message"] = msg
        return "send, edit, fetch, react"

    async def embed_with_image():
        from cogs.welcome_image import render_card
        png = await asyncio.to_thread(render_card, None, "Self-test", "selftest", guild.name, guild.member_count or 1)
        embed = discord.Embed(title="Self-test", description="Embed with an attached image")
        embed.set_image(url="attachment://selftest.png")
        await need("channel").send(embed=embed, file=discord.File(io.BytesIO(png), filename="selftest.png"))
        return f"{len(png) // 1024} KB image rendered and uploaded"

    async def buttons():
        view = discord.ui.View(timeout=1)
        view.add_item(discord.ui.Button(label="Self-test", custom_id=f"{PREFIX}:noop"))
        view.add_item(discord.ui.Button(label="Link", url="https://discord.com"))
        await need("channel").send("Buttons", view=view)
        return "message with buttons"

    async def thread():
        th = await need("message").create_thread(name=f"{PREFIX}-thread")
        await th.send("Thread message")
        await th.delete()
        return "created, posted, deleted"

    async def pin():
        msg = need("message")
        await msg.pin(reason="Self-test")
        await msg.unpin(reason="Self-test")
        return "pinned and unpinned"

    async def slowmode():
        ch = need("channel")
        await ch.edit(slowmode_delay=5)
        await ch.edit(slowmode_delay=0)
        return "set and reset (auto slowmode)"

    async def role():
        r = await guild.create_role(name=f"{PREFIX}-role", permissions=discord.Permissions.none(), reason="Self-test")
        ctx["role"] = r
        await me.add_roles(r, reason="Self-test")
        await me.remove_roles(r, reason="Self-test")
        await need("channel").set_permissions(r, view_channel=True, reason="Self-test")
        return "created, given, removed, used in overwrites"

    async def webhook():
        hook = await need("channel").create_webhook(name=PREFIX, reason="Self-test")
        try:
            if not await post_to_webhook(hook.url, content="Self-test via webhook", username=PREFIX):
                raise RuntimeError("posting through the webhook failed")
        finally:
            await hook.delete(reason="Self-test")
        return "created, posted, deleted (bug/backup/roadmap webhooks)"

    async def voice():
        vc = await guild.create_voice_channel(f"{PREFIX}-voice", category=need("category"), reason="Self-test")
        await vc.edit(name=f"{PREFIX}-voice-renamed", user_limit=2)
        await vc.delete(reason="Self-test")
        return "created, renamed, deleted (join-to-create)"

    async def scheduled_post():
        from cogs.scheduler import send_item
        item = {"id": "selftest", "channel_id": need("channel").id, "title": "Self-test announcement",
                "message": "Scheduled post output", "ping": "none"}
        if not await send_item(run.bot, item):
            raise RuntimeError("send_item returned False")
        return "scheduled announcement posted"

    async def roadmap_render():
        from cogs.roadmap import apply_command, build_embed
        data = {"guilds": {}}
        ok, text, mod = apply_command(data, guild.id, "roadmap add Selftest | planned | Item")
        if not ok:
            raise RuntimeError(text)
        await need("channel").send(embed=build_embed(mod, data["guilds"][str(guild.id)]["mods"][mod]))
        return "roadmap embed posted"

    async def purge():
        deleted = await need("channel").purge(limit=100, reason="Self-test")
        if not deleted:
            raise RuntimeError("nothing deleted")
        return f"{len(deleted)} messages bulk-deleted (/purge)"

    async def audit_log():
        entries = [e async for e in guild.audit_logs(limit=3)]
        return f"readable ({len(entries)} entries)"

    async def invites():
        inv = await guild.invites()
        return f"readable ({len(inv)} invites) - invite tracking works"

    steps = [
        ("Create hidden category", create_category), ("Create text channel", create_channel),
        ("Send, edit, react", send_edit_react), ("Embed with image", embed_with_image), ("Buttons", buttons),
        ("Thread", thread), ("Pin", pin), ("Slowmode", slowmode), ("Temporary role", role),
        ("Webhook", webhook), ("Voice channel", voice), ("Scheduled post output", scheduled_post),
        ("Roadmap output", roadmap_render), ("Bulk delete", purge), ("Audit log", audit_log), ("Invites", invites),
    ]
    for name, func in steps:
        await run.check(g, name, func)


async def cleanup(run: SelfTestRun):
    async def do_cleanup():
        removed = await cleanup_leftovers(run.guild)
        leftovers = [c.name for c in run.guild.channels if is_selftest_name(c.name)] + \
                    [r.name for r in run.guild.roles if is_selftest_name(r.name)]
        if leftovers:
            raise RuntimeError("could not delete: " + ", ".join(leftovers))
        return f"{removed} test object(s) removed"
    await run.check("Cleanup", "Remove test channels and roles", do_cleanup)


# ---------------------------------------------------------------------------
# 4. Feature logic (no side effects)
# ---------------------------------------------------------------------------

async def feature_checks(run: SelfTestRun):
    g = "Features"

    def expect(cond, text):
        if not cond:
            raise RuntimeError(text)

    async def crash_analyzer():
        from crashlog_analyzer import analyze
        r = analyze("Loading Minecraft 1.21.1 with Fabric Loader 0.16.5\n"
                    "\t - Mod 'X' (x) 1.0 requires any version of fabric-api, which is missing!\n")
        expect(any(f.title == "Missing dependency" for f in r["findings"]), "missing dependency not detected")
        expect(r["info"].get("loader") == "Fabric", "loader not detected")
        r = analyze("java.lang.OutOfMemoryError: Java heap space")
        expect(any(f.title == "Out of memory" for f in r["findings"]), "out of memory not detected")
        return "detects missing deps, loader, out of memory"

    async def link_filter():
        from cogs.linkfilter import INVITE_RE, scam_reason
        allowed = SERVER_TEMPLATE.get("link_filter", {}).get("allowed_domains", [])
        expect(scam_reason("dlscord-nitro.ru", "", allowed, []) is not None, "fake discord link not caught")
        expect(scam_reason("modrinth.com", "", allowed, []) is None, "modrinth.com wrongly blocked")
        expect(scam_reason("github.com", "", allowed, []) is None, "github.com wrongly blocked")
        expect(INVITE_RE.search("join discord.gg/abc") is not None, "invite not detected")
        return "scam links caught, real sites allowed, invites detected"

    async def time_parsing():
        from cogs.common import parse_duration
        from cogs.scheduler import parse_when
        expect(parse_duration("1h30m").total_seconds() == 5400, "1h30m wrong")
        expect(parse_duration("abc") is None, "invalid duration accepted")
        expect(parse_when("2h") is not None and parse_when("18:00") is not None, "time formats not parsed")
        return "durations and dates"

    async def levels():
        from cogs.levels import level_from_xp, total_xp_for_level
        expect(all(level_from_xp(total_xp_for_level(n)) == n for n in (1, 5, 10, 50)), "level math wrong")
        return "level curve"

    async def roadmap():
        from cogs.roadmap import apply_command
        data = {"guilds": {}}
        expect(apply_command(data, 1, "roadmap add M | planned | A")[0], "add failed")
        expect(apply_command(data, 1, "roadmap move M | #1 | done")[0], "move failed")
        expect(not apply_command(data, 1, "roadmap add M | nope | B")[0], "bad column accepted")
        return "add, move, validation"

    async def faq():
        from cogs.community import match_faq
        faqs = {"install": {"keywords": ["how to install"]}}
        expect(match_faq("How to install this?", faqs) == "install", "FAQ not matched")
        expect(match_faq("nice mod", faqs) is None, "FAQ matched without a question")
        return "questions matched"

    async def slowmode_levels():
        from cogs.auto_slowmode import target_slowmode
        lv = SERVER_TEMPLATE.get("auto_slowmode", {}).get("levels", [])
        if not lv:
            raise Skip("no levels configured")
        expect(target_slowmode(0, lv) == 0, "slowmode when quiet")
        expect(target_slowmode(10_000, lv) == max(int(l["slowmode"]) for l in lv), "highest level not reached")
        return "scales with activity"

    async def counting():
        from cogs.counting import NUMBER_RE
        expect(NUMBER_RE.match("42 nice").group(1) == "42" and NUMBER_RE.match("hi") is None, "number parsing")
        return "number parsing"

    async def backup():
        from cogs.backup import build_backup_zip, read_backup_zip
        name, data, included = build_backup_zip(run.bot)
        expect(len(data) > 0, "empty backup")
        if included:
            files = read_backup_zip(data)
            expect(set(files) == set(included), "backup round-trip lost files")
        return f"{len(included)} data file(s), {max(1, len(data) // 1024)} KB, restore readable"

    async def images():
        from cogs.profile_card import render_profile
        from cogs.welcome_image import render_card
        a = await asyncio.to_thread(render_card, None, "Test", "test", "Server", 1)
        b = await asyncio.to_thread(render_profile, None, "Test", "test", 0x5865F2, 3, 10, 100, 1, 500, 10, 5,
                                    "01 Jan 2026", [])
        expect(a.startswith(b"\x89PNG") and b.startswith(b"\x89PNG"), "not PNG")
        return "welcome image + profile card"

    async def embed_builder():
        from web.app import build_embed_message
        content, embed, view = build_embed_message({"embed": {"title": "T", "color": "#ff0000"},
                                                     "buttons": [{"label": "L", "url": "https://x.y"}]})
        expect(embed is not None and view is not None, "embed not built")
        try:
            build_embed_message({"buttons": [{"label": "x", "url": "javascript:x"}]})
            raise RuntimeError("unsafe link accepted")
        except ValueError:
            pass
        return "builds embeds, rejects unsafe links"

    async def ticket_form():
        from cogs.tickets import TicketFormModal, _ticket_type
        bug = _ticket_type("bug")
        if not bug or not bug.get("form"):
            raise Skip("bug ticket has no form")
        TicketFormModal(bug)
        return f"{len(bug['form'])} questions"

    async def role_panel():
        from cogs.role_panel import ReactionRolesView, get_panel_config
        buttons = get_panel_config().get("buttons", [])
        view = ReactionRolesView(buttons)
        expect(len(view.children) == len(buttons), "some role buttons couldn't be built (row full?)")
        return f"{len(buttons)} buttons"

    async def compat():
        from cogs.mod_info import compress_versions, loader_support
        s = loader_support([{"loaders": ["fabric"], "game_versions": ["1.21.1", "1.20.1"]}])
        expect(compress_versions(list(s["fabric"])) == "1.20.1, 1.21.1", "version list wrong")
        return "compatibility table helpers"

    for name, func in (("Crash analyzer", crash_analyzer), ("Link filter", link_filter), ("Time parsing", time_parsing),
                       ("Levels", levels), ("Roadmap commands", roadmap), ("FAQ suggestions", faq),
                       ("Auto slowmode", slowmode_levels), ("Counting", counting), ("Backup + restore", backup),
                       ("Images (Pillow)", images), ("Embed builder", embed_builder), ("Bug report form", ticket_form),
                       ("Role panel", role_panel), ("Compatibility table", compat)):
        await run.check(g, name, func)


# ---------------------------------------------------------------------------
# 5. Storage
# ---------------------------------------------------------------------------

async def storage_checks(run: SelfTestRun):
    g = "Storage"

    async def writable():
        path = DATA_DIR / f".{PREFIX}.tmp"
        path.write_text("ok", encoding="utf-8")
        ok = path.read_text(encoding="utf-8") == "ok"
        path.unlink()
        if not ok:
            raise RuntimeError("read back wrong content")
        return str(DATA_DIR)

    async def data_files():
        broken, count = [], 0
        for path in sorted(DATA_DIR.glob("*.json")):
            count += 1
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                broken.append(f"{path.name} ({e})")
        if broken:
            raise RuntimeError("unreadable: " + ", ".join(broken))
        return f"{count} data file(s) OK"

    async def disk():
        free = shutil.disk_usage(DATA_DIR).free // (1024 * 1024)
        if free < 100:
            raise RuntimeError(f"only {free} MB free")
        if free < 300:
            raise Warning_(f"only {free} MB free")
        return f"{free:,} MB free"

    for name, func in (("Data folder writable", writable), ("Data files readable", data_files), ("Disk space", disk)):
        await run.check(g, name, func)


# ---------------------------------------------------------------------------
# 6. External services
# ---------------------------------------------------------------------------

async def service_checks(run: SelfTestRun):
    g = "Services"

    async def modrinth():
        cfg = SERVER_TEMPLATE.get("modrinth", {})
        if not cfg.get("enabled") or not cfg.get("username"):
            raise Skip("Modrinth integration off")
        from cogs.modrinth import _fetch_user_projects
        projects = await _fetch_user_projects(cfg["username"])
        if not projects:
            raise RuntimeError("no projects returned - Modrinth unreachable or username wrong")
        return f"{len(projects)} projects"

    async def github():
        try:
            import updater
        except ImportError:
            raise Skip("updater not installed")
        if not updater.enabled():
            raise Skip("auto-update is off")
        head = await asyncio.to_thread(updater.remote_head)
        st = updater.status()
        note = "" if st.get("installed_commit") in (None, head["commit"]) else " (an update is available)"
        if st.get("last_error"):
            raise Warning_(f"GitHub reachable, but the last update step had a problem: {st['last_error']}")
        return f"reachable, newest {updater.short(head['commit'])}{note}"

    async def webhooks():
        configured, problems = [], []
        for key, env, label in (("bug_webhook", "BUG_WEBHOOK_URL", "bug"), ("backup_webhook", "BACKUP_WEBHOOK_URL", "backup")):
            url = webhook_url(key, env)
            if not url:
                continue
            configured.append(label)
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:  # GET only - posts nothing
                    if r.status != 200:
                        problems.append(f"{label} webhook answered HTTP {r.status} (deleted?)")
        if problems:
            raise RuntimeError("; ".join(problems))
        if not configured:
            raise Warning_("no bug/backup webhook set (/settings bug-webhook, /settings backup-webhook)")
        missing = {"bug", "backup"} - set(configured)
        if missing:
            raise Warning_(f"working: {', '.join(configured)} - not set: {', '.join(sorted(missing))}")
        return "bug + backup webhooks exist"

    async def web_panel():
        if os.getenv("PANEL_ENABLED", "true").lower() not in ("1", "true", "yes"):
            raise Skip("web panel off")
        port = int(os.getenv("PANEL_PORT", "8080"))
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/login", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    raise RuntimeError(f"/login answered HTTP {r.status}")
        if os.getenv("DISCORD_CLIENT_SECRET") and not os.getenv("PANEL_PUBLIC_URL"):
            raise Warning_("running, but PANEL_PUBLIC_URL is empty - Discord login may redirect wrong")
        return f"running on port {port}"

    async def backups_recent():
        if not webhook_url("backup_webhook", "BACKUP_WEBHOOK_URL"):
            raise Skip("no backup webhook")
        last = get_setting("last_backup_ts")
        if not last:
            raise Warning_("no backup sent yet (/backup-now)")
        hours = (time.time() - float(last)) / 3600
        if hours > 36:
            raise Warning_(f"last backup {hours:.0f} hours ago")
        return f"last backup {hours:.1f} hours ago"

    async def panel_launcher():
        import cogs.panel_launcher as pl
        for system in pl.SYSTEMS:  # both launcher files can be built
            name, data = pl.build_launcher(system, "https://discord.com/api/v10/webhooks/1/x", "k", "c2VjcmV0", "test")
            if b"__WEBHOOK_URL__" in data or len(data) < 2000:
                raise RuntimeError(f"launcher template {name} is broken")
        from web.app import current_bot, set_bot
        if current_bot() is None:
            set_bot(run.bot)  # panel HTTP server switched off
        status, headers, _ = await pl.call_panel("GET", "/login", "", b"",
                                                 {"user_id": run.user.id, "name": str(run.user)})
        if status != 303 or headers.get("location") != "/":
            raise RuntimeError(f"relayed panel request answered HTTP {status} (expected a logged-in redirect)")
        relay = pl.load_state()["relay"]
        if not relay:
            raise Skip("not set up yet (/panel-launcher get)")
        channel = run.bot.get_channel(relay["channel_id"])
        if channel is None:
            raise Warning_("the #panel-relay channel is gone - /panel-launcher get creates a new one "
                           "(existing launcher files stop working)")
        hooks = await channel.webhooks()
        if not any(h.id == relay["webhook_id"] for h in hooks):
            raise Warning_("the relay webhook is gone - /panel-launcher get creates a new one "
                           "(existing launcher files stop working)")
        if not run.bot.intents.message_content:
            raise RuntimeError("MESSAGE_CONTENT_INTENT is off - the relay can't read launcher requests")
        return f"relay ok, {len(pl.load_state()['keys'])} launcher file(s) active"

    for name, func in (("Modrinth", modrinth), ("GitHub (auto-update)", github), ("Webhooks", webhooks),
                       ("Web panel", web_panel), ("Panel launcher", panel_launcher), ("Backups", backups_recent)):
        await run.check(g, name, func)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_log(run: SelfTestRun) -> str:
    c = run.counts()
    lines = [
        f"Bot self-test - {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
        f"Server: {run.guild.name} ({run.guild.id}) - started by {run.user} ({run.user.id})",
        f"Result: {c[PASS]} passed, {c[WARN]} warnings, {c[FAIL]} failed, {c[SKIP]} skipped "
        f"({time.time() - run.started:.1f}s)",
        f"Bot: {run.bot.user} - Python {platform.python_version()} - discord.py {discord.__version__} - "
        f"{platform.system()} {platform.release()}",
    ]
    try:
        import updater
        st = updater.status()
        lines.append(f"Version: {updater.short(st.get('installed_commit'))} "
                     f"(auto-update {'on' if st['enabled'] else 'off'}, {st['repo']}@{st['branch']}, phase {st['phase']})")
    except Exception:
        pass
    for title, wanted in (("FAILED", FAIL), ("WARNINGS", WARN)):
        items = [r for r in run.results if r.status == wanted]
        if items:
            lines += ["", "=" * 20 + f" {title} " + "=" * 20]
            for r in items:
                lines.append(f"[{r.status}] {r.group} / {r.name} ({r.seconds:.1f}s)")
                lines.append(f"    {r.detail}")
                if r.trace:
                    lines += ["    " + t for t in r.trace.rstrip().splitlines()]
    lines += ["", "=" * 20 + " ALL CHECKS " + "=" * 20]
    for r in run.results:
        lines.append(f"[{r.status}] {r.group} / {r.name} ({r.seconds:.1f}s) - {r.detail}")
    try:
        from web.log_buffer import LOG_BUFFER
        entries = list(LOG_BUFFER)[-300:]
        lines += ["", "=" * 20 + f" BOT LOG (last {len(entries)} lines) " + "=" * 20]
        for e in entries:
            lines.append(f"{datetime.fromtimestamp(e['time']):%H:%M:%S} {e['level']:<8} {e['name']}: {e['message']}")
    except Exception:
        pass
    return "\n".join(lines) + "\n"


def build_embed(run: SelfTestRun, final: bool) -> discord.Embed:
    c = run.counts()
    if not final:
        color, title = 0x5865F2, "\U0001F9EA Self-test running..."
    elif c[FAIL]:
        color, title = 0xE74C3C, f"❌ Self-test: {c[FAIL]} problem(s)"
    elif c[WARN]:
        color, title = 0xF1C40F, "✅ Self-test passed (with warnings)"
    else:
        color, title = 0x57F287, "✅ Everything works"
    embed = discord.Embed(title=title, color=color)
    groups: dict[str, list[Result]] = {}
    for r in run.results:
        groups.setdefault(r.group, []).append(r)
    for group, items in groups.items():
        bad = [r for r in items if r.status in (FAIL, WARN)]
        ok = sum(1 for r in items if r.status == PASS)
        lines = [f"{ICON[r.status]} **{r.name}:** {r.detail[:150]}" for r in bad[:6]]
        head = f"{ICON[PASS]} {ok}/{len(items)} passed" if not bad else f"{ok}/{len(items)} passed"
        embed.add_field(name=group, value=(head + ("\n" + "\n".join(lines) if lines else ""))[:1024], inline=False)
    embed.set_footer(text=f"{c[PASS]} passed · {c[WARN]} warnings · {c[FAIL]} failed · "
                          f"{c[SKIP]} skipped · {time.time() - run.started:.0f}s")
    return embed


class SelfTest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    @app_commands.command(name="selftest", description="Test everything the bot does, end to end (administrator).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def selftest(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if self.lock.locked():
            await interaction.response.send_message("A self-test is already running.", ephemeral=True)
            return
        async with self.lock:
            await interaction.response.defer(ephemeral=True, thinking=True)
            run = SelfTestRun(self.bot, interaction.guild, interaction.user)
            log.info("Self-test started by %s", interaction.user)
            progress = await interaction.followup.send(embed=build_embed(run, False), ephemeral=True, wait=True)

            await cleanup_leftovers(interaction.guild)
            for step in (bot_checks, server_checks, discord_checks):
                await step(run)
                await self._update(progress, run)
            await cleanup(run)  # always remove what the test created
            for step in (feature_checks, storage_checks, service_checks):
                await step(run)
                await self._update(progress, run)

            c = run.counts()
            log.info("Self-test finished: %d passed, %d warnings, %d failed", c[PASS], c[WARN], c[FAIL])
            embed = build_embed(run, True)
            if not c[FAIL]:
                embed.description = "All test channels and roles were removed again."
                await self._update(progress, run, embed)
                return

            report = build_log(run)
            filename = f"selftest-{datetime.now(timezone.utc):%Y%m%d-%H%M}.log"
            try:
                await interaction.user.send(
                    content=f"❌ The self-test on **{interaction.guild.name}** found {c[FAIL]} problem(s). "
                            "The full log is attached.",
                    embed=embed,
                    file=discord.File(io.BytesIO(report.encode("utf-8")), filename=filename),
                )
                embed.description = "Test channels and roles were removed. The full log was sent to your DMs."
                await self._update(progress, run, embed)
            except (discord.Forbidden, discord.HTTPException):
                embed.description = "I couldn't DM you (DMs closed?) - here's the log file instead."
                await interaction.followup.send(
                    embed=embed, ephemeral=True,
                    file=discord.File(io.BytesIO(report.encode("utf-8")), filename=filename),
                )

    async def _update(self, message: discord.WebhookMessage, run: SelfTestRun, embed: discord.Embed | None = None):
        try:
            await message.edit(embed=embed or build_embed(run, False))
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(SelfTest(bot))
