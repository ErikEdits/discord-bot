"""/afk [reason]: while you're AFK, anyone who mentions you gets a short note.
Your AFK status ends automatically when you write your next message.
State: data/afk.json.
"""

import logging
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, format_duration, load_json, save_json

log = logging.getLogger("setup-bot.afk")

STATE_FILE = DATA_DIR / "afk.json"
NOTICE_COOLDOWN = 60  # seconds per (channel, afk member)


class Afk(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(STATE_FILE)
        self._cooldown: dict[tuple[int, int], float] = {}

    def _save(self) -> None:
        save_json(STATE_FILE, self.data)

    @app_commands.command(name="afk", description="Set yourself AFK - people who mention you get a note.")
    @app_commands.describe(reason="Why you're away (optional)")
    @app_commands.guild_only()
    async def afk(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 200] | None = None):
        self.data.setdefault(str(interaction.guild_id), {})[str(interaction.user.id)] = {
            "reason": reason or "",
            "since": time.time(),
        }
        self._save()
        await interaction.response.send_message(
            f"\U0001F4A4 You're now AFK{': ' + reason if reason else ''}. It ends when you write your next message.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        afk_users = self.data.get(str(message.guild.id), {})
        if not afk_users:
            return
        entry = afk_users.pop(str(message.author.id), None)
        if entry is not None:
            self._save()
            away = format_duration(timedelta(seconds=int(time.time() - entry.get("since", time.time()))))
            try:
                await message.channel.send(f"\U0001F44B Welcome back {message.author.mention}, you were AFK for {away}.",
                                           delete_after=10, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        now = time.monotonic()
        notes = []
        for member in message.mentions:
            info = afk_users.get(str(member.id))
            if info is None or member.id == message.author.id:
                continue
            key = (message.channel.id, member.id)
            if now - self._cooldown.get(key, 0) < NOTICE_COOLDOWN:
                continue
            self._cooldown[key] = now
            reason = f": {info['reason']}" if info.get("reason") else ""
            notes.append(f"\U0001F4A4 **{member.display_name}** is AFK{reason} (since <t:{int(info['since'])}:R>)")
        if notes:
            try:
                await message.reply("\n".join(notes[:5]), mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Afk(bot))
