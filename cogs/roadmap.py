"""Roadmap per mod in CHANNELS["roadmap"].

Each mod gets one embed (Planned / In progress / Done) that the bot keeps up
to date. Items are numbered so they're easy to reference.

Admin commands:
    /roadmap add mod column item
    /roadmap move mod item column
    /roadmap remove mod item
    /roadmap delete-mod mod
    /roadmap refresh
    /roadmap webhook       -> creates (or shows) the channel webhook URL

The webhook lets you - or a script / Claude Code - post into the roadmap
channel from anywhere with a simple HTTP POST. Normal webhook messages just
appear in the channel. Lines starting with "roadmap " are commands; the bot
applies them, deletes the command message and updates the embeds:

    roadmap add SmiteMod | planned | Config screen
    roadmap move SmiteMod | #2 | done          (item number or its text)
    roadmap remove SmiteMod | Config screen
    roadmap delete SmiteMod

curl example:
    curl -H "Content-Type: application/json" \\
         -d '{"content": "roadmap add SmiteMod | progress | 1.21.2 port"}' <WEBHOOK_URL>

State: data/roadmap.json. The webhook ID is stored in data/settings.json.
"""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, get_setting, is_admin, load_json, save_json, set_setting
from server_template import CHANNELS, SERVER_TEMPLATE

log = logging.getLogger("setup-bot.roadmap")

ROADMAP_FILE = DATA_DIR / "roadmap.json"
WEBHOOK_SETTING = "roadmap_webhooks"
WEBHOOK_NAME = "Roadmap"

COLUMNS = {
    "planned":  "\U0001F4DD Planned",
    "progress": "\U0001F6E0️ In progress",
    "done":     "✅ Done",
}
COLUMN_ALIASES = {
    "planned": "planned", "plan": "planned", "todo": "planned", "geplant": "planned", "idea": "planned",
    "progress": "progress", "in progress": "progress", "inprogress": "progress", "wip": "progress",
    "in arbeit": "progress", "doing": "progress",
    "done": "done", "fertig": "done", "finished": "done", "released": "done",
}
COLUMN_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in COLUMNS.items()]
MAX_ITEM_LENGTH = 200


def _config() -> dict:
    return SERVER_TEMPLATE.get("roadmap", {})


def load_roadmap() -> dict:
    data = load_json(ROADMAP_FILE)
    data.setdefault("guilds", {})
    return data


def _guild(data: dict, guild_id: int) -> dict:
    return data["guilds"].setdefault(str(guild_id), {"mods": {}, "order": []})


def find_mod(g: dict, name: str) -> str | None:
    wanted = name.strip().lower()
    return next((m for m in g["mods"] if m.lower() == wanted), None)


def numbered_items(mod: dict) -> list[tuple[int, str, str]]:
    """[(number, column, text)] in display order."""
    out, n = [], 1
    for col in COLUMNS:
        for text in mod.get(col, []):
            out.append((n, col, text))
            n += 1
    return out


def find_item(mod: dict, ref: str) -> tuple[str, int] | None:
    """Find an item by '#3' / '3' or by (unique, case-insensitive) text. Returns (column, index)."""
    ref = ref.strip()
    items = numbered_items(mod)
    number = ref.lstrip("#")
    if number.isdigit():
        for n, col, text in items:
            if n == int(number):
                return col, mod[col].index(text)
        return None
    exact = [(col, text) for _, col, text in items if text.lower() == ref.lower()]
    partial = [(col, text) for _, col, text in items if ref.lower() in text.lower()]
    hits = exact or partial
    if len(hits) == 1:
        col, text = hits[0]
        return col, mod[col].index(text)
    return None


def parse_column(value: str) -> str | None:
    return COLUMN_ALIASES.get(value.strip().lower())


