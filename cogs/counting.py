"""Counting channel: members count up together in CHANNELS["counting"].

- Each message must be the next number (a message may start with the number,
  e.g. "42 almost there").
- The same person can't count twice in a row.
- A wrong number resets the count to 0. The highest count ever is the record.
- Messages without a number are removed to keep the channel clean.
- If someone deletes the latest number, the bot says what the next number is.

State: data/counting.json. Config: SERVER_TEMPLATE["counting"].
"""

import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, is_admin, load_json, mark_bot_delete, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.counting")

STATE_FILE = DATA_DIR / "counting.json"
NUMBER_RE = re.compile(r"^\s*(\d{1,9})(?!\d)")
SPECIAL = {100: "\U0001F4AF", 1000: "\U0001F389", 69: "\U0001F60F", 420: "\U0001F33F"}


def _enabled() -> bool:
    return SERVER_TEMPLATE.get("counting", {}).get("enabled", True)


def _state(data: dict, guild_id: int) -> dict:
    return data.setdefault(str(guild_id), {"current": 0, "last_user": None, "last_message": None,
                                           "record": 0, "record_ts": None})


class Counting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(STATE_FILE)

    def _save(self) -> None:
        save_json(STATE_FILE, self.data)

    def _is_counting(self, channel) -> bool:
        return getattr(channel, "name", None) == CHANNELS.get("counting")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not _enabled() or message.guild is None or message.author.bot or not self._is_counting(message.channel):
            return
        match = NUMBER_RE.match(message.content or "")
        if match is None:
            mark_bot_delete(message.id)
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        number = int(match.group(1))
        st = _state(self.data, message.guild.id)
        expected = st["current"] + 1
        if number == expected and st["last_user"] != message.author.id:
            st["current"] = number
            st["last_user"] = message.author.id
            st["last_message"] = message.id
            new_record = number > st["record"]
            if new_record:
                st["record"], st["record_ts"] = number, time.time()
            self._save()
            try:
                await message.add_reaction(SPECIAL.get(number, "✅"))
                if new_record and number > 1 and number % 50 == 0:
                    await message.channel.send(f"\U0001F3C6 New record: **{number}**!")
            except discord.HTTPException:
                pass
            return

        reason = "counted twice in a row" if number == expected else f"wrote **{number}** instead of **{expected}**"
        reached = st["current"]
        st.update(current=0, last_user=None, last_message=None)
        self._save()
        try:
            await message.add_reaction("❌")
            await message.channel.send(
                f"\U0001F4A5 {message.author.mention} {reason} and ruined it at **{reached}**! "
                f"Record: **{st['record']}**. Start again with **1**.",
                allowed_mentions=discord.AllowedMentions(users=False),
            )
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or not self._is_counting(message.channel):
            return
        st = _state(self.data, message.guild.id)
        if st.get("last_message") == message.id and st["current"] > 0:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} deleted their number **{st['current']}**. "
                    f"The next number is **{st['current'] + 1}**.",
                    allowed_mentions=discord.AllowedMentions(users=False),
                )
            except discord.HTTPException:
                pass

    @app_commands.command(name="counting", description="Show the current count and the record.")
    @app_commands.guild_only()
    async def counting(self, interaction: discord.Interaction):
        st = _state(self.data, interaction.guild_id)
        record = f"**{st['record']}**" + (f" (<t:{int(st['record_ts'])}:R>)" if st.get("record_ts") else "")
        await interaction.response.send_message(
            f"Current count: **{st['current']}** - next number is **{st['current'] + 1}**.\nRecord: {record}",
            ephemeral=True,
        )

    @app_commands.command(name="counting-set", description="Set the current count, e.g. after a mistake (administrator).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def counting_set(self, interaction: discord.Interaction, number: app_commands.Range[int, 0, 999_999_999]):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        st = _state(self.data, interaction.guild_id)
        st.update(current=number, last_user=None, last_message=None)
        self._save()
        await interaction.response.send_message(f"Count set to **{number}** - next is **{number + 1}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Counting(bot))
