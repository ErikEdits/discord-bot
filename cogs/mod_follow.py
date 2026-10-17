"""Follow mods: get a DM when a mod you follow gets an update.

    /follow      pick your mods from a menu (only you see it) - pick again to change
    /following   which mods you follow

The DM has the release info and an "Unfollow" button. Stored in data/mod_follow.json.
Triggered by the "mod_release" event from cogs/modrinth.py.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, feature_on, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.mod_follow")

STATE_FILE = DATA_DIR / "mod_follow.json"


def _load() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("users", {})   # user id -> [project ids]
    return data


async def _projects(bot) -> list[dict]:
    cog = bot.get_cog("Modrinth")
    try:
        if cog is not None and hasattr(cog, "_cached_projects"):
            return await cog._cached_projects() or []
        from cogs.modrinth import _fetch_user_projects
        return await _fetch_user_projects(SERVER_TEMPLATE.get("modrinth", {}).get("username", "")) or []
    except Exception:
        return []


class FollowSelect(discord.ui.Select):
    def __init__(self, projects: list[dict], current: list[str]):
        options = [discord.SelectOption(label=(p.get("title") or p.get("slug") or "?")[:100], value=p["id"],
                                        description=(p.get("description") or "")[:100] or None,
                                        default=p["id"] in current) for p in projects[:25]]
        super().__init__(placeholder="Pick the mods you want update DMs for", min_values=0,
                         max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        data = _load()
        if self.values:
            data["users"][str(interaction.user.id)] = list(self.values)
        else:
            data["users"].pop(str(interaction.user.id), None)
        save_json(STATE_FILE, data)
        names = [o.label for o in self.options if o.value in self.values]
        await interaction.response.edit_message(
            content=("\U0001F514 You get a DM when these get an update: " + ", ".join(f"**{n}**" for n in names))
            if names else "You don't follow any mod now.", view=None)


class UnfollowButton(discord.ui.DynamicItem[discord.ui.Button], template=r"unfollow:(?P<pid>[\w]+)"):
    def __init__(self, project_id: str):
        self.project_id = project_id
        super().__init__(discord.ui.Button(label="Unfollow", emoji="\U0001F515", style=discord.ButtonStyle.secondary,
                                           custom_id=f"unfollow:{project_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        data = _load()
        mine = data["users"].get(str(interaction.user.id), [])
        if self.project_id in mine:
            mine.remove(self.project_id)
            if not mine:
                data["users"].pop(str(interaction.user.id), None)
            save_json(STATE_FILE, data)
        await interaction.response.send_message("\U0001F515 You won't get update DMs for this mod anymore. "
                                                "Use /follow on the server to change it.")


class ModFollow(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(UnfollowButton)

    @app_commands.command(name="follow", description="Get a DM when one of the mods gets an update.")
    @app_commands.guild_only()
    async def follow(self, interaction: discord.Interaction):
        projects = await _projects(self.bot)
        if not projects:
            await interaction.response.send_message("I can't load the mod list right now - try again later.",
                                                    ephemeral=True)
            return
        current = _load()["users"].get(str(interaction.user.id), [])
        view = discord.ui.View(timeout=300)
        view.add_item(FollowSelect(projects, current))
        await interaction.response.send_message("Which mods do you want update DMs for?", view=view, ephemeral=True)

    @app_commands.command(name="following", description="Which mods you get update DMs for.")
    async def following(self, interaction: discord.Interaction):
        ids = _load()["users"].get(str(interaction.user.id), [])
        projects = {p["id"]: p for p in await _projects(self.bot)}
        names = [projects.get(i, {}).get("title", i) for i in ids]
        await interaction.response.send_message(
            ("You follow: " + ", ".join(f"**{n}**" for n in names)) if names
            else "You don't follow any mod yet - use /follow.", ephemeral=True)

    @commands.Cog.listener()
    async def on_mod_release(self, project: dict, version: dict):
        if not feature_on("mod_follow"):
            return
        pid = project.get("id")
        followers = [int(uid) for uid, ids in _load()["users"].items() if pid in ids]
        if not followers:
            return
        from cogs.modrinth import _format_version_embed
        embed = _format_version_embed(project, version)
        embed.set_footer(text="You get this because you follow this mod (/follow).")
        sent = 0
        for uid in followers:
            user = self.bot.get_user(uid)
            if user is None:
                continue
            view = discord.ui.View(timeout=None)
            view.add_item(UnfollowButton(pid))
            try:
                await user.send(embed=embed, view=view)
                sent += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.5)   # gentle on Discord's DM rate limit
        log.info("Update DMs for %s %s: %d sent", project.get("title"), version.get("version_number"), sent)


async def setup(bot):
    await bot.add_cog(ModFollow(bot))
