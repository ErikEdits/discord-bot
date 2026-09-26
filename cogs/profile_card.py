"""Profile card image for /profile (not an extension - used by the levels cog).

Same look as the welcome image: avatar, name, level with progress bar, rank,
XP, messages, voice time, join date and up to 3 role badges.
"""

import io

from PIL import Image, ImageDraw, ImageFilter

from cogs.welcome_image import BG, CARD, MUTED, WHITE, _fit, _font, _hex

WIDTH, HEIGHT = 1100, 420
AVATAR = 200


def _stat(draw, x, y, label, value):
    draw.text((x, y), label.upper(), font=_font(20, bold=True), fill=MUTED)
    draw.text((x, y + 26), value, font=_font(34, bold=True), fill=WHITE)


def render_profile(avatar_bytes: bytes | None, display_name: str, username: str, accent,
                   level: int, xp_into: int, xp_needed: int, rank: int | None, total_xp: int,
                   messages: int, voice_minutes: int, joined: str, badges: list[tuple[str, int]]) -> bytes:
    accent_rgb = _hex(accent or 0x5865F2)
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    base = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-200, -260, 480, 420), fill=accent_rgb + (80,))
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(110)))
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, WIDTH - 1, HEIGHT - 1), radius=36, fill=255)
    img.paste(base, (0, 0), mask)
    draw = ImageDraw.Draw(img)

    # Avatar
    ax, ay = 56, 56
    draw.ellipse((ax - 7, ay - 7, ax + AVATAR + 7, ay + AVATAR + 7), fill=accent_rgb + (255,))
    draw.ellipse((ax - 2, ay - 2, ax + AVATAR + 2, ay + AVATAR + 2), fill=BG + (255,))
    avatar = None
    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR, AVATAR), Image.LANCZOS)
        except Exception:
            avatar = None
    if avatar is None:
        avatar = Image.new("RGBA", (AVATAR, AVATAR), CARD + (255,))
        ImageDraw.Draw(avatar).text((AVATAR / 2, AVATAR / 2), (display_name or "?")[:1].upper(),
                                    font=_font(96, bold=True), fill=WHITE, anchor="mm")
    circle = Image.new("L", (AVATAR, AVATAR), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, AVATAR - 1, AVATAR - 1), fill=255)
    img.paste(avatar, (ax, ay), circle)

    # Name + badges
    tx = ax + AVATAR + 50
    max_w = WIDTH - tx - 50
    name_font = _font(56, bold=True)
    draw.text((tx, 60), _fit(draw, display_name, name_font, max_w), font=name_font, fill=WHITE)
    draw.text((tx, 128), _fit(draw, f"@{username}", _font(26), max_w), font=_font(26), fill=MUTED)
    bx = tx
    badge_font = _font(20, bold=True)
    for name, color in badges[:3]:
        w = int(draw.textlength(name, font=badge_font)) + 28
        if bx + w > WIDTH - 50:
            break
        rgb = _hex(color) if color else MUTED
        draw.rounded_rectangle((bx, 172, bx + w, 204), radius=16, fill=CARD + (255,), outline=rgb + (255,), width=2)
        draw.text((bx + 14, 188), name, font=badge_font, fill=WHITE, anchor="lm")
        bx += w + 10

    # Level + progress bar
    draw.text((tx, 222), f"LEVEL {level}", font=_font(30, bold=True), fill=accent_rgb + (255,))
    rank_text = f"#{rank}" if rank else "-"
    draw.text((WIDTH - 50, 222), f"RANK {rank_text}", font=_font(30, bold=True), fill=WHITE, anchor="ra")
    bar_y, bar_h = 266, 18
    draw.rounded_rectangle((tx, bar_y, WIDTH - 50, bar_y + bar_h), radius=9, fill=CARD + (255,))
    frac = max(0.0, min(1.0, xp_into / xp_needed if xp_needed else 1.0))
    if frac > 0:
        draw.rounded_rectangle((tx, bar_y, tx + max(bar_h, int((WIDTH - 50 - tx) * frac)), bar_y + bar_h),
                               radius=9, fill=accent_rgb + (255,))
    draw.text((WIDTH - 50, bar_y + bar_h + 8), f"{xp_into:,} / {xp_needed:,} XP", font=_font(20), fill=MUTED, anchor="ra")

    # Stats row
    hours, minutes = divmod(voice_minutes, 60)
    voice = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    sy = 318
    col = (WIDTH - 50 - 56) // 4
    _stat(draw, 56, sy, "Total XP", f"{total_xp:,}")
    _stat(draw, 56 + col, sy, "Messages", f"{messages:,}")
    _stat(draw, 56 + col * 2, sy, "Voice time", voice)
    _stat(draw, 56 + col * 3, sy, "Joined", joined)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
