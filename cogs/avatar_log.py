"""Profile picture changes -> CHANNELS["avatar_logs"] (administrators only).

When a member changes their profile picture, the bot posts who it was with a
before / after image. Covers both the normal Discord profile picture
(on_user_update) and a server-specific one (on_member_update, guild avatar).

The before/after image is drawn by the bot and uploaded, so the old picture stays
visible in the log even after Discord removes it from its servers.
If the log channel is missing, it's created at start in the LOGS category, visible only to
Admin and Owner (SERVER_TEMPLATE overwrites "AVATAR_LOG_OVERWRITES").
Config: SERVER_TEMPLATE["avatar_log"].
"""

import asyncio
import io
import logging

import discord
from discord.ext import commands
from PIL import Image, ImageDraw

from cogs.welcome_image import BG, CARD, MUTED, WHITE, _fit, _font
from server_template import AVATAR_LOG_OVERWRITES, CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.avatar_log")

WIDTH, HEIGHT = 900, 400
AVATAR = 240
BEFORE_COLOR = (237, 66, 69)
AFTER_COLOR = (87, 242, 135)


def _config() -> dict:
    return SERVER_TEMPLATE.get("avatar_log", {})


def _avatar_image(data: bytes | None, label: str) -> Image.Image:
    if data:
        try:
            return Image.open(io.BytesIO(data)).convert("RGBA").resize((AVATAR, AVATAR), Image.LANCZOS)
        except Exception:
            pass
    img = Image.new("RGBA", (AVATAR, AVATAR), CARD + (255,))
    draw = ImageDraw.Draw(img)
    draw.text((AVATAR / 2, AVATAR / 2 - 14), "?", font=_font(90, bold=True), fill=MUTED, anchor="mm")
    draw.text((AVATAR / 2, AVATAR / 2 + 52), label, font=_font(20), fill=MUTED, anchor="mm")
    return img


def render_change(before: bytes | None, after: bytes | None, name: str) -> bytes:
    """Before -> after image: two round avatars with labels and an arrow."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(img)
    draw.text((WIDTH / 2, 36), _fit(draw, name, _font(32, bold=True), WIDTH - 80), font=_font(32, bold=True),
              fill=WHITE, anchor="mm")
    top = 80
    slots = ((90, before, "BEFORE", BEFORE_COLOR, "not available"),
             (WIDTH - 90 - AVATAR, after, "AFTER", AFTER_COLOR, "no picture"))
    mask = Image.new("L", (AVATAR, AVATAR), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR - 1, AVATAR - 1), fill=255)
    for x, data, label, color, missing in slots:
        ring = 7
        draw.ellipse((x - ring, top - ring, x + AVATAR + ring, top + AVATAR + ring), fill=color + (255,))
        img.paste(_avatar_image(data, missing), (x, top), mask)
        draw.text((x + AVATAR / 2, top + AVATAR + 42), label, font=_font(28, bold=True), fill=color, anchor="mm")
    # Arrow between the two pictures.
    cy = top + AVATAR // 2
    x1, x2 = 90 + AVATAR + 40, WIDTH - 90 - AVATAR - 40
    draw.line((x1, cy, x2 - 18, cy), fill=WHITE, width=8)
    draw.polygon([(x2, cy), (x2 - 34, cy - 24), (x2 - 34, cy + 24)], fill=WHITE)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


async def _read(asset: discord.Asset | None) -> bytes | None:
    if asset is None:
        return None
    try:
        return await asset.replace(size=256, format="png").read()
    except (discord.HTTPException, discord.NotFound, ValueError):
        return None


class AvatarLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    async def cog_load(self):
        asyncio.create_task(self._ensure_channels(), name="avatar-log-setup")

    async def _ensure_channels(self):
        """Create the log channel right after start, not only on the first change."""
        await self.bot.wait_until_ready()
        if _config().get("enabled", True):
            for guild in self.bot.guilds:
                await self._channel(guild)

    async def _channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        name = CHANNELS["avatar_logs"]
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is not None:
            return channel
        async with self._lock:  # two changes at once must not create two channels
            channel = discord.utils.get(guild.text_channels, name=name)
            if channel is not None:
                return channel
            overwrites = {guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                 embed_links=True, attach_files=True)}
            for role_name, perms in AVATAR_LOG_OVERWRITES.items():
                role = guild.default_role if role_name == "@everyone" else discord.utils.get(guild.roles, name=role_name)
                if role is not None:
                    overwrites[role] = discord.PermissionOverwrite(**perms)
            category = discord.utils.find(lambda c: c.name.endswith("LOGS"), guild.categories)
            try:
                channel = await guild.create_text_channel(
                    name, category=category, overwrites=overwrites,
                    topic="Profile picture changes (before / after). Administrators only.",
                    reason="Avatar log channel")
                log.info("Created avatar log channel in %s", guild.name)
                return channel
            except discord.HTTPException:
                log.warning("Couldn't create the avatar log channel in %s", guild.name)
                return None

    async def _post(self, member: discord.Member, before: discord.Asset | None, after: discord.Asset | None,
                    kind: str) -> None:
        channel = await self._channel(member.guild)
        if channel is None:
            return
        before_bytes, after_bytes = await asyncio.gather(_read(before), _read(after))
        png = await asyncio.to_thread(render_change, before_bytes, after_bytes, member.display_name)
        embed = discord.Embed(
            title="\U0001F5BC️ Profile picture changed",
            description=(f"{member.mention} **{discord.utils.escape_markdown(member.display_name)}** "
                         f"(`@{member.name}`)\n{kind}"),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        links = [f"[Before]({before.url})" if before else "Before: none",
                 f"[After]({after.url})" if after else "After: none (removed)"]
        embed.add_field(name="Pictures", value=" · ".join(links), inline=False)
        embed.set_image(url="attachment://avatar-change.png")
        embed.set_footer(text=f"User ID {member.id}")
        try:
            await channel.send(embed=embed, file=discord.File(io.BytesIO(png), filename="avatar-change.png"),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Couldn't post avatar change of %s in #%s", member, channel.name)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if not _config().get("enabled", True) or after.bot or before.avatar == after.avatar:
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is not None:
                # before.display_avatar is the default avatar when there was no picture.
                await self._post(member, before.display_avatar, after.avatar or after.default_avatar,
                                 "changed their **Discord profile picture**")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        cfg = _config()
        if (not cfg.get("enabled", True) or not cfg.get("server_avatars", True) or after.bot
                or before.guild_avatar == after.guild_avatar):
            return
        # Without a server picture the normal profile picture is shown.
        await self._post(after, before.guild_avatar or before.display_avatar, after.display_avatar,
                         "changed their **server profile picture**" if after.guild_avatar
                         else "removed their **server profile picture**")


async def setup(bot):
    await bot.add_cog(AvatarLog(bot))
