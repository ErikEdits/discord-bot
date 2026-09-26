"""Self-update commands, daily check and admin DMs (see updater.py for how updates work).

    /bot-update-status   version, update step, what changes (administrator)
    /bot-update-check    check GitHub now and download a found update (administrator)
    /bot-restart         restart the bot (administrator, with confirmation)

Once a day the bot checks GitHub while it runs and downloads a found update in
the background ("prepared"); it's installed on the next start. Every update
event (found, downloaded, installed, healthy, rolled back) is sent as a DM to
all members with the Administrator permission.
"""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import updater
from cogs.common import is_admin

log = logging.getLogger("setup-bot.bot_updates")

PHASE_TEXT = {
    "idle": "Up to date",
    "detected": "Update found - it will be downloaded on the next start (step 1 of 3 done)",
    "staged": "Update downloaded - it will be installed on the next start (step 2 of 3 done)",
    "applying": "Installing...",
    "applied": "Update installed - checking that it runs fine",
}


def _admins(bot) -> list[discord.Member]:
    seen, out = set(), []
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot and member.id not in seen and member.guild_permissions.administrator:
                seen.add(member.id)
                out.append(member)
    return out


def _changes_text(changes: dict | None, limit: int = 12) -> str:
    if not changes:
        return "-"
    lines = []
    for key, sign in (("changed", "~"), ("added", "+"), ("deleted", "-")):
        lines += [f"{sign} {p}" for p in changes.get(key, [])]
    text = "\n".join(lines[:limit])
    if len(lines) > limit:
        text += f"\n... and {len(lines) - limit} more"
    return f"```diff\n{text}\n```" if text else "-"


class RestartConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Restart now", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="\U0001F504 Restarting... back in a few seconds.", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Restart cancelled.", view=None)
        self.stop()


class BotUpdates(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.checking = False
        self.notice_loop.start()
        self.daily_loop.start()

    def cog_unload(self):
        self.notice_loop.cancel()
        self.daily_loop.cancel()

    # -------- Background ----------------------------------------------------

    async def send_notices(self) -> None:
        notices = updater.pop_notices()
        if not notices:
            return
        embed = discord.Embed(
            title="\U0001F504 Bot update",
            description="\n\n".join(("❌ " if n["level"] == "error" else "ℹ️ ") + n["text"]
                                    for n in notices[-10:])[:4000],
            color=0xE74C3C if any(n["level"] == "error" for n in notices) else 0x3498DB,
        )
        embed.set_footer(text=f"{updater.repo()} · {updater.branch()} · /bot-update-status")
        for member in _admins(self.bot):
            try:
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @tasks.loop(minutes=1)
    async def notice_loop(self):
        await self.send_notices()

    @notice_loop.before_loop
    async def _before_notices(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def daily_loop(self):
        if not updater.enabled() or self.checking:
            return
        if time.time() - (updater.load_state().get("last_check") or 0) < updater.DAILY_CHECK_SECONDS:
            return
        await self._check()

    @daily_loop.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()

    async def _check(self) -> str:
        self.checking = True
        try:
            result = await asyncio.to_thread(updater.background_check)
        finally:
            self.checking = False
        log.info("Update check: %s", result)
        await self.send_notices()
        return result

    # -------- Commands ------------------------------------------------------

    @app_commands.command(name="bot-update-status", description="Show the bot version and the auto-update status.")
    @app_commands.default_permissions(administrator=True)
    async def update_status(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        st = updater.status()
        embed = discord.Embed(title="Bot update status", color=0x3498DB)
        embed.add_field(name="Auto-update", value="on" if st["enabled"] else "off", inline=True)
        embed.add_field(name="Source", value=f"`{st['repo']}`\nbranch `{st['branch']}`", inline=True)
        embed.add_field(name="Installed version", value=f"`{updater.short(st['installed_commit'])}`", inline=True)
        embed.add_field(name="Status", value=PHASE_TEXT.get(st["phase"], st["phase"]), inline=False)
        pending = st.get("staged") or st.get("target") or st.get("applied")
        if pending:
            embed.add_field(
                name=f"Update {updater.short(pending.get('commit'))}",
                value=f"\"{pending.get('message', '')}\"\n{_changes_text(pending.get('changes'))}"[:1024],
                inline=False,
            )
        if st.get("last_check"):
            embed.add_field(name="Last check", value=f"<t:{int(st['last_check'])}:R>", inline=True)
        if st.get("last_error"):
            embed.add_field(name="Last problem", value=st["last_error"][:1024], inline=False)
        if st.get("bad_commits"):
            embed.add_field(name="Skipped (rolled back) versions",
                            value=", ".join(f"`{updater.short(c)}`" for c in st["bad_commits"][-5:]), inline=False)
        embed.set_footer(text="Updates advance one step per start: check -> download -> install")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bot-update-check", description="Check GitHub for an update now (it's installed on the next start).")
    @app_commands.default_permissions(administrator=True)
    async def update_check(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if self.checking:
            await interaction.response.send_message("A check is already running.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._check()
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(name="bot-restart", description="Restart the bot (counts as one start for updates).")
    @app_commands.default_permissions(administrator=True)
    async def restart_command(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        phase = updater.load_state()["phase"]
        note = {
            "detected": "\nOn this start the found update will be **downloaded**.",
            "staged": "\nOn this start the downloaded update will be **installed**.",
            "applied": "\nThe new update is still being checked - restarting now counts as a start attempt.",
        }.get(phase, "")
        view = RestartConfirmView(interaction.user.id)
        await interaction.response.send_message(f"Restart the bot now?{note}", view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            return
        log.info("Restart requested by %s", interaction.user)
        await asyncio.sleep(1)
        updater.request_restart()
        await self.bot.close()


async def setup(bot):
    await bot.add_cog(BotUpdates(bot))
