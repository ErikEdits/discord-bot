"""Release countdowns (administrator).

    /countdown create title when [channel] [description] [ping]
    /countdown list
    /countdown delete id

The embed uses Discord's live timestamps, so it counts down by itself without
the bot editing it. When the time is up the bot switches the embed to
"Out now!" and optionally pings. `when` uses the same formats as /schedule
("2026-10-01 18:00", "01.10. 18:00", "18:00", "3d").
State: data/countdowns.json.
"""

import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from cogs.scheduler import PING_CHOICES, parse_when, validate_time
from server_template import CHANNELS

log = logging.getLogger("setup-bot.countdown")

STATE_FILE = DATA_DIR / "countdowns.json"


def _load() -> list[dict]:
    return load_json(STATE_FILE, default={"countdowns": []}).get("countdowns", [])


def _save(items: list[dict]) -> None:
    save_json(STATE_FILE, {"countdowns": items})


def build_embed(item: dict, done: bool = False) -> discord.Embed:
    ts = int(item["ends"])
    if done:
        embed = discord.Embed(title=f"\U0001F389 {item['title']} - out now!", color=0x57F287,
                              description=item.get("description") or None)
        embed.set_footer(text="Countdown finished")
    else:
        desc = f"# <t:{ts}:R>\n<t:{ts}:F>"
        if item.get("description"):
            desc = item["description"] + "\n\n" + desc
        embed = discord.Embed(title=f"⏳ {item['title']}", description=desc, color=0x5865F2)
        embed.set_footer(text="Countdown")
    return embed


class Countdown(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.finish_loop.start()

    def cog_unload(self):
        if self.finish_loop.is_running():
            self.finish_loop.cancel()

    group = app_commands.Group(
        name="countdown",
        description="Release countdowns (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @tasks.loop(seconds=30)
    async def finish_loop(self):
        items = _load()
        now = time.time()
        due = [i for i in items if i["ends"] <= now]
        for item in due:
            channel = self.bot.get_channel(item["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(item["message_id"])
                    await message.edit(embed=build_embed(item, done=True))
                    content, mentions = None, discord.AllowedMentions.none()
                    ping = item.get("ping", "none")
                    if ping == "everyone":
                        content, mentions = "@everyone", discord.AllowedMentions(everyone=True)
                    elif ping.startswith("role:"):
                        role = discord.utils.get(channel.guild.roles, name=ping[5:])
                        if role:
                            content, mentions = role.mention, discord.AllowedMentions(roles=[role])
                    await message.reply(
                        (content + " " if content else "") + f"\U0001F389 **{item['title']}** is here!",
                        allowed_mentions=mentions,
                    )
                except discord.HTTPException:
                    log.warning("Could not finish countdown %s", item["id"])
            log.info("Countdown %s finished", item["id"])
        if due:
            due_ids = {i["id"] for i in due}
            _save([i for i in _load() if i["id"] not in due_ids])

    @finish_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @group.command(name="create", description="Post a countdown to a release or event.")
    @app_commands.describe(title="e.g. 'SmiteMod 2.0'", when="e.g. 2026-10-01 18:00, 01.10. 18:00 or 3d",
                           channel="Where to post it (default: announcements)",
                           description="Optional text above the countdown", ping="Who to ping when it's over")
    @app_commands.choices(ping=PING_CHOICES)
    async def create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 200], when: str,
                     channel: discord.TextChannel | None = None, description: str | None = None,
                     ping: app_commands.Choice[str] | None = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        ends = parse_when(when)
        error = validate_time(ends)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        channel = channel or discord.utils.get(interaction.guild.text_channels, name=CHANNELS.get("announcements", "")) \
            or interaction.channel
        item = {
            "id": uuid.uuid4().hex[:6],
            "guild_id": interaction.guild_id,
            "channel_id": channel.id,
            "title": title,
            "description": (description or "")[:1500],
            "ends": ends,
            "ping": ping.value if ping else "none",
        }
        try:
            message = await channel.send(embed=build_embed(item))
        except discord.HTTPException:
            await interaction.response.send_message(f"I can't post in {channel.mention}.", ephemeral=True)
            return
        item["message_id"] = message.id
        items = _load()
        items.append(item)
        _save(items)
        await interaction.response.send_message(
            f"Countdown `{item['id']}` posted in {channel.mention} - ends <t:{int(ends)}:R>.", ephemeral=True
        )

    @group.command(name="list", description="Show running countdowns.")
    async def list_(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        items = [i for i in _load() if i.get("guild_id") == interaction.guild_id]
        if not items:
            await interaction.response.send_message("No running countdowns.", ephemeral=True)
            return
        lines = [f"`{i['id']}` **{i['title']}** in <#{i['channel_id']}> - <t:{int(i['ends'])}:R>" for i in items]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @group.command(name="delete", description="Stop a countdown and delete its message.")
    async def delete(self, interaction: discord.Interaction, countdown_id: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        items = _load()
        item = next((i for i in items if i["id"] == countdown_id.strip()), None)
        if item is None:
            await interaction.response.send_message("No countdown with that ID.", ephemeral=True)
            return
        _save([i for i in items if i["id"] != item["id"]])
        channel = self.bot.get_channel(item["channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                await (await channel.fetch_message(item["message_id"])).delete()
            except discord.HTTPException:
                pass
        await interaction.response.send_message("Countdown deleted.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Countdown(bot))
