"""Forwarded bugs get a number, and the reporter is told when the bug is fixed.

"Forward bug" in a bug ticket (cogs/tickets.py) stores the bug here with the
person who reported it. When it's fixed:

    /bug fixed bug:<number> version:<e.g. 1.4.2> [note]   DMs the reporter "Fixed in version X"
    /bug list                                             open (and recently fixed) bugs

State: data/bugs.json.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import DATA_DIR, is_staff, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.bug_tracker")

STATE_FILE = DATA_DIR / "bugs.json"


def _support_roles() -> list:
    return SERVER_TEMPLATE.get("tickets", {}).get("support_role_names", ["Moderator", "Admin", "Owner"])


def _load() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("next", 1)
    data.setdefault("bugs", {})
    return data


def record_bug(guild_id: int, reporter_id: int | None, title: str, ticket: str, forwarded_by: str) -> int:
    """Remember a forwarded bug. Returns its number."""
    data = _load()
    number = int(data["next"])
    data["next"] = number + 1
    data["bugs"][str(number)] = {
        "guild": guild_id, "reporter": reporter_id, "title": title[:200], "ticket": ticket,
        "by": forwarded_by, "at": time.time(), "status": "open",
    }
    save_json(STATE_FILE, data)
    return number


def forget_bug(number: int) -> None:
    data = _load()
    if data["bugs"].pop(str(number), None) is not None:
        save_json(STATE_FILE, data)


def fixed_dm(bug: dict, number: int, version: str, note: str | None, guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Your bug report was fixed!",
        description=f"The bug you reported on **{guild_name}** is fixed in version **{version}**.\n\n"
                    f"**Bug #{number}:** {bug.get('title', '')}",
        color=0x2ECC71)
    if note:
        embed.add_field(name="Note from the team", value=note[:1000], inline=False)
    embed.set_footer(text="Thanks for reporting it - that really helps!")
    return embed


class BugTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="bug", description="Forwarded bug reports (team).", guild_only=True)

    async def _open_autocomplete(self, interaction: discord.Interaction, current: str):
        bugs = _load()["bugs"]
        out = []
        for num, bug in sorted(bugs.items(), key=lambda kv: -int(kv[0])):
            if bug.get("status") != "open" or bug.get("guild") != interaction.guild_id:
                continue
            label = f"#{num} {bug.get('title', '')}"[:100]
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label, value=int(num)))
        return out[:25]

    @group.command(name="fixed", description="Mark a forwarded bug as fixed - the reporter gets a DM.")
    @app_commands.describe(bug="Bug number (from the forward message)", version="Version with the fix, e.g. 1.4.2",
                           note="Optional note for the reporter")
    @app_commands.autocomplete(bug=_open_autocomplete)
    async def fixed(self, interaction: discord.Interaction, bug: int, version: app_commands.Range[str, 1, 40],
                    note: app_commands.Range[str, 1, 500] | None = None):
        if not is_staff(interaction.user, _support_roles()):
            await interaction.response.send_message("Only the team can do this.", ephemeral=True)
            return
        data = _load()
        entry = data["bugs"].get(str(bug))
        if entry is None or entry.get("guild") != interaction.guild_id:
            await interaction.response.send_message(f"There's no bug #{bug}.", ephemeral=True)
            return
        if entry.get("status") == "fixed":
            await interaction.response.send_message(f"Bug #{bug} is already marked as fixed "
                                                    f"(version {entry.get('version')}).", ephemeral=True)
            return
        entry.update(status="fixed", version=version, fixed_at=time.time(), fixed_by=str(interaction.user))
        save_json(STATE_FILE, data)
        told = "nobody to tell (reporter unknown)"
        if entry.get("reporter"):
            try:
                user = self.bot.get_user(entry["reporter"]) or await self.bot.fetch_user(entry["reporter"])
                await user.send(embed=fixed_dm(entry, bug, version, note, interaction.guild.name))
                told = f"{user.mention} got a DM"
            except discord.HTTPException:
                told = "the reporter couldn't be DM'd (DMs closed or left Discord)"
        await interaction.response.send_message(f"✅ Bug #{bug} marked as fixed in **{version}** - {told}.",
                                                ephemeral=True)
        log.info("Bug #%s fixed in %s by %s", bug, version, interaction.user)

    @group.command(name="list", description="Show open bugs (and the last fixed ones).")
    async def list_bugs(self, interaction: discord.Interaction):
        if not is_staff(interaction.user, _support_roles()):
            await interaction.response.send_message("Only the team can do this.", ephemeral=True)
            return
        bugs = [(int(n), b) for n, b in _load()["bugs"].items() if b.get("guild") == interaction.guild_id]
        open_bugs = sorted((x for x in bugs if x[1].get("status") == "open"), key=lambda x: -x[0])
        fixed = sorted((x for x in bugs if x[1].get("status") == "fixed"), key=lambda x: -x[1].get("fixed_at", 0))
        lines = [f"`#{n}` {b.get('title', '')[:70]} - <t:{int(b.get('at', 0))}:R>" for n, b in open_bugs[:20]]
        embed = discord.Embed(title=f"\U0001F41B Open bugs ({len(open_bugs)})",
                              description="\n".join(lines) or "No open bugs \U0001F389", color=0xE74C3C)
        if fixed:
            embed.add_field(name="Recently fixed", inline=False, value="\n".join(
                f"`#{n}` {b.get('title', '')[:60]} - {b.get('version')}" for n, b in fixed[:5]))
        embed.set_footer(text="Mark one as fixed with /bug fixed")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BugTracker(bot))
