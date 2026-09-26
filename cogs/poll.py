"""Reaction-based polls posted to the polls channel.

Admins create a poll with /poll create; the bot posts an embed with numbered
options, adds the matching number reactions, and members vote by reacting.
When the duration runs out (or /poll close is used) the bot tallies the
reactions, posts a result embed in the polls channel, and DMs the result to
the bot owner.

State is persisted to data/polls.json so polls survive restarts: on startup
open polls are re-scheduled and any already-expired ones are tallied at once.

Privacy: reaction polls are NOT anonymous (you can see who reacted). Only
aggregate results are reported. For true anonymity use native Discord polls.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.poll")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
POLLS_FILE = DATA_DIR / "polls.json"

# Channel the polls live in (key into the central CHANNELS map).
POLL_CHANNEL_KEY = "polls"

# Number emojis 1..9 used for the options + reactions.
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣",
                "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

MAX_OPTIONS = 9
MIN_OPTIONS = 2


def _presets() -> list[dict]:
    return SERVER_TEMPLATE.get("poll_presets", [])


def _preset_choices() -> list[app_commands.Choice]:
    # Built at import time from the template; Discord allows up to 25 choices.
    return [
        app_commands.Choice(name=p.get("label", p["key"])[:100], value=p["key"])
        for p in _presets()[:25]
    ]


def _preset_by_key(key: str) -> dict | None:
    for p in _presets():
        if p["key"] == key:
            return p
    return None


def _poll_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=CHANNELS.get(POLL_CHANNEL_KEY, ""))


def _load_state() -> dict:
    if not POLLS_FILE.exists():
        return {"polls": {}}
    try:
        data = json.loads(POLLS_FILE.read_text(encoding="utf-8"))
        data.setdefault("polls", {})
        return data
    except Exception:
        log.exception("Failed to read polls state")
        return {"polls": {}}


def _save_state(data: dict) -> None:
    POLLS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _build_poll_embed(question: str, options: list[str], end_ts: float, closed: bool = False,
                      results: list[tuple[str, int, float]] | None = None,
                      total: int = 0, winner: str | None = None) -> discord.Embed:
    if closed:
        embed = discord.Embed(
            title="\U0001F4CA Poll closed",
            description=f"**{question}**",
            color=0x95A5A6,
            timestamp=datetime.now(timezone.utc),
        )
        if results:
            lines = []
            for label, votes, pct in results:
                marker = " \U0001F3C6" if winner and label == winner else ""
                bar = "█" * round(pct / 10) or "░"
                lines.append(f"{label} — **{votes}** ({pct:.0f}%){marker}\n`{bar}`")
            embed.add_field(name="Results", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"{total} vote(s) total")
        return embed

    embed = discord.Embed(
        title="\U0001F4CA Poll",
        description=f"**{question}**",
        color=0x5865F2,
    )
    lines = [f"{NUMBER_EMOJI[i]} {opt}" for i, opt in enumerate(options)]
    embed.add_field(name="Options", value="\n".join(lines), inline=False)
    embed.add_field(
        name="How to vote",
        value="React with the matching number. Please pick **only one** option.\nReaction votes are **not anonymous**.",
        inline=False,
    )
    embed.add_field(name="Closes", value=discord.utils.format_dt(datetime.fromtimestamp(end_ts, timezone.utc), style="R"), inline=False)
    return embed


class Poll(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks: dict[str, asyncio.Task] = {}

    group = app_commands.Group(
        name="poll",
        description="Create and manage reaction polls.",
        default_permissions=discord.Permissions(administrator=True),
    )

    async def cog_load(self):
        # Re-schedule open polls after a restart.
        self.bot.loop.create_task(self._resume_polls())

    def cog_unload(self):
        for t in self._tasks.values():
            t.cancel()

    async def _resume_polls(self):
        await self.bot.wait_until_ready()
        state = _load_state()
        for poll_id, poll in list(state["polls"].items()):
            if poll.get("status") != "open":
                continue
            remaining = poll["end_ts"] - time.time()
            if remaining <= 0:
                await self._close_poll(poll_id, reason="expired-on-startup")
            else:
                self._schedule(poll_id, remaining)
        log.info("Resumed %d open poll(s)", len(self._tasks))

    def _schedule(self, poll_id: str, delay: float):
        async def _runner():
            try:
                await asyncio.sleep(delay)
                await self._close_poll(poll_id, reason="expired")
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Poll auto-close failed for %s", poll_id)
        self._tasks[poll_id] = self.bot.loop.create_task(_runner())

    async def _owner_user(self) -> discord.User | None:
        # Explicit OWNER_ID wins; otherwise fall back to the application owner.
        owner_id = os.getenv("OWNER_ID", "").strip()
        if owner_id.isdigit():
            try:
                return self.bot.get_user(int(owner_id)) or await self.bot.fetch_user(int(owner_id))
            except Exception:
                log.exception("Could not fetch OWNER_ID user")
        try:
            info = await self.bot.application_info()
            if info.team and info.team.members:
                return info.team.owner or info.owner
            return info.owner
        except Exception:
            log.exception("Could not resolve application owner")
            return None

    async def _close_poll(self, poll_id: str, reason: str = "manual"):
        state = _load_state()
        poll = state["polls"].get(poll_id)
        if not poll or poll.get("status") != "open":
            return
        task = self._tasks.pop(poll_id, None)
        if task and reason != "expired":  # "expired" means we're running inside that task
            task.cancel()

        channel = self.bot.get_channel(poll["channel_id"])
        message = None
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(poll["message_id"])
            except (discord.NotFound, discord.Forbidden):
                message = None

        options = poll["options"]
        counts = [0] * len(options)
        if message:
            for reaction in message.reactions:
                emoji = str(reaction.emoji)
                if emoji in NUMBER_EMOJI:
                    idx = NUMBER_EMOJI.index(emoji)
                    if idx < len(options):
                        counts[idx] = max(0, reaction.count - 1)  # subtract the bot's own reaction

        total = sum(counts)
        results = []
        for i, opt in enumerate(options):
            pct = (counts[i] / total * 100) if total else 0.0
            results.append((opt, counts[i], pct))
        results.sort(key=lambda r: r[1], reverse=True)
        winner = results[0][0] if total and results[0][1] > 0 else None

        poll["status"] = "closed"
        poll["counts"] = counts
        poll["closed_at"] = time.time()
        _save_state(state)

        result_embed = _build_poll_embed(
            poll["question"], options, poll["end_ts"], closed=True,
            results=results, total=total, winner=winner,
        )
        if message:
            try:
                await message.reply(embed=result_embed)
            except Exception:
                if isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send(embed=result_embed)
                    except Exception:
                        log.exception("Failed to post poll result")

        owner = await self._owner_user()
        if owner:
            dm_lines = [f"**Poll closed:** {poll['question']}", ""]
            for label, votes, pct in results:
                marker = " 🏆" if winner and label == winner else ""
                dm_lines.append(f"- {label}: **{votes}** ({pct:.0f}%){marker}")
            dm_lines.append(f"\nTotal votes: **{total}**")
            dm_lines.append(f"Reason: {reason}")
            try:
                await owner.send("\n".join(dm_lines))
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not DM poll result to owner")
        log.info("Closed poll %s (%d votes, reason=%s)", poll_id, total, reason)

    async def start_poll(self, guild: discord.Guild, question: str, options: list[str],
                         duration_hours: float, created_by: int) -> tuple[str, discord.TextChannel]:
        """Post a poll in the guild's poll channel. Raises ValueError with a user-facing message.

        Used by /poll and by the web panel.
        """
        if not question.strip():
            raise ValueError("The question can't be empty.")
        if not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
            raise ValueError(f"Please provide between {MIN_OPTIONS} and {MAX_OPTIONS} options.")
        if duration_hours <= 0 or duration_hours > 24 * 30:
            raise ValueError("Duration must be between 0 and 720 hours.")
        channel = _poll_channel(guild)
        if channel is None:
            raise ValueError(f"Poll channel not found. Run `/update` to create #{CHANNELS.get(POLL_CHANNEL_KEY)}.")

        end_ts = time.time() + duration_hours * 3600
        embed = _build_poll_embed(question, options, end_ts)
        try:
            message = await channel.send(embed=embed)
            for i in range(len(options)):
                await message.add_reaction(NUMBER_EMOJI[i])
        except discord.Forbidden:
            raise ValueError("I can't post or react in the poll channel.")

        poll_id = uuid.uuid4().hex[:8]
        state = _load_state()
        state["polls"][poll_id] = {
            "id": poll_id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "message_id": message.id,
            "question": question,
            "options": options,
            "end_ts": end_ts,
            "status": "open",
            "created_by": created_by,
            "created_at": time.time(),
        }
        _save_state(state)
        self._schedule(poll_id, end_ts - time.time())
        return poll_id, channel

    async def _create_poll(self, interaction: discord.Interaction, question: str,
                           options: list[str], duration_hours: float):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            poll_id, channel = await self.start_poll(guild, question, options, duration_hours, interaction.user.id)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(
            f"Poll created in {channel.mention} (ID `{poll_id}`). Closes in {duration_hours:g}h.",
            ephemeral=True,
        )

    @group.command(name="create", description="Create a reaction poll (2-9 options).")
    @app_commands.describe(
        frage="The poll question",
        optionen="Comma-separated options (2-9)",
        dauer_stunden="How many hours the poll stays open (default 24)",
    )
    async def create(self, interaction: discord.Interaction, frage: str, optionen: str, dauer_stunden: float = 24.0):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        options = [o.strip() for o in optionen.split(",") if o.strip()]
        await self._create_poll(interaction, frage, options, dauer_stunden)

    @group.command(name="preset", description="Launch a predefined poll.")
    @app_commands.describe(preset="Which predefined poll to post", dauer_stunden="Override duration in hours (optional)")
    @app_commands.choices(preset=_preset_choices())
    async def preset(self, interaction: discord.Interaction, preset: app_commands.Choice[str],
                     dauer_stunden: float | None = None):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        p = _preset_by_key(preset.value)
        if p is None:
            await interaction.response.send_message("That preset no longer exists.", ephemeral=True)
            return
        duration = dauer_stunden if dauer_stunden is not None else float(p.get("duration_hours", 24))
        await self._create_poll(interaction, p["question"], list(p["options"]), duration)

    @group.command(name="close", description="Close a poll early and tally the result.")
    @app_commands.describe(poll_id="The poll ID (from /poll list or the create confirmation)")
    async def close(self, interaction: discord.Interaction, poll_id: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        state = _load_state()
        poll = state["polls"].get(poll_id)
        if not poll:
            await interaction.response.send_message(f"No poll with ID `{poll_id}`.", ephemeral=True)
            return
        if poll.get("status") != "open":
            await interaction.response.send_message(f"Poll `{poll_id}` is already closed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._close_poll(poll_id, reason="manual")
        await interaction.followup.send(f"Poll `{poll_id}` closed and tallied.", ephemeral=True)

    @group.command(name="list", description="List running polls.")
    async def list_polls(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        state = _load_state()
        open_polls = [p for p in state["polls"].values()
                      if p.get("status") == "open" and p.get("guild_id") == interaction.guild_id]
        if not open_polls:
            await interaction.response.send_message("No running polls.", ephemeral=True)
            return
        embed = discord.Embed(title="Running polls", color=0x5865F2)
        for p in open_polls[:20]:
            closes = discord.utils.format_dt(datetime.fromtimestamp(p["end_ts"], timezone.utc), style="R")
            embed.add_field(
                name=f"`{p['id']}` — {p['question'][:80]}",
                value=f"{len(p['options'])} options · closes {closes}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Poll(bot))
