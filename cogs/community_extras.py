"""Community extras: thank-you XP and emoji suggestions.

Thanks   "danke @Name", "thanks @Name", "thx" as a reply ... gives the thanked member
         `xp` XP (level system). The bot reacts with ✨. Limits: once per pair every
         10 minutes, max. 5 thanks per hour per person, never yourself or bots.
Emojis   /emoji-suggest name image -> a card in the suggestions channel with 👍 / 👎
         votes. An administrator clicks "Add" (the bot uploads the emoji) or "Reject".
         Images are kept in data/emoji_suggestions/ until decided (Discord's own
         attachment links expire).
Config: SERVER_TEMPLATE["community_extras"].
"""

import io
import logging
import re
import secrets
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, feature_on, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.community_extras")

EMOJI_FILE = DATA_DIR / "emoji_suggestions.json"
EMOJI_DIR = DATA_DIR / "emoji_suggestions"
THANKS = re.compile(r"(?i)(?<![a-z])(danke(?:sch[oö]n|sehr|e+)?|vielen dank|thanks?|thank you|thx|ty|tysm|merci)(?![a-z])")
PAIR_COOLDOWN = 600
PER_HOUR = 5
MAX_EMOJI_BYTES = 256 * 1024


def _config() -> dict:
    return SERVER_TEMPLATE.get("community_extras", {})


def _emojis() -> dict:
    data = load_json(EMOJI_FILE)
    data.setdefault("suggestions", {})
    return data


def emoji_embed(s: dict, status: str = "") -> discord.Embed:
    embed = discord.Embed(title=f"\U0001F600 Emoji suggestion: :{s['name']}:", color=0xF1C40F,
                          description=f"Suggested by <@{s['by']}>\n\U0001F44D **{len(s['up'])}**  \U0001F44E **{len(s['down'])}**")
    embed.set_thumbnail(url=f"attachment://{s['file']}")
    if status:
        embed.add_field(name="Decision", value=status, inline=False)
        embed.color = 0x57F287 if status.startswith("✅") else 0x95A5A6
    return embed


