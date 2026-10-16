"""Beta feedback: every beta tester gets a short form for each new beta version.

When a beta or alpha version comes out on Modrinth (event "mod_release"), everyone
with the Beta Tester role gets a DM with a "Give feedback" button: 1-5 stars, what
works well, what's broken. After `summary_after_days` days (or with /beta-feedback
summary) the owner gets a summary DM: average stars, number of answers and all
comments as a file.

    /beta-feedback request <version>   ask for feedback on any build (e.g. one posted
                                       in the beta-testing channel)        (administrator)
    /beta-feedback summary             the summary of the newest round now (administrator)
State: data/beta_feedback.json.
"""

import asyncio
import io
import logging
import secrets
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.common import DATA_DIR, is_admin, load_json, save_json
from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.beta_feedback")

STATE_FILE = DATA_DIR / "beta_feedback.json"
STAR = "\u2B50"
BAR = "\u2588"


def _config() -> dict:
    return SERVER_TEMPLATE.get("beta_feedback", {})


def _role_name() -> str:
    return SERVER_TEMPLATE.get("beta", {}).get("role", "Beta Tester")


def _load() -> dict:
    data = load_json(STATE_FILE)
    data.setdefault("rounds", {})   # id -> {label, guild, ts, answers: {user: {...}}, summarized}
    return data


class FeedbackModal(discord.ui.Modal):
    stars = discord.ui.TextInput(label="Rating (1-5 stars)", max_length=1, placeholder="5")
    good = discord.ui.TextInput(label="What works well?", style=discord.TextStyle.paragraph, required=False,
                                max_length=1000)
    bad = discord.ui.TextInput(label="What's broken or annoying?", style=discord.TextStyle.paragraph, required=False,
                               max_length=1000)

    def __init__(self, rid: str, label: str):
        super().__init__(title=f"Feedback: {label}"[:45])
        self.rid = rid

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.stars.value).strip()
        if value not in ("1", "2", "3", "4", "5"):
            await interaction.response.send_message("Please rate with a number from 1 to 5.", ephemeral=True)
            return
        data = _load()
        rnd = data["rounds"].get(self.rid)
        if rnd is None:
            await interaction.response.send_message("This feedback round is closed.", ephemeral=True)
            return
        rnd["answers"][str(interaction.user.id)] = {"name": str(interaction.user), "stars": int(value),
                                                    "good": str(self.good.value), "bad": str(self.bad.value),
                                                    "ts": time.time()}
        save_json(STATE_FILE, data)
        await interaction.response.send_message(f"Thanks! {'⭐' * int(value)} - your feedback was saved "
                                                "(you can send it again to change it).", ephemeral=True)


