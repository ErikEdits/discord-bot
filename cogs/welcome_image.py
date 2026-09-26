"""Welcome image: when someone joins, the bot draws a card with their profile
picture, name and member number and posts it in CHANNELS["welcome"].

Rendering uses Pillow and runs in a worker thread so the bot never stalls.
Config: SERVER_TEMPLATE["welcome_image"].
"""

import asyncio
import io
import logging

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.welcome_image")

WIDTH, HEIGHT = 1100, 380
AVATAR_SIZE = 240
BG = (30, 31, 34)
CARD = (43, 45, 49)
WHITE = (242, 243, 245)
MUTED = (148, 155, 164)

_BOLD_FONTS = ["DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"]
_REGULAR_FONTS = ["DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"]


def _config() -> dict:
    return SERVER_TEMPLATE.get("welcome_image", {})


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for name in (_BOLD_FONTS if bold else _REGULAR_FONTS):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 ships a scalable default font
    except TypeError:
        return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _hex(value) -> tuple[int, int, int]:
    v = int(str(value).replace("#", "").replace("0x", ""), 16) if not isinstance(value, int) else value
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def render_card(avatar_bytes: bytes | None, display_name: str, username: str,
                guild_name: str, member_number: int, accent=0x5865F2) -> bytes:
    accent_rgb = _hex(accent)
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))

    # Rounded card with a soft accent glow in the top-left corner.
    base = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-220, -260, 520, 480), fill=accent_rgb + (90,))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    base = Image.alpha_composite(base, glow)
    ImageDraw.Draw(base).rectangle((0, HEIGHT - 10, WIDTH, HEIGHT), fill=accent_rgb + (255,))
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, WIDTH - 1, HEIGHT - 1), radius=36, fill=255)
    img.paste(base, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # Avatar in a circle with an accent ring.
    ax, ay = 70, (HEIGHT - AVATAR_SIZE) // 2 - 4
    ring = 8
    draw.ellipse((ax - ring, ay - ring, ax + AVATAR_SIZE + ring, ay + AVATAR_SIZE + ring), fill=accent_rgb + (255,))
    draw.ellipse((ax - 3, ay - 3, ax + AVATAR_SIZE + 3, ay + AVATAR_SIZE + 3), fill=BG + (255,))
    avatar = None
    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
        except Exception:
            avatar = None
    if avatar is None:
        avatar = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), CARD + (255,))
        d = ImageDraw.Draw(avatar)
        initial = (display_name or "?")[:1].upper()
        f = _font(110, bold=True)
        d.text((AVATAR_SIZE / 2, AVATAR_SIZE / 2), initial, font=f, fill=WHITE, anchor="mm")
    circle = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), fill=255)
    img.paste(avatar, (ax, ay), circle)

    # Text block.
    tx = ax + AVATAR_SIZE + 60
    max_w = WIDTH - tx - 50
    draw.text((tx, 78), "WELCOME", font=_font(34, bold=True), fill=accent_rgb + (255,))
    name_font = _font(68, bold=True)
    draw.text((tx, 118), _fit(draw, display_name, name_font, max_w), font=name_font, fill=WHITE)
    if username and username != display_name:
        user_font = _font(30)
        draw.text((tx, 200), _fit(draw, f"@{username}", user_font, max_w), font=user_font, fill=MUTED)
    info_font = _font(30)
    info = f"Member #{member_number:,}  ·  {guild_name}"
    draw.text((tx, 262), _fit(draw, info, info_font, max_w), font=info_font, fill=WHITE)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


class WelcomeImage(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not _config().get("enabled", True):
            return
        channel = discord.utils.get(member.guild.text_channels, name=CHANNELS.get("welcome", ""))
        if channel is None:
            return
        try:
            avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
        except (discord.HTTPException, ValueError):
            avatar_bytes = None
        try:
            png = await asyncio.to_thread(
                render_card, avatar_bytes, member.display_name, member.name, member.guild.name,
                member.guild.member_count or 0, _config().get("accent_color", 0x5865F2),
            )
        except Exception:
            log.exception("Failed to render welcome image for %s", member)
            return
        try:
            await channel.send(
                content=f"Hey {member.mention}, welcome to **{member.guild.name}**! \U0001F389",
                file=discord.File(io.BytesIO(png), filename="welcome.png"),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            log.warning("Could not post welcome image in #%s", channel.name)


async def setup(bot):
    await bot.add_cog(WelcomeImage(bot))
