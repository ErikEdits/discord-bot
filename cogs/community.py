"""Community helpers for the modding support server:

- FAQ / canned answers: /faq, /faq-add, /faq-remove, /faq-list
  Saved answers to common questions (install steps, crash logs, MC version...).
  Stored per-guild in data/faqs.json.

- Suggestions: every message in the suggestions channel is reposted by the bot
  as an embed (with attachments), gets up/down reactions and a discussion
  thread. Buttons under it set the status - Accept / In progress / Reject -
  and can only be used by members with the Administrator permission. The
  author gets a DM when the status changes. Stored in data/suggestions.json.

- /announce: post a clean embed announcement to a chosen channel as the bot.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import is_admin, load_json, mark_bot_delete, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.community")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
FAQ_FILE = DATA_DIR / "faqs.json"
SUGGESTIONS_FILE = DATA_DIR / "suggestions.json"
MAX_REUPLOAD_BYTES = 8 * 1024 * 1024

# status key -> (label shown in the embed, color, button emoji)
SUGGESTION_STATUS = {
    "open":     ("\U0001F5F3\uFE0F Open for voting", 0x5865F2, None),
    "accepted": ("\u2705 Accepted",                 0x57F287, "\u2705"),
    "progress": ("\U0001F6E0\uFE0F In progress",      0xF1C40F, "\U0001F6E0\uFE0F"),
    "rejected": ("\u274C Rejected",                 0xE74C3C, "\u274C"),
}

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


def _load_suggestions() -> dict:
    data = load_json(SUGGESTIONS_FILE)
    data.setdefault("next_number", 1)
    data.setdefault("suggestions", {})
    return data


def _suggestion_by_message(data: dict, message_id: int):
    for number, entry in data["suggestions"].items():
        if entry.get("message_id") == message_id:
            return number, entry
    return None, None


def _apply_status(embed: discord.Embed, status: str, by: discord.abc.User | None, reason: str) -> discord.Embed:
    label, color, _emoji = SUGGESTION_STATUS[status]
    embed.color = color
    value = label
    if by is not None:
        value += f" by {by.mention}"
    if reason:
        value += f"\n> {reason[:900]}"
    for i, field in enumerate(embed.fields):
        if field.name == "Status":
            embed.set_field_at(i, name="Status", value=value, inline=False)
            break
    else:
        embed.add_field(name="Status", value=value, inline=False)
    return embed


class SuggestionStatusModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="Reason / note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=900,
    )

    def __init__(self, message: discord.Message, status: str):
        super().__init__(title=f"Mark suggestion as: {SUGGESTION_STATUS[status][0]}"[:45])
        self.message = message
        self.status = status

    async def on_submit(self, interaction: discord.Interaction):
        data = _load_suggestions()
        number, entry = _suggestion_by_message(data, self.message.id)
        embed = self.message.embeds[0] if self.message.embeds else discord.Embed()
        embed = _apply_status(embed, self.status, interaction.user, self.reason.value.strip())
        await interaction.response.edit_message(embed=embed)
        if entry is None:
            return
        entry["status"] = self.status
        entry["status_by"] = interaction.user.id
        entry["status_reason"] = self.reason.value.strip()
        save_json(SUGGESTIONS_FILE, data)
        log.info("Suggestion #%s set to %s by %s", number, self.status, interaction.user)
        if not _suggestions_config().get("dm_author_on_status", True):
            return
        author = interaction.guild.get_member(entry.get("author_id", 0)) if interaction.guild else None
        if author is None:
            return
        label = SUGGESTION_STATUS[self.status][0]
        text = (f"Your suggestion **#{number}** on **{interaction.guild.name}** was marked as **{label}**.\n"
                f"> {entry.get('content', '')[:300]}\n")
        if self.reason.value.strip():
            text += f"\n**Note from the team:** {self.reason.value.strip()}\n"
        text += f"\n{self.message.jump_url}"
        try:
            await author.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass


class SuggestionStatusView(discord.ui.View):
    """Status buttons under each suggestion. Administrator only."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _set(self, interaction: discord.Interaction, status: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only members with the Administrator permission can change the status.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SuggestionStatusModal(interaction.message, status))

    @discord.ui.button(label="Accept", emoji="\u2705", style=discord.ButtonStyle.success, custom_id="suggestion:accepted")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "accepted")

    @discord.ui.button(label="In progress", emoji="\U0001F6E0\uFE0F", style=discord.ButtonStyle.primary,
                       custom_id="suggestion:progress")
    async def progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "progress")

    @discord.ui.button(label="Reject", emoji="\u274C", style=discord.ButtonStyle.danger, custom_id="suggestion:rejected")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "rejected")


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(SuggestionStatusView())

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

    # ---------- Suggestions ----------

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
        if not message.content.strip() and not message.attachments:
            return
        if self.bot.intents.message_content:
            try:
                await self._repost_suggestion(message)
                return
            except discord.Forbidden:
                log.warning("Missing permission to repost suggestions - falling back to reactions only")
            except Exception:
                log.exception("Failed to repost suggestion")
        await self._decorate(message, f"Suggestion by {message.author.display_name}")

    async def _decorate(self, message: discord.Message, thread_name: str) -> None:
        try:
            await message.add_reaction(UP)
            await message.add_reaction(DOWN)
        except (discord.Forbidden, discord.HTTPException):
            pass
        if _suggestions_config().get("create_threads", True):
            try:
                await message.create_thread(name=thread_name[:90],
                                            auto_archive_duration=_suggestions_config().get("thread_archive_minutes", 1440))
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _repost_suggestion(self, message: discord.Message) -> None:
        files = []
        image_name = None
        for attachment in message.attachments[:4]:
            if attachment.size > MAX_REUPLOAD_BYTES:
                continue
            files.append(await attachment.to_file())
            if image_name is None and (attachment.content_type or "").startswith("image/"):
                image_name = attachment.filename
        mark_bot_delete(message.id)
        await message.delete()  # raises Forbidden before anything is posted

        data = _load_suggestions()
        number = data["next_number"]
        data["next_number"] = number + 1
        save_json(SUGGESTIONS_FILE, data)

        author = message.author
        embed = discord.Embed(
            description=message.content[:4000] or "*(see attachment)*",
            color=SUGGESTION_STATUS["open"][1],
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=f"Suggestion #{number} by {author.display_name}", icon_url=author.display_avatar.url)
        if image_name:
            embed.set_image(url=f"attachment://{image_name}")
        _apply_status(embed, "open", None, "")
        embed.set_footer(text=f"Vote with {UP} / {DOWN} \u00b7 discuss in the thread")
        posted = await message.channel.send(embed=embed, files=files, view=SuggestionStatusView())

        data = _load_suggestions()
        data["suggestions"][str(number)] = {
            "message_id": posted.id,
            "channel_id": posted.channel.id,
            "author_id": author.id,
            "content": message.content[:1000],
            "status": "open",
            "created": datetime.now(timezone.utc).isoformat(),
        }
        save_json(SUGGESTIONS_FILE, data)
        await self._decorate(posted, f"Suggestion #{number} by {author.display_name}")
        log.info("Suggestion #%d by %s", number, author)


async def setup(bot):
    await bot.add_cog(Community(bot))
