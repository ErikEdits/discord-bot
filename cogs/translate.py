"""Translate a message: right-click a message -> Apps -> "Translate".

Only you see the answer. It's translated into your Discord language; if the message
already is in your language, into English (or German, if your Discord is English).
Uses Google Translate's free web endpoint - no key needed. Google can refuse it now
and then (too many requests from the host); the bot then says so.
"""

import asyncio
import json
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("setup-bot.translate")

MAX_CHARS = 4000
CACHE_SECONDS = 600
ENDPOINTS = (
    "https://translate.googleapis.com/translate_a/single?client=gtx&dt=t",
    "https://clients5.google.com/translate_a/t?client=dict-chrome-ex",
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36"}

LANGUAGES = {
    "de": "German", "en": "English", "ja": "Japanese", "fr": "French", "es": "Spanish", "it": "Italian",
    "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "cs": "Czech", "hu": "Hungarian",
    "ro": "Romanian", "el": "Greek", "bg": "Bulgarian", "hr": "Croatian", "lt": "Lithuanian", "ko": "Korean",
    "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)", "zh": "Chinese", "ar": "Arabic",
    "hi": "Hindi", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "he": "Hebrew", "iw": "Hebrew",
}

_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")


class TranslateError(Exception):
    pass


def target_language(locale) -> str:
    """Discord locale (de, en-US, pt-BR, zh-TW ...) -> Google language code."""
    value = str(getattr(locale, "value", locale) or "en")
    if value in ("zh-CN", "zh-TW"):
        return value
    return value.split("-")[0].lower() or "en"


def language_name(code: str) -> str:
    return LANGUAGES.get(code) or LANGUAGES.get(code.split("-")[0]) or code


def message_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for e in message.embeds:
        parts += [p for p in (e.title, e.description) if p]
        parts += [f"{f.name}: {f.value}" for f in e.fields]
    text = "\n\n".join(p for p in parts if p.strip())
    guild = message.guild
    text = _CUSTOM_EMOJI.sub(r":\1:", text)
    if guild is not None:
        text = re.sub(r"<@!?(\d+)>", lambda m: "@" + getattr(guild.get_member(int(m[1])), "display_name", "someone"), text)
        text = re.sub(r"<#(\d+)>", lambda m: "#" + getattr(guild.get_channel(int(m[1])), "name", "channel"), text)
        text = re.sub(r"<@&(\d+)>", lambda m: "@" + getattr(guild.get_role(int(m[1])), "name", "role"), text)
    return text.strip()


def parse_gtx(data) -> tuple[str, str]:
    """translate_a/single: [[["Hallo","Hello",...], ...], null, "en", ...]"""
    text = "".join(seg[0] for seg in data[0] if seg and isinstance(seg[0], str))
    return text, data[2] if len(data) > 2 and isinstance(data[2], str) else ""


def parse_dict(data) -> tuple[str, str]:
    """translate_a/t: [["Hallo","en"]] or ["Hallo","en"] or ["Hallo"]"""
    first = data[0]
    if isinstance(first, list):
        return first[0], first[1] if len(first) > 1 else ""
    return first, data[1] if len(data) > 1 and isinstance(data[1], str) else ""


async def google_translate(text: str, target: str, session: aiohttp.ClientSession) -> tuple[str, str]:
    """(translated text, detected source language). Tries both free endpoints."""
    last = "no answer"
    for index, base in enumerate(ENDPOINTS):
        try:
            async with session.post(base, params={"sl": "auto", "tl": target}, data={"q": text},
                                    headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.text()
                if resp.status != 200:
                    last = f"HTTP {resp.status}"
                    continue
            data = json.loads(body)
            result = parse_gtx(data) if index == 0 else parse_dict(data)
            if result[0]:
                return result
            last = "empty answer"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, IndexError, TypeError, KeyError) as exc:
            last = type(exc).__name__
    raise TranslateError(last)


def _chunks(text: str, size: int = 1800) -> list[str]:
    out, current = [], ""
    for para in text.split("\n"):
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        if len(current) + len(para) + 1 > size and current:
            out.append(current)
            current = ""
        current = (current + "\n" + para) if current else para
    if current:
        out.append(current)
    return out


class Translate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cache: dict[tuple[int, str], tuple[float, str, str, str]] = {}
        self.menu = app_commands.ContextMenu(name="Translate", callback=self.translate_menu)
        bot.tree.add_command(self.menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    async def translate(self, text: str, target: str) -> tuple[str, str, str]:
        """(translation, source language, target language actually used)."""
        async with aiohttp.ClientSession() as session:
            pieces = _chunks(text[:MAX_CHARS])
            first, source = await google_translate(pieces[0], target, session)
            if source and source.split("-")[0] == target.split("-")[0]:
                # Already in your language -> English (or German for English speakers).
                target = "de" if target == "en" else "en"
                first, source = await google_translate(pieces[0], target, session)
            rest = [(await google_translate(p, target, session))[0] for p in pieces[1:]]
        return "\n".join([first] + rest), source, target

    async def translate_menu(self, interaction: discord.Interaction, message: discord.Message):
        text = message_text(message)
        if not text:
            await interaction.response.send_message("There's no text in this message to translate.", ephemeral=True)
            return
        wanted = target_language(interaction.locale)
        key = (message.id, wanted)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS and not message.edited_at:
            _, translated, source, target = cached
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                translated, source, target = await self.translate(text, wanted)
            except TranslateError as exc:
                log.warning("Translation failed: %s", exc)
                await interaction.followup.send("Google Translate didn't answer right now - please try again in "
                                                "a minute.", ephemeral=True)
                return
            self._cache[key] = (time.time(), translated, source, target)
            if len(self._cache) > 500:
                for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:250]:
                    self._cache.pop(k, None)
        embed = discord.Embed(description=translated[:4000], color=0x4285F4)
        embed.set_author(name=f"{message.author.display_name} - {language_name(source) if source else 'auto'} "
                              f"→ {language_name(target)}",
                         icon_url=message.author.display_avatar.url)
        embed.set_footer(text="Google Translate" + (" - shortened, the message is very long" if len(text) > MAX_CHARS else ""))
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Original message", url=message.jump_url))
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Translate(bot))
