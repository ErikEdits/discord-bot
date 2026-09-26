"""Giveaways with a join button.

    /giveaway start prize duration [winners] [channel] [required_role]
    /giveaway end id        end early and draw the winners
    /giveaway reroll id     draw new winner(s) (previous winners are excluded)
    /giveaway list          running giveaways

All commands need Administrator. Members join/leave by clicking the button;
entrants are saved in data/giveaways.json so giveaways survive restarts, and
giveaways that ended while the bot was offline are drawn right after start.
Winners are drawn from entrants who are still on the server.
"""

import logging
import random
import time
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, format_duration, is_admin, load_json, parse_duration, save_json

log = logging.getLogger("setup-bot.giveaway")

GIVEAWAYS_FILE = DATA_DIR / "giveaways.json"
JOIN_ID = "giveaway:join"
EMBED_REFRESH_SECONDS = 20  # edit the entrant counter at most this often
KEEP_ENDED_DAYS = 30        # ended giveaways are kept this long (for rerolls), then removed


def _load() -> dict:
    data = load_json(GIVEAWAYS_FILE)
    data.setdefault("giveaways", {})
    return data


def _by_message(data: dict, message_id: int) -> tuple[str, dict] | tuple[None, None]:
    for gid, g in data["giveaways"].items():
        if g.get("message_id") == message_id:
            return gid, g
    return None, None


