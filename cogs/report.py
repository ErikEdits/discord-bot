"""Report a message: right-click a message -> Apps -> "Report message".

The reporter gives a reason, then the team gets the report in the staff chat with
everything needed even if the message is deleted later:
  - a copy of the text (and of embed texts, where scam links often hide)
  - the attachments, re-uploaded
  - a picture of the message in Discord's look (name, avatar, time, text)
Buttons for the team: delete the message, timeout 1 h, warn, done. The reported
member is never told who reported them.

Config: SERVER_TEMPLATE["report"].
"""

import asyncio
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

from cogs.common import is_staff, mark_bot_delete
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.report")

COOLDOWN_SECONDS = 30
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
TIMEOUT_HOURS = 1


def _config() -> dict:
    return SERVER_TEMPLATE.get("report", {})


def _support_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


def _report_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=_config().get("channel", CHANNELS["staff_chat"]))


# -------- Picture of the message ---------------------------------------------

IMG_WIDTH = 900
PAD = 24
AVATAR = 56
TEXT_X = PAD + AVATAR + 18
BG = (49, 51, 56)
NAME_DEFAULT = (242, 243, 245)
TEXT = (219, 222, 225)
MUTED = (148, 155, 164)
LINK = (0, 168, 252)
EMBED_BG = (43, 45, 49)

_CUSTOM_EMOJI = re.compile(r"<a?(:\w+:)\d+>")


def _plain(text: str, guild: discord.Guild | None) -> str:
    """Mentions and custom emojis as people see them."""
    text = _CUSTOM_EMOJI.sub(r"\1", text)
    if guild is not None:
        text = re.sub(r"<@!?(\d+)>", lambda m: "@" + getattr(guild.get_member(int(m[1])), "display_name", "unknown"), text)
        text = re.sub(r"<#(\d+)>", lambda m: "#" + getattr(guild.get_channel(int(m[1])), "name", "unknown"), text)
        text = re.sub(r"<@&(\d+)>", lambda m: "@" + getattr(guild.get_role(int(m[1])), "name", "role"), text)
    return text


_NO_GLYPH = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]")


def _drawable(text: str) -> str:
    """The fonts have no emoji - leave them out of the picture instead of drawing boxes."""
    return _NO_GLYPH.sub("", text)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        line = ""
        for word in para.split(" "):
            candidate = (line + " " + word) if line else word
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # a single word longer than the line (e.g. a long link): break it
            while draw.textlength(word, font=font) > width:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def render_message(author_name: str, name_color: tuple, avatar: bytes | None, when: str, text: str,
                   embeds: list[str], attachment_names: list[str], image: bytes | None) -> bytes:
    """A PNG of the message in Discord's dark look."""
    from cogs.welcome_image import _font
    name_font, text_font, small = _font(24, True), _font(22), _font(17)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    author_name, text = _drawable(author_name) or "?", _drawable(text)
    embeds = [_drawable(e) for e in embeds]
    width = IMG_WIDTH - TEXT_X - PAD
    text_lines = _wrap(probe, text, text_font, width)[:60] if text else []
    embed_blocks = [_wrap(probe, e, small, width - 30)[:20] for e in embeds[:3]]
    pic = None
    if image:
        try:
            pic = Image.open(io.BytesIO(image)).convert("RGB")
            pic.thumbnail((width, 420))
        except Exception:
            pic = None
    line_h, small_h = 30, 24
    height = PAD + 34 + len(text_lines) * line_h
    for block in embed_blocks:
        height += 16 + len(block) * small_h + 16
    height += len(attachment_names) * small_h + (pic.height + 12 if pic else 0) + PAD
    height = max(height, PAD * 2 + AVATAR)

    img = Image.new("RGB", (IMG_WIDTH, height), BG)
    draw = ImageDraw.Draw(img)
    # avatar
    face = None
    if avatar:
        try:
            face = Image.open(io.BytesIO(avatar)).convert("RGB").resize((AVATAR, AVATAR))
        except Exception:
            face = None
    mask = Image.new("L", (AVATAR, AVATAR), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR - 1, AVATAR - 1), fill=255)
    if face is None:
        face = Image.new("RGB", (AVATAR, AVATAR), (88, 101, 242))
    img.paste(face, (PAD, PAD), mask)
    # name + time
    draw.text((TEXT_X, PAD - 2), author_name, font=name_font, fill=name_color)
    draw.text((TEXT_X + draw.textlength(author_name, font=name_font) + 12, PAD + 4), when, font=small, fill=MUTED)
    y = PAD + 34
    for line in text_lines:
        x = TEXT_X
        for i, part in enumerate(re.split(r"(https?://\S+)", line)):
            draw.text((x, y), part, font=text_font, fill=LINK if i % 2 else TEXT)
            x += draw.textlength(part, font=text_font)
        y += line_h
    for block in embed_blocks:
        top = y + 8
        bottom = top + 16 + len(block) * small_h
        draw.rectangle((TEXT_X, top, IMG_WIDTH - PAD, bottom), fill=EMBED_BG)
        draw.rectangle((TEXT_X, top, TEXT_X + 4, bottom), fill=(88, 101, 242))
        yy = top + 8
        for line in block:
            draw.text((TEXT_X + 18, yy), line, font=small, fill=TEXT)
            yy += small_h
        y = bottom + 8
    for name in attachment_names:
        draw.text((TEXT_X, y), "[file] " + name, font=small, fill=MUTED)
        y += small_h
    if pic is not None:
        img.paste(pic, (TEXT_X, y + 6))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# -------- Report flow ---------------------------------------------------------

