"""Daily "Did you know?" tips.

Once a day (SERVER_TEMPLATE["tips"]["hour"]) the bot posts the next tip in the
configured channel. Tips are cycled in a shuffled order so none repeats before
all were shown. Start list: SERVER_TEMPLATE["tips"]["defaults"].

    /tip add text      /tip remove      /tip list      /tip post   (administrator)

State: data/tips.json.
"""

import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
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
    return data


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
        if _config().get("enabled", True):
            self.daily_loop.start()

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
        save_json(STATE_FILE, data)
        return posted

    @tasks.loop(minutes=10)
    async def daily_loop(self):
        now = datetime.now(_tz())
        if now.hour < int(_config().get("hour", 17)):
            return
        if _load().get("last_date") == now.date().isoformat():
            return
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
