"""Video showcase: new YouTube videos about the mods, published after an admin's OK.

    /showcase add <youtube channel link or @handle> [all_videos]   follow a channel
    /showcase remove <channel> / list                                (administrator)

The bot checks the followed channels' feeds (no API key needed). A new video whose
title or description names one of the owner's mods (or any video, with all_videos)
goes to the admin-only review channel with "Publish" / "Ignore". Publish posts it in
the public showcase channel. Both channels are created if missing.
State: data/showcase.json.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, feature_on, is_admin, load_json, save_json
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.showcase")

STATE_FILE = DATA_DIR / "showcase.json"
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
CHANNEL_ID = re.compile(r"(UC[\w-]{22})")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0", "Accept-Language": "en"}


def _config() -> dict:
    return SERVER_TEMPLATE.get("showcase", {})


def _load() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("channels", {})   # yt channel id -> {name, all, seen: [video ids]}
    data.setdefault("videos", {})     # video id -> {title, url, author, mod, status}
    return data


def parse_feed(xml_text: str) -> tuple[str, list[dict]]:
    root = ET.fromstring(xml_text)
    name = root.findtext("a:title", default="", namespaces=NS)
    videos = []
    for e in root.findall("a:entry", NS):
        videos.append({
            "id": e.findtext("yt:videoId", default="", namespaces=NS),
            "title": e.findtext("a:title", default="", namespaces=NS),
            "url": (e.find("a:link", NS).get("href") if e.find("a:link", NS) is not None else ""),
            "author": e.findtext("a:author/a:name", default=name, namespaces=NS),
            "published": e.findtext("a:published", default="", namespaces=NS),
            "description": e.findtext("media:group/media:description", default="", namespaces=NS),
        })
    return name, videos


def matching_mod(video: dict, projects: list[dict]) -> str | None:
    text = f" {video['title']} {video['description']} ".lower()
    flat = re.sub(r"[^a-z0-9]", "", text)
    for p in projects:
        title = (p.get("title") or "").lower()
        slug = (p.get("slug") or "").lower()
        if (title and len(title) >= 4 and (title in text or re.sub(r"[^a-z0-9]", "", title) in flat)) or \
                (slug and len(slug) >= 5 and slug in text):
            return p.get("title") or slug
    return None


class ShowcaseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"showcase:(?P<action>pub|ign):(?P<vid>[\w-]{6,20})"):
    def __init__(self, action: str, vid: str):
        self.action, self.vid = action, vid
        super().__init__(discord.ui.Button(
            label="Publish" if action == "pub" else "Ignore", emoji="\U0001F4E2" if action == "pub" else "\U0001F5D1",
            style=discord.ButtonStyle.success if action == "pub" else discord.ButtonStyle.secondary,
            custom_id=f"showcase:{action}:{vid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["vid"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Showcase")
        if cog is not None:
            await cog.decide(interaction, self.vid, self.action == "pub")


class Showcase(commands.Cog):
    group = app_commands.Group(name="showcase", description="YouTube videos about the mods (administrator).",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(ShowcaseButton)
        self.loop.change_interval(minutes=int(_config().get("check_minutes", 60)))
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def _fetch(self, url: str) -> str | None:
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    return await r.text() if r.status == 200 else None
        except Exception:
            return None

    async def resolve(self, text: str) -> str | None:
        """YouTube channel id from a link, @handle or id."""
        text = text.strip()
        m = CHANNEL_ID.search(text)
        if m and "/watch" not in text:
            return m.group(1)
        if text.startswith("@"):
            text = f"https://www.youtube.com/{text}"
        elif not text.startswith("http"):
            text = f"https://www.youtube.com/@{text}"
        page = await self._fetch(text)
        if not page:
            return None
        m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', page) or \
            re.search(r'youtube\.com/channel/(UC[\w-]{22})', page)
        return m.group(1) if m else None

    async def _channel(self, guild: discord.Guild, key: str) -> discord.TextChannel | None:
        name = CHANNELS[key]
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch is not None:
            return ch
        if key == "showcase_review":
            anchor = discord.utils.get(guild.text_channels, name=CHANNELS.get("admin_only"))
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
            for rname in ("Admin", "Owner"):
                if (role := discord.utils.get(guild.roles, name=rname)) is not None:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                   read_message_history=True)
            topic = "New YouTube videos about the mods - Publish or Ignore (admins only)."
        else:
            anchor = discord.utils.get(guild.text_channels, name=CHANNELS.get("mod_releases"))
            overwrites = {guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
            topic = "Videos about the mods from the community."
        try:
            return await guild.create_text_channel(name, category=anchor.category if anchor else None,
                                                   overwrites=overwrites, topic=topic, reason="Video showcase")
        except discord.HTTPException:
            log.exception("Couldn't create #%s", name)
            return None

    @tasks.loop(minutes=60)
    async def loop(self):
        if not _config().get("enabled", True) or not feature_on("showcase"):
            return
        data = _load()
        if not data["channels"]:
            return
        cog = self.bot.get_cog("Modrinth")
        projects = await cog._cached_projects() if cog is not None and hasattr(cog, "_cached_projects") else []
        new = []
        for cid, info in data["channels"].items():
            xml_text = await self._fetch(FEED.format(cid))
            if not xml_text:
                continue
            try:
                name, videos = parse_feed(xml_text)
            except ET.ParseError:
                continue
            info["name"] = name or info.get("name", cid)
            first = not info.get("seen")
            seen = set(info.get("seen", []))
            for v in videos:
                if not v["id"] or v["id"] in seen:
                    continue
                seen.add(v["id"])
                if first:
                    continue   # the first check only remembers what's already there
                mod = matching_mod(v, projects)
                if mod or info.get("all"):
                    new.append(dict(v, mod=mod))
            info["seen"] = list(seen)[-200:]
        for v in new:
            data["videos"][v["id"]] = {"title": v["title"], "url": v["url"], "author": v["author"], "mod": v["mod"],
                                       "status": "pending"}
        save_json(STATE_FILE, data)
        for v in new:
            for guild in self.bot.guilds:
                await self._review(guild, v)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _review(self, guild: discord.Guild, v: dict) -> None:
        ch = await self._channel(guild, "showcase_review")
        if ch is None:
            return
        embed = discord.Embed(title=v["title"][:256], url=v["url"], color=0xFF0000,
                              description=f"by **{v['author']}**" + (f" - about **{v['mod']}**" if v.get("mod") else ""))
        if v.get("published"):
            try:
                embed.timestamp = datetime.fromisoformat(v["published"])
            except ValueError:
                pass
        view = discord.ui.View(timeout=None)
        view.add_item(ShowcaseButton("pub", v["id"]))
        view.add_item(ShowcaseButton("ign", v["id"]))
        try:
            await ch.send(content=v["url"], embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Couldn't post a video for review")

    async def decide(self, interaction: discord.Interaction, vid: str, publish: bool):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can decide this.", ephemeral=True)
            return
        data = _load()
        v = data["videos"].get(vid)
        if v is None or v["status"] != "pending":
            await interaction.response.edit_message(view=None)
            return
        if publish:
            ch = await self._channel(interaction.guild, "showcase")
            if ch is None:
                await interaction.response.send_message("Couldn't find or create the showcase channel.", ephemeral=True)
                return
            await ch.send(f"\U0001F3AC **{v['title']}** by **{v['author']}**"
                          + (f" - about **{v['mod']}**" if v.get("mod") else "") + f"\n{v['url']}")
        v["status"] = "published" if publish else "ignored"
        v["by"] = str(interaction.user)
        save_json(STATE_FILE, data)
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        verdict = "\U0001F4E2 Published" if publish else "Ignored"
        embed.add_field(name="Decision", value=f"{verdict} by {interaction.user.mention}", inline=False)
        embed.color = 0x57F287 if publish else 0x95A5A6
        await interaction.response.edit_message(embed=embed, view=None)

    @group.command(name="add", description="Follow a YouTube channel for videos about the mods.")
    @app_commands.describe(channel="Channel link, @handle or channel id",
                           all_videos="Show every new video of this channel, not only ones naming a mod")
    async def add(self, interaction: discord.Interaction, channel: str, all_videos: bool = False):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cid = await self.resolve(channel)
        xml_text = await self._fetch(FEED.format(cid)) if cid else None
        if not xml_text:
            await interaction.followup.send("I couldn't find that YouTube channel. Try the full channel link.",
                                            ephemeral=True)
            return
        name, videos = parse_feed(xml_text)
        data = _load()
        data["channels"][cid] = {"name": name, "all": all_videos, "seen": [v["id"] for v in videos]}
        save_json(STATE_FILE, data)
        await interaction.followup.send(f"✅ Following **{name}**. New videos "
                                        f"{'' if all_videos else 'naming one of the mods '}go to "
                                        f"#{CHANNELS['showcase_review']} for your OK.", ephemeral=True)

    @group.command(name="remove", description="Stop following a YouTube channel.")
    async def remove(self, interaction: discord.Interaction, channel: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        data = _load()
        key = next((cid for cid, i in data["channels"].items()
                    if cid == channel or i.get("name", "").lower() == channel.lower().lstrip("@")), None)
        if key is None:
            m = CHANNEL_ID.search(channel)
            key = m.group(1) if m and m.group(1) in data["channels"] else None
        if key is None:
            await interaction.response.send_message("I don't follow that channel.", ephemeral=True)
            return
        name = data["channels"].pop(key).get("name", key)
        save_json(STATE_FILE, data)
        await interaction.response.send_message(f"Stopped following **{name}**.", ephemeral=True)

    @remove.autocomplete("channel")
    async def _remove_ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=i.get("name", cid)[:100], value=cid)
                for cid, i in _load()["channels"].items() if current.lower() in i.get("name", cid).lower()][:25]

    @group.command(name="list", description="Followed YouTube channels.")
    async def list_channels(self, interaction: discord.Interaction):
        rows = [f"- **{i.get('name', cid)}**" + (" (all videos)" if i.get("all") else "")
                for cid, i in _load()["channels"].items()]
        await interaction.response.send_message("\n".join(rows) or "No channels yet - /showcase add.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Showcase(bot))