def apply_command(data: dict, guild_id: int, line: str) -> tuple[bool, str, str | None]:
    """Apply one text command. Returns (ok, message, affected mod name or None)."""
    text = line.strip()
    if not text.lower().startswith("roadmap "):
        return False, "not a roadmap command", None
    body = text[len("roadmap "):].strip()
    action, _, rest = body.partition(" ")
    action = action.lower()
    parts = [p.strip() for p in rest.split("|")]
    g = _guild(data, guild_id)

    if action == "add":
        if len(parts) != 3 or not all(parts):
            return False, "use: roadmap add <mod> | <planned|progress|done> | <text>", None
        mod_name, col_raw, item = parts
        col = parse_column(col_raw)
        if col is None:
            return False, f"unknown column '{col_raw}' (planned, progress, done)", None
        existing = find_mod(g, mod_name)
        if existing is None:
            existing = mod_name[:80]
            g["mods"][existing] = {"planned": [], "progress": [], "done": [], "message_id": None}
            g["order"].append(existing)
        g["mods"][existing][col].append(item[:MAX_ITEM_LENGTH])
        return True, f"added to {existing} / {col}", existing

    if action == "move":
        if len(parts) != 3 or not all(parts):
            return False, "use: roadmap move <mod> | <#number or text> | <column>", None
        mod_name, ref, col_raw = parts
        existing = find_mod(g, mod_name)
        if existing is None:
            return False, f"no roadmap for '{mod_name}'", None
        col = parse_column(col_raw)
        if col is None:
            return False, f"unknown column '{col_raw}'", None
        found = find_item(g["mods"][existing], ref)
        if found is None:
            return False, f"item '{ref}' not found (or not unique) in {existing}", None
        old_col, idx = found
        item = g["mods"][existing][old_col].pop(idx)
        g["mods"][existing][col].append(item)
        return True, f"moved '{item}' to {col}", existing

    if action == "remove":
        if len(parts) != 2 or not all(parts):
            return False, "use: roadmap remove <mod> | <#number or text>", None
        mod_name, ref = parts
        existing = find_mod(g, mod_name)
        if existing is None:
            return False, f"no roadmap for '{mod_name}'", None
        found = find_item(g["mods"][existing], ref)
        if found is None:
            return False, f"item '{ref}' not found (or not unique) in {existing}", None
        col, idx = found
        item = g["mods"][existing][col].pop(idx)
        return True, f"removed '{item}'", existing

    if action == "delete":
        mod_name = rest.strip()
        existing = find_mod(g, mod_name) if mod_name else None
        if existing is None:
            return False, f"no roadmap for '{mod_name}'", None
        g["mods"][existing]["deleted"] = True
        return True, f"deleted roadmap of {existing}", existing

    return False, f"unknown action '{action}' (add, move, remove, delete)", None


def build_embed(name: str, mod: dict) -> discord.Embed:
    embed = discord.Embed(title=f"\U0001F5FA️ {name}", color=0x3498DB, timestamp=datetime.now(timezone.utc))
    items = numbered_items(mod)
    max_done = int(_config().get("max_done_shown", 10))
    for col, label in COLUMNS.items():
        rows = [(n, text) for n, c, text in items if c == col]
        hidden = 0
        if col == "done" and len(rows) > max_done:
            hidden = len(rows) - max_done
            rows = rows[-max_done:]
        lines = [f"`{n}` {text}" for n, text in rows]
        if hidden:
            lines.insert(0, f"*... {hidden} older*")
        value = "\n".join(lines) or "*nothing yet*"
        if len(value) > 1024:
            value = value[:1000].rsplit("\n", 1)[0] + "\n*...*"
        embed.add_field(name=label, value=value, inline=False)
    embed.set_footer(text="Last updated")
    return embed


