"""Community helpers for the modding support server:

- FAQ / canned answers: /faq, /faq-add, /faq-remove, /faq-list
  Saved answers to common questions (install steps, crash logs, MC version...).
  Stored per-guild in data/faqs.json.

- Suggestion voting: in the suggestions channel the bot auto-adds up/down
  reactions and opens a discussion thread for each new suggestion.

- /announce: post a clean embed announcement to a chosen channel as the bot.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.community")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
FAQ_FILE = DATA_DIR / "faqs.json"

UP = "\U0001F44D"    # thumbs up
DOWN = "\U0001F44E"  # thumbs down


def _load_faqs() -> dict:
    if not FAQ_FILE.exists():
        return {}
    try:
        return json.loads(FAQ_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read faqs file")
        return {}


def _save_faqs(data: dict) -> None:
    FAQ_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _guild_faqs(guild_id: int) -> dict:
    return _load_faqs().get(str(guild_id), {})


def _suggestions_config() -> dict:
    return SERVER_TEMPLATE.get("suggestions", {})


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- FAQ ----------

    faq_group = app_commands.Group(name="faq", description="Show saved answers to common questions.")

    async def _faq_key_autocomplete(self, interaction: discord.Interaction, current: str):
        faqs = _guild_faqs(interaction.guild_id or 0)
        cur = (current or "").lower()
        out = []
        for key in sorted(faqs.keys()):
            if cur in key.lower():
                out.append(app_commands.Choice(name=key, value=key))
            if len(out) >= 25:
                break
        return out

    @faq_group.command(name="show", description="Show a saved FAQ answer (everyone can see it).")
    @app_commands.describe(name="Which FAQ to show")
    @app_commands.autocomplete(name=_faq_key_autocomplete)
    async def faq_show(self, interaction: discord.Interaction, name: str):
        faqs = _guild_faqs(interaction.guild_id or 0)
        entry = faqs.get(name.lower())
        if not entry:
            available = ", ".join(sorted(faqs.keys())) or "(none yet)"
            await interaction.response.send_message(
                f"No FAQ named `{name}`. Available: {available}", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"FAQ: {name.lower()}",
            description=entry["answer"],
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed)

    @faq_group.command(name="add", description="Add or update a FAQ answer (admin).")
    @app_commands.describe(name="Short key, e.g. install", answer="The answer text")
    @app_commands.default_permissions(administrator=True)
    async def faq_add(self, interaction: discord.Interaction, name: str, answer: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load_faqs()
        g = str(interaction.guild_id)
        data.setdefault(g, {})[name.lower()] = {
            "answer": answer,
            "added_by": str(interaction.user),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _save_faqs(data)
        await interaction.response.send_message(f"Saved FAQ `{name.lower()}`.", ephemeral=True)

    @faq_group.command(name="remove", description="Delete a FAQ answer (admin).")
    @app_commands.autocomplete(name=_faq_key_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def faq_remove(self, interaction: discord.Interaction, name: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load_faqs()
        g = str(interaction.guild_id)
        if g in data and name.lower() in data[g]:
            del data[g][name.lower()]
            _save_faqs(data)
            await interaction.response.send_message(f"Deleted FAQ `{name.lower()}`.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No FAQ named `{name}`.", ephemeral=True)

    @faq_group.command(name="list", description="List all saved FAQ keys.")
    async def faq_list(self, interaction: discord.Interaction):
        faqs = _guild_faqs(interaction.guild_id or 0)
        if not faqs:
            await interaction.response.send_message("No FAQs saved yet. Add one with `/faq add`.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Saved FAQs",
            description="\n".join(f"`{k}`" for k in sorted(faqs.keys())),
            color=0x5865F2,
        )
        embed.set_footer(text="Show one with /faq show")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Announce ----------

    @app_commands.command(name="announce", description="Post an embed announcement to a channel as the bot (admin).")
    @app_commands.describe(channel="Target channel", title="Announcement title", message="Announcement body (use \\n for new lines)", ping_everyone="Ping @everyone?")
    @app_commands.default_permissions(administrator=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel,
                       title: str, message: str, ping_everyone: bool = False):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        embed = discord.Embed(
            title=title,
            description=message.replace("\\n", "\n"),
            color=0x57F287,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Announcement by {interaction.user.display_name}")
        content = "@everyone" if ping_everyone else None
        try:
            await channel.send(content=content, embed=embed,
                               allowed_mentions=discord.AllowedMentions(everyone=ping_everyone))
        except discord.Forbidden:
            await interaction.response.send_message(f"I can't post in {channel.mention}.", ephemeral=True)
            return
        await interaction.response.send_message(f"Announcement posted in {channel.mention}.", ephemeral=True)

    # ---------- Suggestion voting ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        cfg = _suggestions_config()
        if not cfg.get("enabled", True):
            return
        suggestions_name = CHANNELS.get("suggestions")
        if not suggestions_name or message.channel.name != suggestions_name:
            return
        # Auto up/down vote reactions
        try:
            await message.add_reaction(UP)
            await message.add_reaction(DOWN)
        except (discord.Forbidden, discord.HTTPException):
            pass
        # Optional discussion thread
        if cfg.get("create_threads", True):
            try:
                name = f"Suggestion by {message.author.display_name}"[:90]
                await message.create_thread(name=name, auto_archive_duration=cfg.get("thread_archive_minutes", 1440))
            except (discord.Forbidden, discord.HTTPException):
                pass


async def setup(bot):
    await bot.add_cog(Community(bot))
