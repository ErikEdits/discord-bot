"""Word filter: censors insults instead of deleting the whole message.

A message with a word from the list is deleted and posted again right away with the
same name and avatar (through a webhook), the bad words replaced by stars - so the
conversation keeps going, only the word is gone. Hidden spellings are caught too:
"h u r e", "h.u.r.e", "hur3", "huuure". Staff see the original in #mod-logs.

The list is small on purpose (strong insults and slurs only). Change it with
    /wordfilter add <word> / remove <word> / list      (administrator)
Words added there are stored in data/word_filter.json, on top of the default list.
Config: SERVER_TEMPLATE["word_filter"].
"""

import io
import logging
import re
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, feature_on, is_admin, load_json, mark_bot_delete, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.word_filter")

STATE_FILE = DATA_DIR / "word_filter.json"
WEBHOOK_NAME = "Word filter"
MAX_REUPLOAD = 8 * 1024 * 1024

DEFAULT_WORDS = [
    # German
    "hurensohn", "hure", "fotze", "wichser", "missgeburt", "spast", "schlampe", "neger", "kanake",
    # English
    "nigger", "nigga", "faggot", "retard", "cunt", "whore", "kys",
]

# Letters people swap in to get around filters.
LOOKALIKE = {
    "a": "a4@äàáâ", "e": "e3€éèê", "i": "i1!|íìî", "o": "o0öóòô", "u": "uüvúùû", "s": "s5$z",
    "t": "t7+", "g": "g9", "b": "b8", "l": "l1|", "c": "ck(", "k": "kc",
}
_SEP = r"[\W_]*"   # anything between the letters: spaces, dots, dashes, emojis ...


def _config() -> dict:
    return SERVER_TEMPLATE.get("word_filter", {})


def word_pattern(word: str) -> str:
    parts = []
    for ch in word.lower():
        chars = LOOKALIKE.get(ch, ch)
        parts.append("[" + re.escape(chars) + "]+")
    body = _SEP.join(parts)
    # Short words must stand alone ("kys" not in "skys"); longer ones also catch "hurensohns", "spasten".
    end = r"(?![^\W\d_])" if len(word) <= 3 else ""
    return r"(?<![^\W\d_])" + body + end


def build_regex(words) -> re.Pattern | None:
    words = sorted({w.strip().lower() for w in words if w.strip()}, key=len, reverse=True)
    if not words:
        return None
    return re.compile("|".join(word_pattern(w) for w in words), re.IGNORECASE)


def censor(text: str, regex: re.Pattern | None) -> tuple[str, list[str]]:
    """(censored text, the matched pieces)."""
    if regex is None or not text:
        return text, []
    found = []

    def stars(m):
        found.append(m.group(0))
        piece = m.group(0)
        return piece[0] + "\\*" * max(len(re.sub(r"[\W_]", "", piece)) - 1, 3)
    return regex.sub(stars, text), found


class WordFilter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = load_json(STATE_FILE)
        self.state.setdefault("added", [])
        self.state.setdefault("removed", [])
        self._regex = None
        self._webhooks: dict[int, discord.Webhook] = {}
        self.rebuild()

    def words(self) -> list[str]:
        base = [w for w in _config().get("words", DEFAULT_WORDS) if w not in self.state["removed"]]
        return sorted(set(base) | set(self.state["added"]))

    def rebuild(self) -> None:
        self._regex = build_regex(self.words())

    async def _webhook(self, channel) -> discord.Webhook | None:
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        hook = self._webhooks.get(parent.id)
        if hook is not None:
            return hook
        try:
            hooks = await parent.webhooks()
            hook = next((h for h in hooks if h.name == WEBHOOK_NAME and h.token), None)
            if hook is None:
                hook = await parent.create_webhook(name=WEBHOOK_NAME, reason="Word filter reposts")
        except (discord.HTTPException, AttributeError):
            return None
        self._webhooks[parent.id] = hook
        return hook

    async def _files(self, message: discord.Message) -> list[discord.File]:
        files = []
        async with aiohttp.ClientSession() as session:
            for att in message.attachments[:10]:
                if att.size > MAX_REUPLOAD:
                    continue
                try:
                    async with session.get(att.url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 200:
                            files.append(discord.File(io.BytesIO(await r.read()), filename=att.filename,
                                                      spoiler=att.is_spoiler()))
                except Exception:
                    continue
        return files

    async def check(self, message: discord.Message) -> None:
        if not _config().get("enabled", True) or message.guild is None or message.author.bot:
            return
        if message.webhook_id is not None or not isinstance(message.author, discord.Member):
            return
        censored, found = censor(message.content or "", self._regex)
        if not found:
            return
        # A message the link filter removes anyway must not come back through the webhook.
        lf = self.bot.get_cog("LinkFilter")
        if lf is not None and not message.author.guild_permissions.manage_messages:
            try:
                if await lf._violation(message):
                    return
            except Exception:
                pass
        hook = await self._webhook(message.channel)
        files = await self._files(message)
        mark_bot_delete(message.id)
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            return
        if hook is not None:
            kwargs = {"thread": message.channel} if isinstance(message.channel, discord.Thread) else {}
            try:
                await hook.send(censored[:2000], username=message.author.display_name[:80],
                                avatar_url=message.author.display_avatar.url, files=files,
                                allowed_mentions=discord.AllowedMentions.none(), **kwargs)
            except discord.HTTPException:
                log.exception("Couldn't repost the censored message")
        await self._log(message, found)

    async def _log(self, message: discord.Message, found: list[str]) -> None:
        try:
            from cogs.logging_cog import _get_log_channel, _safe_send
            ch = _get_log_channel(message.guild, "mod")
            if ch is None:
                return
            embed = discord.Embed(title="\U0001F910 Words censored", color=0xF1C40F, timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Member", value=f"{message.author.mention} ({message.author})", inline=False)
            embed.add_field(name="Channel", value=message.channel.mention, inline=True)
            embed.add_field(name="Words", value=", ".join(f"`{w}`" for w in found)[:1024], inline=True)
            embed.add_field(name="Original", value=(message.content or "")[:1000], inline=False)
            await _safe_send(ch, embed=embed)
        except Exception:
            log.exception("Couldn't log the censored message")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if feature_on("word_filter"):
            await self.check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content and feature_on("word_filter"):
            await self.check(after)

    group = app_commands.Group(name="wordfilter", description="Words the bot censors (administrator).",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @group.command(name="add", description="Censor a word (hidden spellings are caught automatically).")
    async def add(self, interaction: discord.Interaction, word: app_commands.Range[str, 2, 40]):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        word = word.strip().lower()
        if word in self.state["removed"]:
            self.state["removed"].remove(word)
        if word not in self.state["added"]:
            self.state["added"].append(word)
        save_json(STATE_FILE, self.state)
        self.rebuild()
        await interaction.response.send_message(f"✅ `{word}` is censored now.", ephemeral=True)

    @group.command(name="remove", description="Stop censoring a word.")
    async def remove(self, interaction: discord.Interaction, word: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        word = word.strip().lower()
        if word in self.state["added"]:
            self.state["added"].remove(word)
        elif word not in self.state["removed"]:
            self.state["removed"].append(word)
        save_json(STATE_FILE, self.state)
        self.rebuild()
        await interaction.response.send_message(f"`{word}` is no longer censored.", ephemeral=True)

    @group.command(name="list", description="Show the censored words (only you see them).")
    async def list_words(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.send_message("Censored: " + ", ".join(f"||{w}||" for w in self.words()),
                                                ephemeral=True)


async def setup(bot):
    await bot.add_cog(WordFilter(bot))