class Roadmap(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(
        name="roadmap",
        description="Manage the mod roadmaps (administrator).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    def _channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        return discord.utils.get(guild.text_channels, name=CHANNELS.get("roadmap", ""))

    async def render(self, guild: discord.Guild, data: dict, mod_name: str) -> None:
        """Create, update or delete the embed of one mod. Saves data."""
        g = _guild(data, guild.id)
        mod = g["mods"].get(mod_name)
        channel = self._channel(guild)
        if mod is None:
            return
        message = None
        if channel and mod.get("message_id"):
            try:
                message = await channel.fetch_message(mod["message_id"])
            except (discord.NotFound, discord.Forbidden):
                message = None
        if mod.get("deleted"):
            if message:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            g["mods"].pop(mod_name, None)
            g["order"] = [m for m in g["order"] if m != mod_name]
            save_json(ROADMAP_FILE, data)
            return
        if channel is None:
            save_json(ROADMAP_FILE, data)
            return
        embed = build_embed(mod_name, mod)
        try:
            if message:
                await message.edit(embed=embed)
            else:
                message = await channel.send(embed=embed)
                mod["message_id"] = message.id
        except discord.HTTPException:
            log.exception("Failed to render roadmap for %s", mod_name)
        save_json(ROADMAP_FILE, data)

    # -------- Webhook input -----------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.webhook_id is None:
            return
        hooks = get_setting(WEBHOOK_SETTING) or {}
        if hooks.get(str(message.guild.id)) != message.webhook_id:
            return
        lines = [l for l in (message.content or "").splitlines() if l.strip()]
        commands_ = [l for l in lines if l.strip().lower().startswith("roadmap ")]
        if not commands_:
            return  # a normal post through the webhook - leave it
        data = load_roadmap()
        affected, errors = [], []
        for line in commands_:
            ok, text, mod = apply_command(data, message.guild.id, line)
            if ok and mod not in affected:
                affected.append(mod)
            if not ok:
                errors.append(f"`{line.strip()[:80]}` - {text}")
            log.info("Roadmap webhook: %s -> %s", line.strip()[:80], text)
        save_json(ROADMAP_FILE, data)
        for mod in affected:
            await self.render(message.guild, data, mod)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if errors:
            try:
                await message.channel.send("⚠️ Roadmap command failed:\n" + "\n".join(errors[:5]), delete_after=30)
            except discord.HTTPException:
                pass

    # -------- Commands ------------------------------------------------------

    async def _mod_autocomplete(self, interaction: discord.Interaction, current: str):
        names = list(_guild(load_roadmap(), interaction.guild_id)["mods"].keys())
        modrinth = self.bot.get_cog("Modrinth")
        if modrinth is not None:
            try:
                names += [p.get("title", "") for p in await modrinth._cached_projects()]
            except Exception:
                pass
        seen, out = set(), []
        for n in names:
            if n and n.lower() not in seen and current.lower() in n.lower():
                seen.add(n.lower())
                out.append(app_commands.Choice(name=n[:100], value=n[:100]))
        return out[:25]

    async def _item_autocomplete(self, interaction: discord.Interaction, current: str):
        mod_name = getattr(interaction.namespace, "mod", "") or ""
        g = _guild(load_roadmap(), interaction.guild_id)
        existing = find_mod(g, mod_name)
        if existing is None:
            return []
        return [
            app_commands.Choice(name=f"#{n} [{col}] {text}"[:100], value=f"#{n}")
            for n, col, text in numbered_items(g["mods"][existing])
            if current.lower() in text.lower() or current.strip("#") == str(n)
        ][:25]

    async def _run(self, interaction: discord.Interaction, line: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if self._channel(interaction.guild) is None:
            await interaction.response.send_message(
                f"The #{CHANNELS.get('roadmap')} channel is missing - run `/update` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = load_roadmap()
        ok, text, mod = apply_command(data, interaction.guild_id, line)
        if ok:
            await self.render(interaction.guild, data, mod)
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)

    @group.command(name="add", description="Add an item to a mod's roadmap.")
    @app_commands.describe(mod="Mod name (new mods get their own embed)", column="Where to put it", item="What it is")
    @app_commands.choices(column=COLUMN_CHOICES)
    @app_commands.autocomplete(mod=_mod_autocomplete)
    async def add(self, interaction: discord.Interaction, mod: str, column: app_commands.Choice[str],
                  item: app_commands.Range[str, 1, MAX_ITEM_LENGTH]):
        await self._run(interaction, f"roadmap add {mod.replace('|', '/')} | {column.value} | {item.replace('|', '/')}")

    @group.command(name="move", description="Move an item to another column.")
    @app_commands.describe(mod="Mod name", item="Item number or text", column="New column")
    @app_commands.choices(column=COLUMN_CHOICES)
    @app_commands.autocomplete(mod=_mod_autocomplete, item=_item_autocomplete)
    async def move(self, interaction: discord.Interaction, mod: str, item: str, column: app_commands.Choice[str]):
        await self._run(interaction, f"roadmap move {mod} | {item} | {column.value}")

    @group.command(name="remove", description="Remove an item from a roadmap.")
    @app_commands.describe(mod="Mod name", item="Item number or text")
    @app_commands.autocomplete(mod=_mod_autocomplete, item=_item_autocomplete)
    async def remove(self, interaction: discord.Interaction, mod: str, item: str):
        await self._run(interaction, f"roadmap remove {mod} | {item}")

    @group.command(name="delete-mod", description="Delete a mod's whole roadmap embed.")
    @app_commands.autocomplete(mod=_mod_autocomplete)
    async def delete_mod(self, interaction: discord.Interaction, mod: str):
        await self._run(interaction, f"roadmap delete {mod}")

    @group.command(name="refresh", description="Re-draw all roadmap embeds (e.g. after deleting messages).")
    async def refresh(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = load_roadmap()
        for mod in list(_guild(data, interaction.guild_id)["order"]):
            await self.render(interaction.guild, data, mod)
        await interaction.followup.send("Roadmap refreshed.", ephemeral=True)

    @group.command(name="webhook", description="Create or show the roadmap webhook URL (keep it secret!).")
    @app_commands.describe(regenerate="Delete the old webhook and create a new URL (if the old one leaked)")
    async def webhook(self, interaction: discord.Interaction, regenerate: bool = False):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        channel = self._channel(interaction.guild)
        if channel is None:
            await interaction.response.send_message(
                f"The #{CHANNELS.get('roadmap')} channel is missing - run `/update` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        hooks = get_setting(WEBHOOK_SETTING) or {}
        hook = None
        try:
            for h in await channel.webhooks():
                if h.id == hooks.get(str(interaction.guild_id)):
                    hook = h
            if hook and regenerate:
                await hook.delete(reason=f"Roadmap webhook regenerated by {interaction.user}")
                hook = None
            if hook is None:
                hook = await channel.create_webhook(name=WEBHOOK_NAME, reason=f"Roadmap webhook by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("I need the **Manage Webhooks** permission in that channel.", ephemeral=True)
            return
        hooks[str(interaction.guild_id)] = hook.id
        set_setting(WEBHOOK_SETTING, hooks)
        example = "roadmap add SmiteMod | planned | Config screen"
        await interaction.followup.send(
            f"**Roadmap webhook for {channel.mention}** - keep this URL secret, anyone with it can post there:\n"
            f"||{hook.url}||\n\n"
            "Normal messages sent to it appear in the channel. Lines starting with `roadmap` are commands:\n"
            "```\nroadmap add <mod> | <planned|progress|done> | <text>\n"
            "roadmap move <mod> | <#number or text> | <column>\n"
            "roadmap remove <mod> | <#number or text>\n"
            "roadmap delete <mod>\n```"
            f"Example with curl:\n```\ncurl -H \"Content-Type: application/json\" "
            f"-d '{{\"content\": \"{example}\"}}' <URL>\n```",
            ephemeral=True,
        )
        log.info("Roadmap webhook %s by %s", "regenerated" if regenerate else "shown", interaction.user)


async def setup(bot):
    await bot.add_cog(Roadmap(bot))