def build_embed(g: dict) -> discord.Embed:
    ended = g.get("status") != "open"
    if ended:
        winners = g.get("winner_ids") or []
        winner_text = ", ".join(f"<@{w}>" for w in winners) if winners else "Nobody joined \U0001F622"
        description = (
            f"**Winner{'s' if len(winners) != 1 else ''}:** {winner_text}\n"
            f"**Entries:** {len(g.get('entrants', []))}\n"
            f"Hosted by <@{g['host_id']}>"
        )
        color = 0x95A5A6
    else:
        description = (
            f"Click **\U0001F389 Join** to enter!\n\n"
            f"**Ends:** <t:{int(g['end_ts'])}:R> (<t:{int(g['end_ts'])}:f>)\n"
            f"**Winners:** {g['winners']}\n"
            f"**Entries:** {len(g.get('entrants', []))}\n"
            f"Hosted by <@{g['host_id']}>"
        )
        if g.get("required_role_id"):
            description += f"\n**Required role:** <@&{g['required_role_id']}>"
        color = 0xE91E63
    embed = discord.Embed(
        title=f"\U0001F381 {g['prize']}"[:256],
        description=description,
        color=color,
        timestamp=datetime.fromtimestamp(g["end_ts"], timezone.utc),
    )
    embed.set_footer(text=f"{'Ended' if ended else 'Ends'} · ID {g['id']}")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, ended: bool = False):
        super().__init__(timeout=None)
        if ended:
            self.join.disabled = True
            self.join.label = "Ended"

    @discord.ui.button(label="Join", emoji="\U0001F389", style=discord.ButtonStyle.success, custom_id=JOIN_ID)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Giveaways" = interaction.client.get_cog("Giveaways")
        data = _load()
        gid, g = _by_message(data, interaction.message.id)
        if g is None or g.get("status") != "open" or g["end_ts"] <= time.time():
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        role_id = g.get("required_role_id")
        if role_id and not any(r.id == role_id for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message(f"You need the <@&{role_id}> role to join.", ephemeral=True)
            return
        entrants = g.setdefault("entrants", [])
        if interaction.user.id in entrants:
            entrants.remove(interaction.user.id)
            text = "You left the giveaway."
        else:
            entrants.append(interaction.user.id)
            text = f"You joined the giveaway for **{g['prize']}**. Good luck! (click again to leave)"
        save_json(GIVEAWAYS_FILE, data)
        await interaction.response.send_message(text, ephemeral=True)
        if cog:
            await cog.maybe_refresh(interaction.message, gid)


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_refresh: dict[str, float] = {}
        bot.add_view(GiveawayView())
        self.end_loop.start()

    def cog_unload(self):
        if self.end_loop.is_running():
            self.end_loop.cancel()

    group = app_commands.Group(
        name="giveaway",
        description="Run giveaways (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def maybe_refresh(self, message: discord.Message, gid: str) -> None:
        now = time.monotonic()
        if now - self._last_refresh.get(gid, 0) < EMBED_REFRESH_SECONDS:
            return
        self._last_refresh[gid] = now
        g = _load()["giveaways"].get(gid)
        if g:
            try:
                await message.edit(embed=build_embed(g))
            except discord.HTTPException:
                pass

    async def _fetch_message(self, g: dict) -> discord.Message | None:
        channel = self.bot.get_channel(g["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.fetch_message(g["message_id"])
        except (discord.NotFound, discord.Forbidden):
            return None

    def _draw(self, g: dict, count: int, exclude: set[int]) -> list[int]:
        guild = self.bot.get_guild(g["guild_id"])
        pool = [uid for uid in g.get("entrants", []) if uid not in exclude and (guild is None or guild.get_member(uid))]
        return random.sample(pool, min(count, len(pool)))

    async def end_giveaway(self, gid: str) -> dict | None:
        data = _load()
        g = data["giveaways"].get(gid)
        if not g or g.get("status") != "open":
            return None
        g["status"] = "ended"
        g["ended_at"] = time.time()
        g["winner_ids"] = self._draw(g, int(g["winners"]), set())
        save_json(GIVEAWAYS_FILE, data)

        message = await self._fetch_message(g)
        if message:
            try:
                await message.edit(embed=build_embed(g), view=GiveawayView(ended=True))
                if g["winner_ids"]:
                    mentions = ", ".join(f"<@{w}>" for w in g["winner_ids"])
                    await message.reply(
                        f"\U0001F389 Congratulations {mentions}! You won **{g['prize']}**!",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                else:
                    await message.reply(f"Nobody joined the giveaway for **{g['prize']}**.")
            except discord.HTTPException:
                log.exception("Failed to announce giveaway result")
        log.info("Giveaway %s ended: %s winner(s) of %d entrants", gid, len(g["winner_ids"]), len(g.get("entrants", [])))
        return g

    @tasks.loop(seconds=15)
    async def end_loop(self):
        data = _load()
        now = time.time()
        for gid, g in list(data["giveaways"].items()):
            if g.get("status") == "open" and g["end_ts"] <= now:
                await self.end_giveaway(gid)
        # Forget old ended giveaways so the file stays small.
        data = _load()
        cutoff = now - KEEP_ENDED_DAYS * 86400
        old = [gid for gid, g in data["giveaways"].items() if g.get("status") != "open" and g.get("ended_at", now) < cutoff]
        if old:
            for gid in old:
                del data["giveaways"][gid]
            save_json(GIVEAWAYS_FILE, data)

    @end_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @group.command(name="start", description="Start a giveaway.")
    @app_commands.describe(
        prize="What can be won",
        duration="How long it runs, e.g. 30m, 1d, 1w",
        winners="Number of winners (1-20)",
        channel="Where to post it (default: this channel)",
        required_role="Only members with this role can join (optional)",
    )
    async def start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str,
                    winners: app_commands.Range[int, 1, 20] = 1, channel: discord.TextChannel | None = None,
                    required_role: discord.Role | None = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta.total_seconds() < 60 or delta.days > 60:
            await interaction.response.send_message("Duration must be between 1 minute and 60 days (e.g. 30m, 1d, 1w).",
                                                    ephemeral=True)
            return
        channel = channel or interaction.channel
        g = {
            "id": uuid.uuid4().hex[:6],
            "guild_id": interaction.guild_id,
            "channel_id": channel.id,
            "message_id": None,
            "prize": prize,
            "winners": winners,
            "end_ts": time.time() + delta.total_seconds(),
            "host_id": interaction.user.id,
            "required_role_id": required_role.id if required_role else None,
            "entrants": [],
            "status": "open",
        }
        try:
            message = await channel.send(embed=build_embed(g), view=GiveawayView())
        except discord.Forbidden:
            await interaction.response.send_message(f"I can't post in {channel.mention}.", ephemeral=True)
            return
        g["message_id"] = message.id
        data = _load()
        data["giveaways"][g["id"]] = g
        save_json(GIVEAWAYS_FILE, data)
        await interaction.response.send_message(
            f"Giveaway `{g['id']}` started in {channel.mention} - ends in {format_duration(delta)}.", ephemeral=True
        )
        log.info("Giveaway %s started by %s: %s", g["id"], interaction.user, prize)

    @group.command(name="end", description="End a giveaway now and draw the winners.")
    @app_commands.describe(giveaway_id="The ID from the giveaway footer or /giveaway list")
    async def end(self, interaction: discord.Interaction, giveaway_id: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = await self.end_giveaway(giveaway_id.strip())
        if g is None:
            await interaction.followup.send("No running giveaway with that ID.", ephemeral=True)
            return
        await interaction.followup.send(f"Giveaway `{giveaway_id}` ended.", ephemeral=True)

    @group.command(name="reroll", description="Draw new winner(s) for an ended giveaway.")
    @app_commands.describe(giveaway_id="The giveaway ID", count="How many new winners (default 1)")
    async def reroll(self, interaction: discord.Interaction, giveaway_id: str,
                     count: app_commands.Range[int, 1, 20] = 1):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        g = data["giveaways"].get(giveaway_id.strip())
        if not g or g.get("status") == "open":
            await interaction.response.send_message("No ended giveaway with that ID.", ephemeral=True)
            return
        previous = set(g.get("winner_ids", [])) | set(g.get("rerolled_ids", []))
        new = self._draw(g, count, previous)
        if not new:
            await interaction.response.send_message("There are no other entrants left to draw.", ephemeral=True)
            return
        g.setdefault("rerolled_ids", []).extend(g.get("winner_ids", []))
        g["winner_ids"] = new
        save_json(GIVEAWAYS_FILE, data)
        message = await self._fetch_message(g)
        mentions = ", ".join(f"<@{w}>" for w in new)
        if message:
            try:
                await message.edit(embed=build_embed(g))
                await message.reply(f"\U0001F501 New winner{'s' if len(new) > 1 else ''}: {mentions}! "
                                    f"You won **{g['prize']}**!",
                                    allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"Rerolled: {mentions}", ephemeral=True)

    @group.command(name="list", description="List running giveaways.")
    async def list_giveaways(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        running = [g for g in _load()["giveaways"].values()
                   if g.get("status") == "open" and g.get("guild_id") == interaction.guild_id]
        if not running:
            await interaction.response.send_message("No running giveaways.", ephemeral=True)
            return
        embed = discord.Embed(title="Running giveaways", color=0xE91E63)
        for g in running[:20]:
            embed.add_field(
                name=f"`{g['id']}` - {g['prize']}"[:256],
                value=f"<#{g['channel_id']}> · {len(g.get('entrants', []))} entries · ends <t:{int(g['end_ts'])}:R>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