class EmojiButton(discord.ui.DynamicItem[discord.ui.Button], template=r"emojisug:(?P<action>up|down|add|no):(?P<sid>[0-9a-f]{8})"):
    LOOK = {"up": ("\U0001F44D", discord.ButtonStyle.secondary, None), "down": ("\U0001F44E", discord.ButtonStyle.secondary, None),
            "add": ("✅", discord.ButtonStyle.success, "Add"), "no": ("❌", discord.ButtonStyle.danger, "Reject")}

    def __init__(self, action: str, sid: str):
        self.action, self.sid = action, sid
        emoji, style, label = self.LOOK[action]
        super().__init__(discord.ui.Button(emoji=emoji, style=style, label=label, custom_id=f"emojisug:{action}:{sid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["sid"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("CommunityExtras")
        if cog is not None:
            await cog.emoji_button(interaction, self.action, self.sid)


def emoji_view(sid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("up", "down", "add", "no"):
        view.add_item(EmojiButton(action, sid))
    return view


class CommunityExtras(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pairs: dict[tuple[int, int], float] = {}
        self.given: dict[int, list[float]] = {}
        bot.add_dynamic_items(EmojiButton)

    # -------- Thanks XP -----------------------------------------------------------

    def thanked(self, message: discord.Message) -> list[discord.Member]:
        if not THANKS.search(message.content or ""):
            return []
        people = [m for m in message.mentions if isinstance(m, discord.Member)]
        ref = message.reference.resolved if message.reference else None
        if not people and isinstance(ref, discord.Message) and isinstance(ref.author, discord.Member):
            people = [ref.author]
        return [m for m in people if not m.bot and m.id != message.author.id][:3]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not _config().get("thanks_xp", True):
            return
        if not feature_on("thanks_xp"):
            return
        people = self.thanked(message)
        if not people:
            return
        levels = self.bot.get_cog("Levels")
        if levels is None:
            return
        now = time.time()
        recent = [t for t in self.given.get(message.author.id, []) if now - t < 3600]
        rewarded = False
        for member in people:
            if len(recent) >= PER_HOUR or now - self.pairs.get((message.author.id, member.id), 0) < PAIR_COOLDOWN:
                continue
            self.pairs[(message.author.id, member.id)] = now
            recent.append(now)
            await levels.add_xp(member, int(_config().get("thanks_xp_amount", 30)))
            rewarded = True
        self.given[message.author.id] = recent
        if rewarded:
            try:
                await message.add_reaction("✨")
            except discord.HTTPException:
                pass

    # -------- Emoji suggestions -----------------------------------------------------

    @app_commands.command(name="emoji-suggest", description="Suggest a new server emoji - everyone can vote on it.")
    @app_commands.describe(name="Emoji name, e.g. smite_happy", image="PNG, JPG or GIF, max. 256 KB")
    @app_commands.guild_only()
    async def emoji_suggest(self, interaction: discord.Interaction, name: app_commands.Range[str, 2, 32],
                            image: discord.Attachment):
        name = name.strip().strip(":")
        if not re.fullmatch(r"[A-Za-z0-9_]{2,32}", name):
            await interaction.response.send_message("The name may only contain letters, numbers and _.", ephemeral=True)
            return
        ext = image.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg", "gif", "webp") or image.size > MAX_EMOJI_BYTES:
            await interaction.response.send_message("Use a PNG, JPG, GIF or WEBP image of max. 256 KB.", ephemeral=True)
            return
        channel = discord.utils.get(interaction.guild.text_channels, name=CHANNELS.get("suggestions"))
        if channel is None:
            await interaction.response.send_message("The suggestions channel is missing.", ephemeral=True)
            return
        data = _emojis()
        if sum(1 for s in data["suggestions"].values() if s["by"] == interaction.user.id and s["status"] == "open") >= 3:
            await interaction.response.send_message("You already have 3 open emoji suggestions.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = await image.read()
        sid = secrets.token_hex(4)
        EMOJI_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{sid}.{ext}"
        (EMOJI_DIR / fname).write_bytes(raw)
        s = {"name": name, "by": interaction.user.id, "file": fname, "up": [], "down": [], "status": "open",
             "ts": time.time(), "guild": interaction.guild.id}
        try:
            msg = await channel.send(embed=emoji_embed(s), file=discord.File(io.BytesIO(raw), filename=fname),
                                     view=emoji_view(sid))
        except discord.HTTPException:
            await interaction.followup.send("Couldn't post the suggestion.", ephemeral=True)
            return
        s["message"] = msg.id
        data["suggestions"][sid] = s
        save_json(EMOJI_FILE, data)
        await interaction.followup.send(f"✅ Your emoji suggestion is up in {channel.mention}.", ephemeral=True)

    async def emoji_button(self, interaction: discord.Interaction, action: str, sid: str):
        data = _emojis()
        s = data["suggestions"].get(sid)
        if s is None or s["status"] != "open":
            await interaction.response.send_message("This suggestion is already decided.", ephemeral=True)
            return
        uid = interaction.user.id
        if action in ("up", "down"):
            other = "down" if action == "up" else "up"
            if uid in s[action]:
                s[action].remove(uid)
            else:
                s[action].append(uid)
                if uid in s[other]:
                    s[other].remove(uid)
            save_json(EMOJI_FILE, data)
            await interaction.response.edit_message(embed=emoji_embed(s))
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can add or reject emojis.", ephemeral=True)
            return
        path = EMOJI_DIR / s["file"]
        if action == "add":
            try:
                emoji = await interaction.guild.create_custom_emoji(name=s["name"], image=path.read_bytes(),
                                                                    reason=f"Emoji suggestion approved by {interaction.user}")
            except (discord.HTTPException, OSError) as exc:
                await interaction.response.send_message(f"Couldn't add the emoji: {exc}", ephemeral=True)
                return
            status = f"✅ Added as {emoji} by {interaction.user.mention}"
        else:
            status = f"❌ Rejected by {interaction.user.mention}"
        s["status"] = "added" if action == "add" else "rejected"
        save_json(EMOJI_FILE, data)
        path.unlink(missing_ok=True)
        await interaction.response.edit_message(embed=emoji_embed(s, status), view=None)
        member = interaction.guild.get_member(s["by"])
        if member is not None:
            try:
                await member.send(f"Your emoji suggestion :{s['name']}: was "
                                  f"{'added' if action == 'add' else 'not added'} on **{interaction.guild.name}**.")
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(CommunityExtras(bot))