class FeedbackButton(discord.ui.DynamicItem[discord.ui.Button], template=r"betafb:(?P<rid>[0-9a-f]{8})"):
    def __init__(self, rid: str):
        self.rid = rid
        super().__init__(discord.ui.Button(label="Give feedback", emoji="\U0001F4DD", style=discord.ButtonStyle.primary,
                                           custom_id=f"betafb:{rid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        rnd = _load()["rounds"].get(self.rid)
        if rnd is None:
            await interaction.response.send_message("This feedback round is closed.", ephemeral=True)
            return
        await interaction.response.send_modal(FeedbackModal(self.rid, rnd["label"]))


def summary(rnd: dict) -> tuple[discord.Embed, discord.File | None]:
    answers = list(rnd["answers"].values())
    embed = discord.Embed(title=f"\U0001F4CA Beta feedback: {rnd['label']}", color=0x9B59B6)
    if not answers:
        embed.description = f"No answers yet ({rnd.get('asked', 0)} testers asked)."
        return embed, None
    avg = sum(a["stars"] for a in answers) / len(answers)
    embed.description = (f"**{avg:.1f}/5** {'⭐' * round(avg)} from **{len(answers)}** of "
                         f"{rnd.get('asked', '?')} testers")
    dist = {s: sum(1 for a in answers if a["stars"] == s) for s in range(5, 0, -1)}
    embed.add_field(name="Ratings", value="\n".join(f"{s}⭐ {'█' * n} {n}" for s, n in dist.items()),
                    inline=False)
    bad = [a for a in answers if a["bad"].strip()]
    if bad:
        embed.add_field(name="Problems (latest)", value="\n".join(f"- {a['bad'][:150]}" for a in bad[-5:])[:1024],
                        inline=False)
    lines = []
    for a in answers:
        lines += [f"{a['name']} - {a['stars']}/5", f"  Good: {a['good'] or '-'}", f"  Bad:  {a['bad'] or '-'}", ""]
    file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="beta-feedback.txt")
    return embed, file


class BetaFeedback(commands.Cog):
    group = app_commands.Group(name="beta-feedback", description="Feedback forms for beta versions (administrator).",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(FeedbackButton)
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def start_round(self, guild: discord.Guild, label: str, url: str | None = None) -> tuple[str, int]:
        role = discord.utils.get(guild.roles, name=_role_name())
        testers = [m for m in (role.members if role else []) if not m.bot]
        rid = secrets.token_hex(4)
        data = _load()
        data["rounds"][rid] = {"label": label, "guild": guild.id, "ts": time.time(), "answers": {},
                               "asked": len(testers), "summarized": False}
        save_json(STATE_FILE, data)
        embed = discord.Embed(title=f"\U0001F9EA New beta: {label}",
                              description="Please test it and tell us how it went - it takes a minute.",
                              color=0x9B59B6, url=url)
        sent = 0
        for m in testers:
            view = discord.ui.View(timeout=None)
            view.add_item(FeedbackButton(rid))
            try:
                await m.send(embed=embed, view=view)
                sent += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.5)
        log.info("Beta feedback round %s (%s): asked %d testers", rid, label, sent)
        return rid, sent

    async def send_summary(self, rid: str) -> bool:
        data = _load()
        rnd = data["rounds"].get(rid)
        guild = self.bot.get_guild(rnd["guild"]) if rnd else None
        if guild is None or guild.owner is None:
            return False
        embed, file = summary(rnd)
        try:
            await guild.owner.send(embed=embed, file=file) if file else await guild.owner.send(embed=embed)
        except discord.HTTPException:
            return False
        rnd["summarized"] = True
        save_json(STATE_FILE, data)
        return True

    @commands.Cog.listener()
    async def on_mod_release(self, project: dict, version: dict):
        if not _config().get("enabled", True) or version.get("version_type") not in ("beta", "alpha"):
            return
        label = f"{project.get('title')} {version.get('version_number')}"
        url = f"https://modrinth.com/mod/{project.get('slug')}/version/{version.get('id')}"
        for guild in self.bot.guilds:
            await self.start_round(guild, label, url)

    @tasks.loop(hours=1)
    async def loop(self):
        days = float(_config().get("summary_after_days", 3))
        for rid, rnd in list(_load()["rounds"].items()):
            if not rnd.get("summarized") and time.time() - rnd["ts"] > days * 86400:
                await self.send_summary(rid)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @group.command(name="request", description="Ask all beta testers for feedback on a build.")
    @app_commands.describe(version="e.g. SmiteMod 1.3.0-beta.2")
    async def request(self, interaction: discord.Interaction, version: app_commands.Range[str, 2, 80]):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        _rid, sent = await self.start_round(interaction.guild, version)
        await interaction.followup.send(f"\U0001F4E8 Asked {sent} beta tester(s) for feedback on **{version}**. "
                                        "You get a summary in a few days (or with /beta-feedback summary).",
                                        ephemeral=True)

    @group.command(name="summary", description="Send yourself the summary of the newest feedback round now.")
    async def summary_cmd(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        rounds = [(rid, r) for rid, r in _load()["rounds"].items() if r["guild"] == interaction.guild.id]
        if not rounds:
            await interaction.response.send_message("No feedback round yet.", ephemeral=True)
            return
        rid, rnd = max(rounds, key=lambda x: x[1]["ts"])
        embed, file = summary(rnd)
        if file:
            await interaction.response.send_message(embed=embed, file=file, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BetaFeedback(bot))