def _embed_texts(message: discord.Message) -> list[str]:
    texts = []
    for e in message.embeds:
        parts = [p for p in (e.author.name if e.author else None, e.title, e.description, e.url) if p]
        parts += [f"{f.name}: {f.value}" for f in e.fields]
        if parts:
            texts.append("\n".join(parts))
    return texts


async def _download(url: str, limit: int) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.content.read(limit + 1)
                return data if len(data) <= limit else None
    except Exception:
        return None


class ReportModal(discord.ui.Modal, title="Report message"):
    reason = discord.ui.TextInput(label="What's wrong with this message?", style=discord.TextStyle.paragraph,
                                  min_length=3, max_length=500, placeholder="e.g. scam link, insult, spam")

    def __init__(self, cog: "Report", message: discord.Message):
        super().__init__()
        self.cog = cog
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await self.cog.send_report(interaction, self.message, str(self.reason.value).strip())
        await interaction.followup.send(
            "✅ Thanks - the team has been notified. The member won't see who reported them." if ok
            else "Sorry, the report couldn't be delivered. Please contact the team directly.", ephemeral=True)


class ReportAction(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"report:(?P<action>del|mute|warn|done):(?P<channel>\d+):(?P<message>\d+):(?P<author>\d+)"):
    """Buttons under a report. Work after a restart (the IDs are in the custom_id)."""

    LOOK = {
        "del": ("Delete message", "\U0001F5D1", discord.ButtonStyle.danger),
        "mute": (f"Timeout {TIMEOUT_HOURS} h", "⏳", discord.ButtonStyle.secondary),
        "warn": ("Warn", "⚠", discord.ButtonStyle.secondary),
        "done": ("Done", "✅", discord.ButtonStyle.success),
    }

    def __init__(self, action: str, channel_id: int, message_id: int, author_id: int):
        self.action, self.channel_id, self.message_id, self.author_id = action, channel_id, message_id, author_id
        label, emoji, style = self.LOOK[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"report:{action}:{channel_id}:{message_id}:{author_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["channel"]), int(match["message"]), int(match["author"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Report")
        if cog is not None:
            await cog.handle_action(interaction, self.action, self.channel_id, self.message_id, self.author_id)


