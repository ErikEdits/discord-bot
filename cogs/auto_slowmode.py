"""Automatic slowmode that scales with activity.

For the channels in SERVER_TEMPLATE["auto_slowmode"]["channels"] the bot counts
messages in the last 60 seconds. The busier the channel, the longer the
slowmode (see "levels"). It goes back down one step at a time once the channel
has been calmer for `calm_minutes`, and never below the channel's normal
slowmode. Changes are logged in #mod-logs.

The normal slowmode is remembered in data/auto_slowmode.json so it's restored
after a restart.
"""

import logging
import time
from collections import deque

import discord
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.auto_slowmode")

STATE_FILE = DATA_DIR / "auto_slowmode.json"
WINDOW = 60


def _config() -> dict:
    return SERVER_TEMPLATE.get("auto_slowmode", {})


def target_slowmode(count: int, levels: list[dict]) -> int:
    """Slowmode for a message count (0 = no extra slowmode)."""
    for level in sorted(levels, key=lambda l: l["messages"], reverse=True):
        if count >= level["messages"]:
            return int(level["slowmode"])
    return 0


class AutoSlowmode(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.times: dict[int, deque] = {}
        self.state = load_json(STATE_FILE)  # channel_id -> {"base", "applied", "changed"}
        if _config().get("enabled", True):
            self.check_loop.start()

    def cog_unload(self):
        if self.check_loop.is_running():
            self.check_loop.cancel()

    def _save(self) -> None:
        save_json(STATE_FILE, self.state)

    @commands.Cog.listener()
    async def on_ready(self):
        # After a restart the counters are empty - put channels back to their normal slowmode.
        for cid, st in list(self.state.items()):
            channel = self.bot.get_channel(int(cid))
            if isinstance(channel, discord.TextChannel) and channel.slowmode_delay != st["base"]:
                try:
                    await channel.edit(slowmode_delay=st["base"], reason="Auto-slowmode reset after restart")
                except discord.HTTPException:
                    continue
            self.state.pop(cid, None)
        self._save()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.name not in set(_config().get("channels", [])):
            return
        q = self.times.setdefault(message.channel.id, deque())
        q.append(time.monotonic())

    async def _set(self, channel: discord.TextChannel, seconds: int, count: int, st: dict) -> None:
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Auto-slowmode ({count} messages/min)")
        except discord.HTTPException:
            log.warning("Could not change slowmode in #%s", channel.name)
            return
        st["applied"], st["changed"] = seconds, time.time()
        log.info("Auto-slowmode #%s -> %ss (%d msgs/min)", channel.name, seconds, count)
        try:
            from cogs.logging_cog import _get_log_channel, _safe_send
            ch = _get_log_channel(channel.guild, "mod")
            if ch:
                text = (f"{channel.mention}: slowmode **{seconds}s** ({count} messages in the last minute)"
                        if seconds > st["base"] else f"{channel.mention}: back to normal slowmode ({st['base']}s)")
                await _safe_send(ch, embed=discord.Embed(title="Auto-Slowmode", description=text, color=0x95A5A6))
        except Exception:
            pass

    @tasks.loop(seconds=20)
    async def check_loop(self):
        cfg = _config()
        levels = cfg.get("levels", [])
        calm = float(cfg.get("calm_minutes", 3)) * 60
        now_mono, now = time.monotonic(), time.time()
        changed = False
        for cid, q in list(self.times.items()):
            while q and now_mono - q[0] > WINDOW:
                q.popleft()
            channel = self.bot.get_channel(cid)
            if not isinstance(channel, discord.TextChannel):
                continue
            count = len(q)
            key = str(cid)
            st = self.state.get(key)
            wanted = target_slowmode(count, levels)
            if st is None:
                if wanted <= channel.slowmode_delay:
                    continue  # the channel's own slowmode is already enough
                st = {"base": channel.slowmode_delay, "applied": channel.slowmode_delay, "changed": 0}
                self.state[key] = st
                changed = True
            desired = max(st["base"], wanted)
            if desired > st["applied"]:
                await self._set(channel, desired, count, st)
                changed = True
            elif desired < st["applied"] and now - st["changed"] >= calm:
                # Step down one level at a time.
                lower = [int(l["slowmode"]) for l in levels if int(l["slowmode"]) < st["applied"]]
                step = max([desired] + lower)
                await self._set(channel, max(st["base"], step), count, st)
                if st["applied"] <= st["base"]:
                    self.state.pop(key, None)
                changed = True
            if not q and key not in self.state:
                self.times.pop(cid, None)
        if changed:
            self._save()

    @check_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(AutoSlowmode(bot))
