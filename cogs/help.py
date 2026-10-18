"""/help: all commands by category, with a category menu and a search.

Only commands the member can actually use are shown (based on each command's
default permissions). Command names are clickable (Discord command mentions).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("setup-bot.help")

# module -> category. Anything not listed lands in "Other".
CATEGORIES: dict[str, tuple[str, str]] = {
    "Moderation": ("\U0001F6E1️", "moderation ban_tools word_filter linkfilter temp_roles antispam report"),
    "Tickets & Support": ("\U0001F3AB", "tickets support_helpers crash_analyzer bug_tracker support_voice"),
    "Mods": ("\U0001F9E9", "modrinth curseforge mod_info mod_stats mod_follow modpack_check beta beta_feedback "
                           "roadmap showcase countdown"),
    "Community": ("\U0001F4AC", "levels profile_card giveaway poll reminders afk counting tips community "
                                "community_extras temp_voice translate staff_list invites"),
    "Minecraft": ("⛏️", "mc_logs mclog_cloudflare mclog_core mc_chat_log"),
    "Server & Bot": ("⚙️", "utility settings backup bot_updates selftest panel_launcher grants maintenance "
                                    "scheduler stats weekly_report error_alerts command_stats bot_health help "
                                    "role_panel __main__ bot"),
}
OTHER = ("Other", "\U0001F4E6")
MODULE_CATEGORY = {m: name for name, (_, mods) in CATEGORIES.items() for m in mods.split()}


def category_of(command) -> str:
    module = (getattr(command, "module", None) or "").split(".")[-1]
    return MODULE_CATEGORY.get(module, OTHER[0])


def emoji_of(category: str) -> str:
    return CATEGORIES.get(category, (OTHER[1], ""))[0]


def can_use(member, command, parent=None) -> bool:
    """Members see a command if they have its default permissions (or are administrators)."""
    needed = getattr(command, "default_permissions", None) or getattr(parent, "default_permissions", None)
    if needed is None or needed.value == 0:
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    if perms.administrator:
        return True
    from cogs.common import is_granted
    if is_granted(member):
        return True
    return needed.is_subset(perms)


def collect(tree: app_commands.CommandTree, member) -> list[dict]:
    """[{name, description, category}] - every slash (sub)command the member may use, plus context menus."""
    out = []

    def walk(cmd, parent=None, prefix=""):
        if isinstance(cmd, app_commands.Group):
            if not can_use(member, cmd, parent):
                return
            for sub in cmd.commands:
                walk(sub, cmd if parent is None else parent, f"{prefix}{cmd.name} ")
            return
        if not can_use(member, cmd, parent):
            return
        root = parent or cmd
        out.append({"name": f"{prefix}{cmd.name}", "root": root.name, "description": cmd.description or "",
                    "category": category_of(root if parent else cmd)})

    for cmd in tree.get_commands(type=discord.AppCommandType.chat_input):
        walk(cmd)
    menus = tree.get_commands(type=discord.AppCommandType.message) + tree.get_commands(type=discord.AppCommandType.user)
    for menu in menus:
        if can_use(member, menu):
            out.append({"name": menu.name, "root": None, "description": "Right-click a message/member → Apps",
                        "category": "Right-click menu"})
    out.sort(key=lambda c: c["name"])
    return out


def search_commands(commands_: list[dict], query: str) -> list[dict]:
    words = query.lower().split()
    scored = []
    for c in commands_:
        hay_name, hay_desc = c["name"].lower(), c["description"].lower()
        if all(w in hay_name or w in hay_desc for w in words):
            scored.append((0 if all(w in hay_name for w in words) else 1, c))
    return [c for _, c in sorted(scored, key=lambda x: (x[0], x[1]["name"]))]


def _line(c: dict, ids: dict[str, int]) -> str:
    cid = ids.get(c["root"]) if c["root"] else None
    name = f"</{c['name']}:{cid}>" if cid else f"`{'/' if c['root'] else ''}{c['name']}`"
    return f"{name} - {c['description'][:90]}"


def page_embed(title: str, items: list[dict], ids: dict[str, int], footer: str = "") -> discord.Embed:
    lines = [_line(c, ids) for c in items]
    text, shown = "", 0
    for line in lines:
        if len(text) + len(line) + 1 > 3900:
            break
        text += line + "\n"
        shown += 1
    if shown < len(lines):
        text += f"... and {len(lines) - shown} more - use the search."
    embed = discord.Embed(title=title, description=text or "Nothing found.", color=0x5865F2)
    if footer:
        embed.set_footer(text=footer)
    return embed


def overview_embed(commands_: list[dict]) -> discord.Embed:
    counts: dict[str, int] = {}
    for c in commands_:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    embed = discord.Embed(title="❓ Help", color=0x5865F2,
                          description="Pick a category below or press **Search**.\n"
                                      "You only see the commands you're allowed to use.")
    for cat, n in sorted(counts.items(), key=lambda kv: list(CATEGORIES).index(kv[0]) if kv[0] in CATEGORIES else 99):
        embed.add_field(name=f"{emoji_of(cat) if cat != 'Right-click menu' else chr(0x1F5B1)} {cat}",
                        value=f"{n} command{'s' if n != 1 else ''}", inline=True)
    return embed


class SearchModal(discord.ui.Modal, title="Search commands"):
    query = discord.ui.TextInput(label="What are you looking for?", placeholder="e.g. ticket, ban, download",
                                 max_length=50)

    def __init__(self, view: "HelpView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        found = search_commands(self.view.commands, self.query.value)
        await interaction.response.edit_message(
            embed=page_embed(f"\U0001F50E \"{self.query.value}\" - {len(found)} found", found, self.view.ids),
            view=self.view)


class CategorySelect(discord.ui.Select):
    def __init__(self, categories: list[str]):
        options = [discord.SelectOption(label="Overview", value="__overview", emoji="❓")]
        for cat in categories:
            emoji = emoji_of(cat) if cat != "Right-click menu" else "\U0001F5B1️"
            options.append(discord.SelectOption(label=cat, value=cat, emoji=emoji))
        super().__init__(placeholder="Pick a category", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        view: HelpView = self.view
        value = self.values[0]
        if value == "__overview":
            embed = overview_embed(view.commands)
        else:
            items = [c for c in view.commands if c["category"] == value]
            embed = page_embed(f"{emoji_of(value) if value != 'Right-click menu' else ''} {value}".strip(), items,
                               view.ids, footer=f"{len(items)} commands")
        await interaction.response.edit_message(embed=embed, view=view)


class HelpView(discord.ui.View):
    def __init__(self, owner_id: int, commands_: list[dict], ids: dict[str, int]):
        super().__init__(timeout=600)
        self.owner_id, self.commands, self.ids = owner_id, commands_, ids
        cats = []
        for c in commands_:
            if c["category"] not in cats:
                cats.append(c["category"])
        order = list(CATEGORIES) + [OTHER[0], "Right-click menu"]
        cats.sort(key=lambda c: order.index(c) if c in order else 99)
        self.add_item(CategorySelect(cats))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Use /help yourself to get your own menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Search", emoji="\U0001F50E", style=discord.ButtonStyle.primary, row=1)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SearchModal(self))


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ids: dict[int, dict[str, int]] = {}   # guild -> command name -> id (for clickable mentions)

    async def _ids(self, guild: discord.Guild | None) -> dict[str, int]:
        key = guild.id if guild else 0
        if key not in self.ids:
            try:
                fetched = await self.bot.tree.fetch_commands(guild=guild)
                self.ids[key] = {c.name: c.id for c in fetched}
            except discord.HTTPException:
                return {}
        return self.ids[key]

    @app_commands.command(name="help", description="All commands you can use, by category - with search.")
    @app_commands.describe(search="Search right away, e.g. 'ticket'")
    async def help(self, interaction: discord.Interaction, search: app_commands.Range[str, 1, 50] | None = None):
        member = interaction.user
        if interaction.guild is not None and not isinstance(member, discord.Member):
            member = interaction.guild.get_member(member.id) or member
        cmds = collect(self.bot.tree, member)
        ids = await self._ids(interaction.guild)
        view = HelpView(interaction.user.id, cmds, ids)
        if search:
            found = search_commands(cmds, search)
            embed = page_embed(f"\U0001F50E \"{search}\" - {len(found)} found", found, ids)
        else:
            embed = overview_embed(cmds)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
