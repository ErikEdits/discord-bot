"""Reminders delivered by DM.

    /remind in:2h30m text:"test the update"
    /reminders list
    /reminders delete id

Reminders are stored in data/reminders.json, so they survive restarts
(ones that became due while the bot was offline are sent right after start).
Limits come from SERVER_TEMPLATE["reminders"].
"""

import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, format_duration, load_json, parse_duration, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.reminders")

REMINDERS_FILE = DATA_DIR / "reminders.json"


def _config() -> dict:
    return SERVER_TEMPLATE.get("reminders", {})


def _load() -> list:
    data = load_json(REMINDERS_FILE, default={"reminders": []})
    return data.get("reminders", [])


def _save(reminders: list) -> None:
    save_json(REMINDERS_FILE, {"reminders": reminders})


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.deliver_loop.start()

    def cog_unload(self):
        if self.deliver_loop.is_running():
            self.deliver_loop.cancel()

    @app_commands.command(name="remind", description="Get a DM reminder later.")
    @app_commands.describe(when="In how long, e.g. 10m, 2h, 1d12h, 1w", text="What to remind you of")
    @app_commands.rename(when="in")
    async def remind(self, interaction: discord.Interaction, when: str, text: app_commands.Range[str, 1, 1000]):
        delta = parse_duration(when)
        max_days = int(_config().get("max_days", 365))
        if delta is None or delta.total_seconds() < 60:
            await interaction.response.send_message("Use a duration like `10m`, `2h`, `1d12h` or `1w` (min. 1 minute).",
                                                    ephemeral=True)
            return
        if delta.days > max_days:
            await interaction.response.send_message(f"Reminders can be at most {max_days} days ahead.", ephemeral=True)
            return
        reminders = _load()
        mine = [r for r in reminders if r["user_id"] == interaction.user.id]
        max_per_user = int(_config().get("max_per_user", 25))
        if len(mine) >= max_per_user:
            await interaction.response.send_message(
                f"You already have {max_per_user} reminders. Delete one with `/reminders delete`.", ephemeral=True
            )
            return
        due = time.time() + delta.total_seconds()
        reminder = {
            "id": uuid.uuid4().hex[:6],
            "user_id": interaction.user.id,
            "text": text,
            "due": due,
            "created": time.time(),
            "guild_name": interaction.guild.name if interaction.guild else None,
        }
        reminders.append(reminder)
        _save(reminders)
        await interaction.response.send_message(
            f"⏰ Got it! I'll DM you <t:{int(due)}:R> (in {format_duration(delta)}):\n> {text}\n"
            f"-# Make sure you allow DMs from this server's members. ID: `{reminder['id']}`",
            ephemeral=True,
        )

    reminders_group = app_commands.Group(name="reminders", description="Manage your reminders.")

    @reminders_group.command(name="list", description="Show your upcoming reminders.")
    async def list_reminders(self, interaction: discord.Interaction):
        mine = sorted((r for r in _load() if r["user_id"] == interaction.user.id), key=lambda r: r["due"])
        if not mine:
            await interaction.response.send_message("You have no reminders. Create one with `/remind`.", ephemeral=True)
            return
        lines = [f"`{r['id']}` <t:{int(r['due'])}:R> - {r['text'][:80]}" for r in mine[:25]]
        embed = discord.Embed(title="Your reminders", description="\n".join(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @reminders_group.command(name="delete", description="Delete one of your reminders.")
    @app_commands.describe(reminder_id="The ID from /reminders list")
    async def delete_reminder(self, interaction: discord.Interaction, reminder_id: str):
        reminders = _load()
        keep = [r for r in reminders if not (r["id"] == reminder_id.strip() and r["user_id"] == interaction.user.id)]
        if len(keep) == len(reminders):
            await interaction.response.send_message("No reminder of yours with that ID.", ephemeral=True)
            return
        _save(keep)
        await interaction.response.send_message("Reminder deleted.", ephemeral=True)

    @delete_reminder.autocomplete("reminder_id")
    async def _reminder_autocomplete(self, interaction: discord.Interaction, current: str):
        mine = sorted((r for r in _load() if r["user_id"] == interaction.user.id), key=lambda r: r["due"])
        return [
            app_commands.Choice(name=f"{r['id']} - {r['text']}"[:100], value=r["id"])
            for r in mine if current.lower() in (r["id"] + r["text"]).lower()
        ][:25]

    @tasks.loop(seconds=20)
    async def deliver_loop(self):
        reminders = _load()
        now = time.time()
        due = [r for r in reminders if r["due"] <= now]
        if not due:
            return
        for r in due:
            try:
                user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
                embed = discord.Embed(title="⏰ Reminder", description=r["text"], color=0x5865F2)
                where = f" on **{r['guild_name']}**" if r.get("guild_name") else ""
                embed.set_footer(text="Set with /remind")
                await user.send(content=f"You asked me <t:{int(r['created'])}:R>{where} to remind you:", embed=embed)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                log.info("Could not deliver reminder %s to %s (DMs closed?)", r["id"], r["user_id"])
        # Re-read before saving: someone may have added a reminder while we were sending.
        due_ids = {r["id"] for r in due}
        _save([r for r in _load() if r["id"] not in due_ids])

    @deliver_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Reminders(bot))
