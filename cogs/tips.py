"""Daily "Did you know?" tips.

Once a day (SERVER_TEMPLATE["tips"]["hour"]) the bot posts the next tip in the
configured channel. Tips are cycled in a shuffled order so none repeats before
all were shown. Start list: SERVER_TEMPLATE["tips"]["defaults"].

    /tip add text      /tip remove      /tip list      /tip post   (administrator)
    /tip settings [enabled] [every] [time]   on/off, how often, what time (administrator)

"every" of a day or more posts at the set time every N days; shorter intervals
(e.g. 12h) post whenever that much time has passed since the last tip.

State: data/tips.json.
"""

import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, format_duration, is_admin, load_json, parse_duration, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.tips")

STATE_FILE = DATA_DIR / "tips.json"


def _config() -> dict:
    return SERVER_TEMPLATE.get("tips", {})


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_config().get("timezone", "Europe/Berlin"))
    except Exception:
        return ZoneInfo("UTC")


def _load() -> dict:
    data = load_json(STATE_FILE)
    if "tips" not in data:
        data["tips"] = list(_config().get("defaults", []))
    data.setdefault("queue", [])
    data.setdefault("last_date", None)
    data.setdefault("settings", {})
    return data


def tip_settings(data: dict | None = None) -> dict:
    """Saved with /tip settings, defaults from SERVER_TEMPLATE["tips"]."""
    saved = (data or _load())["settings"]
    return {
        "enabled": bool(saved.get("enabled", _config().get("enabled", True))),
        "every_seconds": int(saved.get("every_seconds", 86400)),
        "hour": int(saved.get("hour", _config().get("hour", 17))),
        "minute": int(saved.get("minute", 0)),
    }


def tip_due(data: dict, now: datetime) -> bool:
    s = tip_settings(data)
    if not s["enabled"]:
        return False
    if s["every_seconds"] < 86400:
        return time.time() - float(data.get("last_ts") or 0) >= s["every_seconds"]
    if (now.hour, now.minute) < (s["hour"], s["minute"]):
        return False
    if not data.get("last_date"):
        return True
    days = max(1, round(s["every_seconds"] / 86400))
    return (now.date() - date.fromisoformat(data["last_date"])).days >= days


def next_tip(data: dict) -> str | None:
    """Pop the next tip from the shuffled queue (refilled when empty)."""
    if not data["tips"]:
        return None
    data["queue"] = [t for t in data["queue"] if t in data["tips"]]
    if not data["queue"]:
        data["queue"] = random.sample(data["tips"], len(data["tips"]))
    return data["queue"].pop(0)


class Tips(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_loop.start()  # always runs; /tip settings can switch tips on and off

    def cog_unload(self):
        if self.daily_loop.is_running():
            self.daily_loop.cancel()

    group = app_commands.Group(
        name="tip",
        description="'Did you know?' tips (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def post_tip(self) -> bool:
        data = _load()
        tip = next_tip(data)
        if tip is None:
            return False
        embed = discord.Embed(title="\U0001F4A1 Did you know?", description=tip, color=0xF1C40F)
        posted = False
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=_config().get("channel", ""))
            if channel is None:
                continue
            try:
                await channel.send(embed=embed)
                posted = True
            except discord.HTTPException:
                pass
        data["last_date"] = datetime.now(_tz()).date().isoformat()
        data["last_ts"] = time.time()
        save_json(STATE_FILE, data)
        return posted

    @tasks.loop(minutes=5)
    async def daily_loop(self):
        if tip_due(_load(), datetime.now(_tz())):
            await self.post_tip()

    @daily_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _tip_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=f"{i + 1}. {t}"[:100], value=str(i + 1))
            for i, t in enumerate(_load()["tips"]) if current.lower() in t.lower() or current == str(i + 1)
        ][:25]

    @group.command(name="add", description="Add a tip.")
    async def add(self, interaction: discord.Interaction, text: app_commands.Range[str, 5, 1000]):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        data["tips"].append(text)
        save_json(STATE_FILE, data)
        await interaction.response.send_message(f"Tip #{len(data['tips'])} added.", ephemeral=True)

    @group.command(name="remove", description="Remove a tip.")
    @app_commands.describe(number="Tip number from /tip list")
    @app_commands.autocomplete(number=_tip_autocomplete)
    async def remove(self, interaction: discord.Interaction, number: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        if not number.isdigit() or not 1 <= int(number) <= len(data["tips"]):
            await interaction.response.send_message("No tip with that number.", ephemeral=True)
            return
        removed = data["tips"].pop(int(number) - 1)
        save_json(STATE_FILE, data)
        await interaction.response.send_message(f"Removed: {removed[:200]}", ephemeral=True)

    @group.command(name="list", description="Show all tips.")
    async def list_(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        tips = _load()["tips"]
        text = "\n".join(f"**{i + 1}.** {t[:180]}" for i, t in enumerate(tips)) or "No tips yet - add one with `/tip add`."
        embed = discord.Embed(title=f"Tips ({len(tips)})", description=text[:4000], color=0xF1C40F)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="settings", description="Tips on/off, how often and at what time.")
    @app_commands.describe(
        enabled="Turn the 'Did you know?' tips on or off",
        every="How often, e.g. 1d (daily), 2d, 7d or 12h",
        time="Time of day for daily (or longer) tips, e.g. 17:00",
    )
    async def settings(self, interaction: discord.Interaction, enabled: bool | None = None,
                       every: str | None = None, time: str | None = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        changes = {}
        if enabled is not None:
            changes["enabled"] = enabled
        if every is not None:
            d = parse_duration(every)
            if d is None or not 3600 <= d.total_seconds() <= 30 * 86400:
                await interaction.response.send_message(
                    "`every` must be between 1h and 30d, e.g. `1d`, `2d`, `7d` or `12h`.", ephemeral=True)
                return
            changes["every_seconds"] = int(d.total_seconds())
        if time is not None:
            m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(?:uhr)?\s*", time.lower())
            if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
                await interaction.response.send_message("`time` must look like `17:00` or `9`.", ephemeral=True)
                return
            changes["hour"], changes["minute"] = int(m.group(1)), int(m.group(2) or 0)
        if changes:
            data["settings"].update(changes)
            save_json(STATE_FILE, data)
            log.info("Tip settings changed by %s: %s", interaction.user, changes)
        s = tip_settings(data)
        every_text = format_duration(timedelta(seconds=s["every_seconds"]))
        if s["every_seconds"] >= 86400:
            when = f"every {every_text} at {s['hour']:02d}:{s['minute']:02d} ({_config().get('timezone', 'Europe/Berlin')})"
        else:
            when = f"every {every_text}"
        embed = discord.Embed(title="\U0001F4A1 'Did you know?' tips", color=0xF1C40F if s["enabled"] else 0x95A5A6)
        embed.add_field(name="Status", value="✅ on" if s["enabled"] else "⛔ off", inline=True)
        embed.add_field(name="When", value=when, inline=True)
        embed.add_field(name="Tips", value=str(len(data["tips"])), inline=True)
        embed.set_footer(text="Saved ✓" if changes else "Change: /tip settings enabled:… every:1d time:17:00")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="post", description="Post the next tip right now.")
    async def post(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await self.post_tip()
        await interaction.followup.send("Tip posted." if ok else "Nothing posted (no tips or channel missing).",
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tips(bot))