def report_view(channel_id: int, message_id: int, author_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("del", "mute", "warn", "done"):
        view.add_item(ReportAction(action, channel_id, message_id, author_id))
    return view


class Report(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last: dict[int, float] = {}
        self._reported: set[tuple[int, int]] = set()
        self.menu = app_commands.ContextMenu(name="Report message", callback=self.report_menu)
        self.menu.guild_only = True
        bot.tree.add_command(self.menu)
        bot.add_dynamic_items(ReportAction)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    async def report_menu(self, interaction: discord.Interaction, message: discord.Message):
        if not _config().get("enabled", True):
            await interaction.response.send_message("Reporting is turned off.", ephemeral=True)
            return
        if message.author.id == interaction.user.id:
            await interaction.response.send_message("You can't report your own message.", ephemeral=True)
            return
        if message.author.id == getattr(self.bot.user, "id", None):
            await interaction.response.send_message("That's a message from the bot itself.", ephemeral=True)
            return
        if (interaction.user.id, message.id) in self._reported:
            await interaction.response.send_message("You already reported this message - the team has it.",
                                                    ephemeral=True)
            return
        wait = COOLDOWN_SECONDS - (time.monotonic() - self._last.get(interaction.user.id, 0))
        if wait > 0:
            await interaction.response.send_message(f"Please wait {int(wait) + 1} s before the next report.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self, message))

    async def send_report(self, interaction: discord.Interaction, message: discord.Message, reason: str) -> bool:
        guild = interaction.guild
        channel = _report_channel(guild) if guild else None
        if channel is None:
            log.warning("Report channel not found")
            return False
        self._last[interaction.user.id] = time.monotonic()
        self._reported.add((interaction.user.id, message.id))
        author = message.author
        text = _plain(message.content or "", guild)
        embeds = [_plain(t, guild) for t in _embed_texts(message)]

        files, names, first_image = [], [], None
        for att in message.attachments[:MAX_ATTACHMENTS]:
            names.append(att.filename)
            data = await _download(att.url, MAX_ATTACHMENT_BYTES) if att.size <= MAX_ATTACHMENT_BYTES else None
            if data is None:
                continue
            files.append(discord.File(io.BytesIO(data), filename=att.filename, spoiler=True))
            if first_image is None and (att.content_type or "").startswith("image/"):
                first_image = data
        avatar = await _download(author.display_avatar.replace(size=128, format="png").url, 2 * 1024 * 1024)
        color = getattr(author, "color", None)
        name_color = color.to_rgb() if color and color.value else NAME_DEFAULT
        when = message.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        try:
            png = await asyncio.to_thread(render_message, author.display_name, name_color, avatar, when, text,
                                          embeds, names, first_image)
            files.insert(0, discord.File(io.BytesIO(png), filename="message.png"))
        except Exception:
            log.exception("Couldn't draw the reported message")
            png = None

        embed = discord.Embed(title="\U0001F6A9 Message reported", color=0xED4245,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Author", value=f"{author.mention} (`{author}`, {author.id})", inline=False)
        embed.add_field(name="Channel", value=f"{message.channel.mention} - [jump to message]({message.jump_url})",
                        inline=False)
        embed.add_field(name="Reported by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Sent", value=discord.utils.format_dt(message.created_at, "f"), inline=True)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        copy = text + ("\n\n[embed]\n" + "\n\n".join(embeds) if embeds else "")
        if copy.strip():
            shown = copy if len(copy) <= 1000 else copy[:1000] + "..."
            embed.add_field(name="Message text (copy)", value=f"```{shown.replace('```', '`` `')}```", inline=False)
        if len(copy) > 1000:
            files.append(discord.File(io.BytesIO(copy.encode("utf-8")), filename="message.txt"))
        if names:
            embed.add_field(name="Attachments", value="\n".join(names)[:1024], inline=False)
        if png is not None:
            embed.set_image(url="attachment://message.png")
        embed.set_footer(text="The member isn't told who reported them.")
        try:
            await channel.send(embed=embed, files=files[:10], view=report_view(message.channel.id, message.id, author.id),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Couldn't post the report")
            return False
        try:
            from cogs.stats import record_event
            record_event(guild.id, "reports")
        except Exception:
            pass
        log.info("%s reported a message by %s in #%s", interaction.user, author, getattr(message.channel, "name", "?"))
        return True

    async def handle_action(self, interaction: discord.Interaction, action: str, channel_id: int, message_id: int,
                            author_id: int):
        guild = interaction.guild
        if guild is None or not is_staff(interaction.user, _support_roles()):
            await interaction.response.send_message("Only the team can handle reports.", ephemeral=True)
            return
        from cogs.moderation import add_warning, mod_action_embed, record_action, send_to_mod_logs
        member = guild.get_member(author_id)
        reason = "Reported message"
        if action == "done":
            await self._mark(interaction, f"✅ Closed by {interaction.user.mention}", close=True)
            return
        if action == "del":
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
            try:
                if channel is None:
                    raise discord.NotFound(type("R", (), {"status": 404, "reason": "gone"})(), "channel gone")
                mark_bot_delete(message_id)
                await channel.get_partial_message(message_id).delete()
                note = f"\U0001F5D1 Message deleted by {interaction.user.mention}"
            except discord.NotFound:
                note = f"\U0001F5D1 Message was already gone ({interaction.user.mention})"
            except discord.HTTPException:
                await interaction.response.send_message("Couldn't delete it - missing permissions?", ephemeral=True)
                return
            await self._mark(interaction, note)
            return
        if member is None:
            await interaction.response.send_message("That member isn't on the server anymore.", ephemeral=True)
            return
        if action == "mute":
            try:
                await member.timeout(timedelta(hours=TIMEOUT_HOURS), reason=f"{reason} (by {interaction.user})")
            except discord.HTTPException:
                await interaction.response.send_message("Couldn't time them out - missing permissions, or they "
                                                        "rank above the bot.", ephemeral=True)
                return
            record_action(guild.id, member.id, "timeout", interaction.user, reason, duration=f"{TIMEOUT_HOURS}h")
            await send_to_mod_logs(guild, mod_action_embed("Member Timed Out", 0xE67E22, member, interaction.user,
                                                           reason, {"Duration": f"{TIMEOUT_HOURS} h"}))
            await self._mark(interaction, f"⏳ {member.mention} timed out for {TIMEOUT_HOURS} h by "
                                          f"{interaction.user.mention}")
            return
        count = add_warning(guild.id, member, interaction.user, reason)
        await send_to_mod_logs(guild, mod_action_embed("Member Warned", 0xF39C12, member, interaction.user, reason,
                                                       {"Total warnings": str(count)}))
        try:
            await member.send(f"You were warned in **{guild.name}**.\nReason: {reason}")
        except discord.HTTPException:
            pass
        await self._mark(interaction, f"⚠ {member.mention} warned by {interaction.user.mention} "
                                      f"({count} warning(s) now)")

    @staticmethod
    async def _mark(interaction: discord.Interaction, note: str, close: bool = False):
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed is None:
            await interaction.response.send_message(note, ephemeral=True)
            return
        field = next((i for i, f in enumerate(embed.fields) if f.name == "Handled"), None)
        if field is None:
            embed.add_field(name="Handled", value=note, inline=False)
        else:
            embed.set_field_at(field, name="Handled", value=(embed.fields[field].value + "\n" + note)[-1024:],
                               inline=False)
        if close:
            embed.color = 0x57F287
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Report(bot))
