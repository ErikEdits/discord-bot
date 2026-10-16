"""Modpack check: do the server owner's mods fit into someone's modpack?

    /modpack-check file:<latest.log / crash report / mod list> [minecraft] [loader]

The bot reads which mods are installed (latest.log, crash report, or simply a list of
jar file names / "modid version" lines) and checks every one of the owner's Modrinth
mods in it:
  - made for this Minecraft version and loader?
  - newest version installed?
  - required libraries installed? (from the mod's Modrinth dependencies)
  - mods installed that it's marked incompatible with?
Only the person who ran it sees the answer.
"""

import json
import logging
import re
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from crashlog_analyzer import extract_info, extract_mods

log = logging.getLogger("setup-bot.modpack_check")

API = "https://api.modrinth.com/v2"
UA = {"User-Agent": "ErikEdits-Discord-Bot modpack check"}
_JAR = re.compile(r"([A-Za-z][\w\-+.]*?)(?:[-_+](?:mc|fabric|forge|neoforge|quilt)?v?\d[\w.+\-]*)?\.jar")
_LINE = re.compile(r"^\s*([a-z0-9_\-]{2,64})[\s:=]+v?(\d[\w.+\-]*)\s*$", re.MULTILINE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def read_mods(text: str) -> dict[str, str]:
    """modid/name -> version ('?' when unknown) from a log, crash report or a plain list."""
    mods = extract_mods(text)
    if mods:
        return mods
    for mod_id, ver in _LINE.findall(text):
        mods.setdefault(mod_id, ver)
    for m in _JAR.finditer(text):
        name = m.group(1).lower().rstrip("-_.")
        mods.setdefault(name, re.search(r"\d[\w.+\-]*(?=\.jar)", m.group(0)).group(0)
                        if re.search(r"\d[\w.+\-]*(?=\.jar)", m.group(0)) else "?")
    return mods


async def _get(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json() if r.status == 200 else None
    except Exception:
        return None


async def check_modpack(projects: list[dict], installed: dict[str, str], mc: str | None, loader: str | None,
                        session: aiohttp.ClientSession) -> list[tuple[str, list[str]]]:
    """[(mod title, [lines])] for every owner mod found in the pack."""
    by_norm = {_norm(k): (k, v) for k, v in installed.items()}
    installed_norm = set(by_norm)
    results = []
    for p in projects:
        hit = next((by_norm[n] for n in {_norm(p.get("slug", "")), _norm(p.get("title", ""))} if n and n in by_norm), None)
        if hit is None:
            continue
        title, lines = p.get("title") or hit[0], []
        ver = hit[1]
        if mc and p.get("game_versions") and mc not in p["game_versions"]:
            lines.append(f"❌ not made for Minecraft {mc}")
        if loader and p.get("loaders") and loader not in p["loaders"]:
            lines.append(f"❌ no version for {loader.title()}")
        if lines or not (mc and loader):
            if not (mc and loader):
                lines.append("ℹ Minecraft version / loader unknown - pass them as options for a full check")
            results.append((title, lines))
            continue
        versions = await _get(session, f"{API}/project/{p['id']}/version?loaders={quote(json.dumps([loader]))}"
                                       f"&game_versions={quote(json.dumps([mc]))}") or []
        versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        latest = versions[0] if versions else None
        if latest is None:
            lines.append(f"❌ no version for Minecraft {mc} + {loader.title()}")
            results.append((title, lines))
            continue
        newest = latest.get("version_number", "")
        if ver != "?" and newest and newest.lower() not in ver.lower() and ver.lower() not in newest.lower():
            lines.append(f"⬆ installed `{ver}`, newest is `{newest}`")
        deps = latest.get("dependencies") or []
        ids = [d["project_id"] for d in deps if d.get("project_id") and d.get("dependency_type") in ("required", "incompatible")]
        names = {}
        if ids:
            for proj in await _get(session, f"{API}/projects?ids={quote(json.dumps(ids))}") or []:
                names[proj["id"]] = proj
        for d in deps:
            proj = names.get(d.get("project_id"))
            if proj is None:
                continue
            present = bool({_norm(proj.get("slug", "")), _norm(proj.get("title", ""))} & installed_norm)
            if d.get("dependency_type") == "required" and not present:
                lines.append(f"❌ needs **{proj.get('title')}** - missing "
                             f"([download](https://modrinth.com/mod/{proj.get('slug')}))")
            elif d.get("dependency_type") == "incompatible" and present:
                lines.append(f"⚠ doesn't work together with **{proj.get('title')}** (installed)")
        if not lines:
            lines.append(f"✅ fits - `{ver}` is fine for {mc} {loader.title()}")
        results.append((title, lines))
    return results


class ModpackCheck(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="modpack-check", description="Check if our mods fit into your modpack.")
    @app_commands.describe(file="latest.log, a crash report or a list of your mod files",
                           minecraft="e.g. 1.20.1 (if the file doesn't say it)", loader="if the file doesn't say it")
    @app_commands.choices(loader=[app_commands.Choice(name=x, value=x.lower()) for x in ("Fabric", "NeoForge", "Forge", "Quilt")])
    async def modpack_check(self, interaction: discord.Interaction, file: discord.Attachment,
                            minecraft: str | None = None, loader: str | None = None):
        if file.size > 8 * 1024 * 1024:
            await interaction.response.send_message("That file is too big (max. 8 MB).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from cogs.crash_analyzer import _decode
        text = _decode(await file.read(), file.filename)
        if not text:
            await interaction.followup.send("I couldn't read that file as text.", ephemeral=True)
            return
        info = extract_info(text)
        mc = minecraft or info.get("minecraft")
        ldr = loader or (info.get("loader") or "").lower() or None
        installed = read_mods(text)
        if not installed:
            await interaction.followup.send("I couldn't find a mod list in that file. Upload latest.log, a crash "
                                            "report, or a text file with your mod file names.", ephemeral=True)
            return
        cog = self.bot.get_cog("Modrinth")
        projects = await cog._cached_projects() if cog is not None and hasattr(cog, "_cached_projects") else []
        async with aiohttp.ClientSession() as session:
            results = await check_modpack(projects, installed, mc, ldr, session)
        embed = discord.Embed(title="\U0001F9E9 Modpack check", color=0x5865F2,
                              description=f"**{len(installed)}** mods found - Minecraft **{mc or '?'}** - "
                                          f"**{(ldr or '?').title()}**")
        for title, lines in results[:20]:
            embed.add_field(name=title[:256], value="\n".join(lines)[:1024], inline=False)
        if not results:
            embed.add_field(name="None of our mods found", value="None of our mods are in this pack.", inline=False)
        bad = sum(1 for _, lines in results if any(x.startswith(("❌", "⚠")) for x in lines))
        embed.color = 0xED4245 if bad else 0x57F287 if results else 0x95A5A6
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ModpackCheck(bot))
