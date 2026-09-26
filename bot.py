"""Discord bot that builds a complete server from a template.

Run /setup in a server (Administrator only) and the bot creates the
roles, categories, channels, and server settings defined in
server_template.py. Existing items with the same name are left alone,
so /setup is safe to run more than once.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load .env BEFORE any internal module reads os.getenv at import time.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("setup-bot")

from server_template import SERVER_TEMPLATE, CHANNELS
from web import log_buffer
from web.app import app as panel_app, set_bot as set_panel_bot

log_buffer.install()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and add your bot token."
    )

# Delay between each Discord API write during /setup and /serverreset.
# Higher = gentler on CPU/network (useful while gaming). 0 disables throttling.
THROTTLE_SECONDS = float(os.getenv("SETUP_THROTTLE_SECONDS", "0.6"))

# Web panel binding. Bind to 127.0.0.1 locally; switch to 0.0.0.0 when on a real server.
PANEL_HOST = os.getenv("PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(os.getenv("PANEL_PORT", "8080"))
PANEL_ENABLED = os.getenv("PANEL_ENABLED", "true").lower() in ("1", "true", "yes")

# Low-power mode: skip the noisy logging cog and background tasks. Use when running
# on a phone or other power-constrained host to keep CPU/heat low.
LOW_POWER = os.getenv("LOW_POWER", "").lower() in ("1", "true", "yes")


async def throttle() -> None:
    if THROTTLE_SECONDS > 0:
        await asyncio.sleep(THROTTLE_SECONDS)


VERIFICATION_LEVELS = {
    "none": discord.VerificationLevel.none,
    "low": discord.VerificationLevel.low,
    "medium": discord.VerificationLevel.medium,
    "high": discord.VerificationLevel.high,
    "highest": discord.VerificationLevel.highest,
}

NOTIFICATION_LEVELS = {
    "all_messages": discord.NotificationLevel.all_messages,
    "only_mentions": discord.NotificationLevel.only_mentions,
}

CONTENT_FILTERS = {
    "disabled": discord.ContentFilter.disabled,
    "no_role": discord.ContentFilter.no_role,
    "all_members": discord.ContentFilter.all_members,
}

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

VALID_AUTOMOD_PRESETS = {"profanity", "sexual_content", "slurs"}


def build_automod_presets(names):
    flags = {n: True for n in names if n in VALID_AUTOMOD_PRESETS}
    if not flags:
        return None
    return discord.AutoModPresets(**flags)


intents = discord.Intents.default()
intents.members = True  # Required for member join/leave/update events. Enable Server Members Intent in the Developer Portal.
# Required by the anti-spam cog to read message text. Needs the Message Content
# toggle in the Developer Portal too - if that toggle is OFF, the bot fails to
# start; set MESSAGE_CONTENT_INTENT=false in .env to boot without it.
intents.message_content = os.getenv("MESSAGE_CONTENT_INTENT", "true").lower() in ("1", "true", "yes")
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


class RoleToggleButton(discord.ui.Button):
    def __init__(self, role_name, label, emoji, style, row):
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"rr:{role_name}",
            row=row,
        )
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Not in a server.", ephemeral=True)
            return
        role = discord.utils.get(guild.roles, name=self.role_name)
        if role is None:
            await interaction.response.send_message(
                f"Role `{self.role_name}` is missing. Ask an admin to re-run `/setup`.",
                ephemeral=True,
            )
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("Could not resolve your member object.", ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-assign panel")
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Self-assign panel")
                await interaction.response.send_message(f"Added **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't change that role - my role is below it in the hierarchy.",
                ephemeral=True,
            )


class ReactionRolesView(discord.ui.View):
    def __init__(self, buttons_def):
        super().__init__(timeout=None)
        for b in buttons_def:
            self.add_item(RoleToggleButton(
                role_name=b["role"],
                label=b["label"],
                emoji=b.get("emoji"),
                style=BUTTON_STYLES.get(b.get("style", "secondary"), discord.ButtonStyle.secondary),
                row=b.get("row", 0),
            ))


class RulesAcceptView(discord.ui.View):
    def __init__(self, role_name: str = "Member", label: str = "I Accept the Rules", emoji: str = "✅"):
        super().__init__(timeout=None)
        self.add_item(RulesAcceptButton(role_name=role_name, label=label, emoji=emoji))


class RulesAcceptButton(discord.ui.Button):
    def __init__(self, role_name: str, label: str, emoji: str):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.success,
            custom_id="rules:accept",
        )
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        role = discord.utils.get(guild.roles, name=self.role_name)
        if role is None:
            await interaction.response.send_message(
                f"Role `{self.role_name}` is missing. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("Could not resolve your member.", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message(
                f"You already accepted the rules and have the {role.mention} role.",
                ephemeral=True,
            )
            return
        try:
            await member.add_roles(role, reason="Rules accepted")
            await interaction.response.send_message(
                f"Welcome! You now have the {role.mention} role and full access to the server.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't grant that role - tell an admin to move my role above it.",
                ephemeral=True,
            )


if LOW_POWER:
    EXTENSIONS = (
        "cogs.maintenance",
        "cogs.moderation",
        "cogs.tickets",
        "cogs.utility",
        "cogs.backup",
        "cogs.poll",
        "cogs.community",
    )
    log.info("LOW_POWER mode: skipping cogs.logging_cog, modrinth, welcome_dm (background-heavy)")
else:
    EXTENSIONS = (
        "cogs.maintenance",
        "cogs.moderation",
        "cogs.logging_cog",
        "cogs.antispam",
        "cogs.tickets",
        "cogs.modrinth",
        "cogs.welcome_dm",
        "cogs.utility",
        "cogs.backup",
        "cogs.poll",
        "cogs.community",
    )


async def _start_web_panel():
    if not PANEL_ENABLED:
        log.info("Web panel disabled via PANEL_ENABLED=false")
        return
    import uvicorn
    set_panel_bot(bot)
    config = uvicorn.Config(
        panel_app,
        host=PANEL_HOST,
        port=PANEL_PORT,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve(), name="web-panel")
    log.info("Web panel starting at http://%s:%d (login with PANEL_PASSWORD)", PANEL_HOST, PANEL_PORT)


async def _setup_hook():
    panel = SERVER_TEMPLATE.get("reaction_role_panel")
    if panel and panel.get("buttons"):
        bot.add_view(ReactionRolesView(panel["buttons"]))
        log.info("Registered persistent reaction-role view")
    rules_cfg = SERVER_TEMPLATE.get("rules_acceptance", {})
    if rules_cfg.get("enabled"):
        bot.add_view(RulesAcceptView(
            role_name=rules_cfg.get("role_granted", "Member"),
            label=rules_cfg.get("button_label", "I Accept the Rules"),
            emoji=rules_cfg.get("button_emoji", "✅"),
        ))
        log.info("Registered persistent rules-accept view")
    # Register the Modrinth mod-download select as a persistent view (with current mod list)
    modrinth_cfg = SERVER_TEMPLATE.get("modrinth", {})
    if modrinth_cfg.get("enabled") and modrinth_cfg.get("username"):
        try:
            from cogs.modrinth import _fetch_user_projects, ModDownloadView
            mods = await _fetch_user_projects(modrinth_cfg["username"])
            bot.add_view(ModDownloadView(mods or []))
            log.info("Registered persistent mod-download view with %d mods", len(mods or []))
        except Exception:
            log.exception("Failed to register mod-download view; selection will fail until next restart")
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info("Loaded extension: %s", ext)
        except Exception:
            log.exception("Failed to load extension: %s", ext)
    await _start_web_panel()

bot.setup_hook = _setup_hook


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        log.info("Cleared any global command registrations on Discord")
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild '%s'", len(synced), guild.name)
    except Exception:
        log.exception("Failed to sync slash commands")


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("Synced %d command(s) on join to guild '%s'", len(synced), guild.name)
    except Exception:
        log.exception("Failed to sync commands on joining guild '%s'", guild.name)


CHANNEL_MENTION_PATTERN = re.compile(r"<#([^>]+?)>")
_KEYWORD_PREFIX = re.compile(r"^[\W_]+", re.UNICODE)


def channel_keyword(name: str) -> str:
    """Strip leading emoji/symbol prefix and lowercase. '📋-rules' -> 'rules'."""
    return _KEYWORD_PREFIX.sub("", name).lower().strip()


def find_existing_channel(guild: discord.Guild, template_name: str, channel_type: str = "text"):
    """Match by exact name first, then by emoji-stripped keyword.

    `channel_type` filters the candidate pool so a category like '💬 GENERAL'
    isn't returned when looking for the text channel '💬-general'.
    """
    if channel_type == "voice":
        candidates = list(guild.voice_channels)
    elif channel_type == "stage":
        candidates = list(guild.stage_channels)
    elif channel_type == "forum":
        candidates = list(guild.forums)
    else:
        candidates = list(guild.text_channels)

    for ch in candidates:
        if ch.name == template_name:
            return ch
    target = channel_keyword(template_name)
    if not target:
        return None
    for ch in candidates:
        if channel_keyword(ch.name) == target:
            return ch
    return None


def resolve_channel_mentions(text: str, guild: discord.Guild) -> str:
    """Replace <#channel-name> with the actual channel mention if found."""
    def replace(match):
        name = match.group(1)
        ch = discord.utils.get(guild.channels, name=name)
        return ch.mention if ch else f"#{name}"
    return CHANNEL_MENTION_PATTERN.sub(replace, text)


def color_from_hex(value):
    if isinstance(value, int):
        return discord.Color(value)
    text = str(value).strip().lstrip("#")
    if text.lower().startswith("0x"):
        text = text[2:]
    return discord.Color(int(text, 16))


def make_overwrite(perms):
    return discord.PermissionOverwrite(**perms)


def resolve_overwrites(guild, overwrites_def, role_lookup):
    result = {}
    for role_name, perms in overwrites_def.items():
        if role_name == "@everyone":
            target = guild.default_role
        else:
            target = role_lookup.get(role_name) or discord.utils.get(
                guild.roles, name=role_name
            )
        if target:
            result[target] = make_overwrite(perms)
    return result


async def ensure_roles(guild, template_roles, update_existing=False):
    role_lookup = {}
    created, updated = [], []
    for role_def in reversed(template_roles):
        name = role_def["name"]
        existing = discord.utils.get(guild.roles, name=name)
        if existing:
            role_lookup[name] = existing
            if update_existing and not existing.managed and existing < guild.me.top_role:
                desired_perms = discord.Permissions(**role_def.get("permissions", {}))
                desired_color = color_from_hex(role_def.get("color", "0x99AAB5"))
                desired_hoist = role_def.get("hoist", False)
                desired_mentionable = role_def.get("mentionable", False)
                if (existing.permissions != desired_perms
                        or existing.color != desired_color
                        or existing.hoist != desired_hoist
                        or existing.mentionable != desired_mentionable):
                    try:
                        await existing.edit(
                            permissions=desired_perms,
                            color=desired_color,
                            hoist=desired_hoist,
                            mentionable=desired_mentionable,
                            reason="Server update",
                        )
                        updated.append(name)
                        log.info("Updated role: %s", name)
                        await throttle()
                    except discord.Forbidden:
                        log.warning("Missing permission to update role: %s", name)
                    except Exception:
                        log.exception("Failed to update role: %s", name)
            continue
        try:
            role = await guild.create_role(
                name=name,
                permissions=discord.Permissions(**role_def.get("permissions", {})),
                color=color_from_hex(role_def.get("color", "0x99AAB5")),
                hoist=role_def.get("hoist", False),
                mentionable=role_def.get("mentionable", False),
                reason="Server setup",
            )
            role_lookup[name] = role
            created.append(name)
            log.info("Created role: %s", name)
            await throttle()
        except discord.Forbidden:
            log.warning("Missing permission to create role: %s", name)
        except Exception:
            log.exception("Failed to create role: %s", name)
    return role_lookup, created, updated


async def ensure_categories_and_channels(guild, template, role_lookup, update_existing=False):
    created_channels = []
    updated_channels = []
    categories_created = []
    categories_updated = []
    for cat_def in template["categories"]:
        cat_overwrites = resolve_overwrites(
            guild, cat_def.get("overwrites", {}), role_lookup
        )
        category = discord.utils.get(guild.categories, name=cat_def["name"])
        if not category:
            try:
                category = await guild.create_category(
                    name=cat_def["name"],
                    overwrites=cat_overwrites,
                    reason="Server setup",
                )
                log.info("Created category: %s", cat_def["name"])
                categories_created.append(cat_def["name"])
                await throttle()
            except Exception:
                log.exception("Failed to create category: %s", cat_def["name"])
                continue
        elif update_existing:
            if dict(category.overwrites) != cat_overwrites:
                try:
                    await category.edit(overwrites=cat_overwrites, reason="Server update")
                    categories_updated.append(cat_def["name"])
                    log.info("Updated category overwrites: %s", cat_def["name"])
                    await throttle()
                except discord.Forbidden:
                    log.warning("Missing permission to update category: %s", cat_def["name"])
                except Exception:
                    log.exception("Failed to update category: %s", cat_def["name"])
        else:
            log.info("Category already exists: %s", cat_def["name"])

        for ch_def in cat_def.get("channels", []):
            ch_type = ch_def.get("type", "text")
            existing = find_existing_channel(guild, ch_def["name"], ch_type)
            if existing:
                # Adopt: rename to template name, move to template category if needed.
                edits = {}
                if existing.name != ch_def["name"]:
                    edits["name"] = ch_def["name"]
                if getattr(existing, "category_id", None) != category.id:
                    edits["category"] = category

                if update_existing:
                    ow_def = ch_def.get("overwrites")
                    if ow_def is not None:
                        desired = resolve_overwrites(guild, ow_def, role_lookup)
                        if dict(existing.overwrites) != desired:
                            edits["overwrites"] = desired
                    elif not getattr(existing, "permissions_synced", True):
                        edits["sync_permissions"] = True
                    if isinstance(existing, discord.TextChannel):
                        if (ch_def.get("topic") or None) != (existing.topic or None):
                            edits["topic"] = ch_def.get("topic")
                        if ch_def.get("slowmode", 0) != existing.slowmode_delay:
                            edits["slowmode_delay"] = ch_def.get("slowmode", 0)
                    elif isinstance(existing, discord.ForumChannel):
                        if (ch_def.get("topic") or None) != (existing.topic or None):
                            edits["topic"] = ch_def.get("topic")
                    elif isinstance(existing, discord.VoiceChannel):
                        if ch_def.get("user_limit", 0) != existing.user_limit:
                            edits["user_limit"] = ch_def.get("user_limit", 0)

                if edits:
                    try:
                        await existing.edit(
                            reason="Server update" if update_existing else "Server setup adoption",
                            **edits,
                        )
                        updated_channels.append(ch_def["name"])
                        log.info("Updated channel %s (%s)", ch_def["name"], ", ".join(edits.keys()))
                        await throttle()
                    except discord.Forbidden:
                        log.warning("Missing permission to update channel: %s", existing.name)
                    except Exception:
                        log.exception("Failed to update channel: %s", existing.name)
                else:
                    log.info("Channel already up to date: %s", ch_def["name"])
                continue

            ch_overwrites = resolve_overwrites(
                guild, ch_def.get("overwrites", {}), role_lookup
            )
            try:
                if ch_type == "text":
                    channel = await guild.create_text_channel(
                        name=ch_def["name"],
                        category=category,
                        topic=ch_def.get("topic"),
                        slowmode_delay=ch_def.get("slowmode", 0),
                        nsfw=ch_def.get("nsfw", False),
                        overwrites=ch_overwrites,
                        reason="Server setup",
                    )
                elif ch_type == "voice":
                    channel = await guild.create_voice_channel(
                        name=ch_def["name"],
                        category=category,
                        user_limit=ch_def.get("user_limit", 0),
                        overwrites=ch_overwrites,
                        reason="Server setup",
                    )
                elif ch_type == "stage":
                    channel = await guild.create_stage_channel(
                        name=ch_def["name"],
                        category=category,
                        overwrites=ch_overwrites,
                        reason="Server setup",
                    )
                elif ch_type == "forum":
                    channel = await guild.create_forum(
                        name=ch_def["name"],
                        category=category,
                        topic=ch_def.get("topic"),
                        overwrites=ch_overwrites,
                        reason="Server setup",
                    )
                else:
                    log.warning("Unknown channel type %r for %s", ch_type, ch_def["name"])
                    continue
                created_channels.append(channel)
                log.info("Created %s channel: %s", ch_type, ch_def["name"])
                await throttle()
            except Exception:
                log.exception("Failed to create channel: %s", ch_def["name"])
    return {
        "created": created_channels,
        "updated": updated_channels,
        "categories_created": categories_created,
        "categories_updated": categories_updated,
    }


async def apply_server_settings(guild, settings):
    if not settings:
        return []
    kwargs = {}
    applied = []

    if "verification_level" in settings:
        level = VERIFICATION_LEVELS.get(settings["verification_level"])
        if level is not None:
            kwargs["verification_level"] = level
            applied.append("verification_level")

    if "default_notifications" in settings:
        notif = NOTIFICATION_LEVELS.get(settings["default_notifications"])
        if notif is not None:
            kwargs["default_notifications"] = notif
            applied.append("default_notifications")

    if "explicit_content_filter" in settings:
        cf = CONTENT_FILTERS.get(settings["explicit_content_filter"])
        if cf is not None:
            kwargs["explicit_content_filter"] = cf
            applied.append("explicit_content_filter")

    if "description" in settings:
        if "COMMUNITY" in guild.features:
            kwargs["description"] = settings["description"]
            applied.append("description")
        else:
            log.info("Server description skipped: requires Community Server")

    if "afk_channel" in settings:
        afk = discord.utils.get(guild.voice_channels, name=settings["afk_channel"])
        if afk:
            kwargs["afk_channel"] = afk
            applied.append("afk_channel")

    if "afk_timeout" in settings:
        kwargs["afk_timeout"] = int(settings["afk_timeout"])
        applied.append("afk_timeout")

    if "system_channel" in settings:
        sysc = discord.utils.get(guild.text_channels, name=settings["system_channel"])
        if sysc:
            kwargs["system_channel"] = sysc
            applied.append("system_channel")

    if not kwargs:
        return applied
    try:
        await guild.edit(**kwargs, reason="Server setup")
        log.info("Applied server settings: %s", applied)
    except discord.Forbidden:
        log.warning("Missing permission to edit guild settings")
    except Exception:
        log.exception("Failed to edit guild settings")
    return applied


async def create_automod_rules(guild, config):
    if not config or not config.get("enabled"):
        return []

    alert_channel = None
    if config.get("alert_channel"):
        alert_channel = discord.utils.get(guild.text_channels, name=config["alert_channel"])

    actions = [discord.AutoModRuleAction()]
    if alert_channel:
        actions.append(discord.AutoModRuleAction(channel_id=alert_channel.id))

    try:
        existing = await guild.fetch_automod_rules()
        existing_names = {r.name for r in existing}
    except (discord.Forbidden, discord.HTTPException):
        existing_names = set()

    rules = config.get("rules", {})
    rule_defs = []
    if rules.get("spam"):
        rule_defs.append((
            "Block Spam Content",
            discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam),
        ))
    if rules.get("mention_spam_limit"):
        rule_defs.append((
            "Block Mention Spam",
            discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.mention_spam,
                mention_limit=int(rules["mention_spam_limit"]),
            ),
        ))
    if rules.get("keyword_presets"):
        presets = build_automod_presets(rules["keyword_presets"])
        if presets:
            rule_defs.append((
                "Block Offensive Language",
                discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword_preset,
                    presets=presets,
                ),
            ))

    created = []
    for name, trigger in rule_defs:
        if name in existing_names:
            log.info("AutoMod rule already exists: %s", name)
            continue
        try:
            rule = await guild.create_automod_rule(
                name=name,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason="Server setup",
            )
            created.append(rule)
            log.info("Created AutoMod rule: %s", name)
            await throttle()
        except discord.Forbidden:
            log.warning("Missing permission for AutoMod rule: %s", name)
        except Exception:
            log.exception("Failed to create AutoMod rule: %s", name)
    return created


ONBOARDING_PROMPT_TYPES = {
    "multiple_choice": discord.OnboardingPromptType.multiple_choice,
    "dropdown": discord.OnboardingPromptType.dropdown,
}

ONBOARDING_MODES = {
    "default": discord.OnboardingMode.default,
    "advanced": discord.OnboardingMode.advanced,
}


async def configure_onboarding(guild, template, role_lookup):
    config = template.get("onboarding", {})
    if not config.get("enabled"):
        return False
    if "COMMUNITY" not in guild.features:
        log.info("Onboarding skipped: server is not a Community Server")
        return False

    default_channels = []
    for key in config.get("default_channel_keys", []):
        channel_name = CHANNELS.get(key)
        if not channel_name:
            continue
        ch = discord.utils.get(guild.text_channels, name=channel_name)
        if ch is None:
            continue
        if not ch.permissions_for(guild.default_role).view_channel:
            log.info("Skipping onboarding default channel %s (@everyone can't view)", ch.name)
            continue
        default_channels.append(ch)

    prompts = []
    for prompt_def in config.get("prompts", []):
        prompt_type = ONBOARDING_PROMPT_TYPES.get(prompt_def.get("type", "multiple_choice"))
        if prompt_type is None:
            continue
        options = []
        for opt in prompt_def.get("options", []):
            role_obj = None
            role_name = opt.get("role")
            if role_name:
                role_obj = role_lookup.get(role_name) or discord.utils.get(guild.roles, name=role_name)
            opt_kwargs = {"title": opt["title"]}
            if opt.get("description"):
                opt_kwargs["description"] = opt["description"]
            if opt.get("emoji"):
                opt_kwargs["emoji"] = opt["emoji"]
            if role_obj:
                opt_kwargs["roles"] = [role_obj]
            options.append(discord.OnboardingPromptOption(**opt_kwargs))
        if not options:
            continue
        prompts.append(discord.OnboardingPrompt(
            type=prompt_type,
            title=prompt_def["title"],
            options=options,
            single_select=prompt_def.get("single_select", True),
            required=prompt_def.get("required", False),
            in_onboarding=True,
        ))

    if not prompts:
        return False

    try:
        await guild.edit_onboarding(
            prompts=prompts,
            default_channels=default_channels,
            enabled=True,
            mode=ONBOARDING_MODES.get(config.get("mode", "default"), discord.OnboardingMode.default),
            reason="Server setup",
        )
        log.info("Configured onboarding with %d prompts and %d default channels", len(prompts), len(default_channels))
        return True
    except discord.Forbidden:
        log.warning("Missing permission to edit onboarding")
        return False
    except Exception:
        log.exception("Failed to configure onboarding")
        return False


async def configure_welcome_screen(guild, template):
    config = template.get("welcome_screen", {})
    if not config.get("enabled"):
        return False
    if "COMMUNITY" not in guild.features:
        log.info("Welcome screen skipped: server is not a Community Server")
        return False
    welcome_channels = []
    for cc in config.get("channels", []):
        ch = discord.utils.get(guild.text_channels, name=cc["channel"])
        if ch is None:
            continue
        if not ch.permissions_for(guild.default_role).view_channel:
            log.info("Skipping welcome-screen channel %s (@everyone can't view)", ch.name)
            continue
        welcome_channels.append(discord.WelcomeChannel(
            channel=ch,
            description=cc.get("description", ""),
            emoji=cc.get("emoji"),
        ))
    if not welcome_channels:
        return False
    try:
        await guild.edit_welcome_screen(
            enabled=True,
            description=config.get("description", "Welcome!"),
            welcome_channels=welcome_channels,
        )
        log.info("Configured welcome screen with %d channels", len(welcome_channels))
        return True
    except discord.Forbidden:
        log.warning("Missing permission to set welcome screen")
        return False
    except Exception:
        log.exception("Failed to configure welcome screen")
        return False


def build_roles_panel(template):
    panel = template.get("reaction_role_panel")
    if not panel or not panel.get("buttons"):
        return None, None
    embed = discord.Embed(
        title=panel.get("title", "Self-Assign Roles"),
        description=panel.get("description", ""),
        color=0x5865F2,
    )
    return embed, ReactionRolesView(panel["buttons"])


def build_ticket_panel(template):
    types_def = template.get("tickets", {}).get("types", [])
    if not types_def:
        return None, None
    from cogs.tickets import TicketPanelView
    lines = []
    for t in types_def:
        first_line = (t.get("intro", "") or "").splitlines()[0][:100] if t.get("intro") else ""
        lines.append(f"{t.get('emoji', '')} **{t['label']}** - {first_line}")
    embed = discord.Embed(
        title="Open a Ticket",
        description="Pick the type that fits your issue. Each ticket is a private channel between you and the staff.\n\n" + "\n".join(lines),
        color=0x5865F2,
    )
    return embed, TicketPanelView(types_def)


def build_mod_download_panel(template, mods):
    config = template.get("modrinth", {})
    from cogs.modrinth import ModDownloadView
    embed = discord.Embed(
        title="Download my mods",
        description=(
            f"Pick a mod from the dropdown below to get a **direct download link** "
            f"to its latest version.\n\n"
            f"All projects: https://modrinth.com/user/{config['username']}"
        ),
        color=0x1bd96a,
    )
    for mod in mods[:10]:
        desc = (mod.get("description") or "").strip()[:120] or "—"
        embed.add_field(name=mod.get("title", "Unknown"), value=desc, inline=False)
    return embed, ModDownloadView(mods)


async def post_mod_download_panel(guild, template, bot_user):
    config = template.get("modrinth", {})
    if not config.get("enabled") or not config.get("username"):
        return False
    channel_name = CHANNELS.get("mod_downloads")
    if not channel_name:
        return False
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel is None:
        return False
    if await channel_has_bot_messages(channel, bot_user):
        return False
    try:
        from cogs.modrinth import _fetch_user_projects
        mods = await _fetch_user_projects(config["username"])
    except Exception:
        log.exception("Failed to fetch Modrinth projects for download panel")
        return False
    if not mods:
        return False
    embed, view = build_mod_download_panel(template, mods)
    try:
        await channel.send(embed=embed, view=view)
        log.info("Posted mod-download panel with %d mods", len(mods))
        return True
    except Exception:
        log.exception("Failed to post mod download panel")
        return False


async def post_ticket_panel(guild, template, bot_user):
    channel = discord.utils.get(guild.text_channels, name=CHANNELS["tickets"])
    if channel is None:
        return False
    if await channel_has_bot_messages(channel, bot_user):
        return False
    embed, view = build_ticket_panel(template)
    if embed is None:
        return False
    try:
        await channel.send(embed=embed, view=view)
        log.info("Posted ticket panel to #%s", channel.name)
        return True
    except Exception:
        log.exception("Failed to post ticket panel")
        return False


async def post_reaction_role_panel(guild, template, bot_user):
    panel = template.get("reaction_role_panel")
    if not panel or not panel.get("buttons"):
        return False
    channel_name = panel["channel"]
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        return False
    if await channel_has_bot_messages(channel, bot_user):
        return False
    embed, view = build_roles_panel(template)
    try:
        await channel.send(embed=embed, view=view)
        log.info("Posted reaction-role panel to #%s", channel.name)
        return True
    except Exception:
        log.exception("Failed to post reaction-role panel")
        return False


async def channel_has_bot_messages(channel, bot_user):
    try:
        async for msg in channel.history(limit=20):
            if msg.author.id == bot_user.id:
                return True
    except discord.Forbidden:
        return True
    return False


def build_rules_embed_view(guild, template):
    embed = discord.Embed(
        title=f"{guild.name} - Rules",
        description=template["rules_message"],
        color=0x5865F2,
    )
    embed.set_footer(text="Breaking these rules may result in warnings, mutes, or bans.")
    rules_cfg = template.get("rules_acceptance", {})
    view = None
    if rules_cfg.get("enabled"):
        view = RulesAcceptView(
            role_name=rules_cfg.get("role_granted", "Member"),
            label=rules_cfg.get("button_label", "I Accept the Rules"),
            emoji=rules_cfg.get("button_emoji", "✅"),
        )
    return embed, view


def build_welcome_embed(guild, template):
    embed = discord.Embed(
        title=f"Welcome to {guild.name}!",
        description=resolve_channel_mentions(template["welcome_message"], guild),
        color=0x57F287,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def find_latest_bot_message(channel, bot_user, limit=50):
    try:
        async for msg in channel.history(limit=limit):
            if msg.author.id == bot_user.id:
                return msg
    except discord.Forbidden:
        return None
    return None


async def refresh_or_post(channel, bot_user, embed, view=None):
    """Edit the bot's latest message in the channel, or post a new one."""
    msg = await find_latest_bot_message(channel, bot_user)
    try:
        if msg is not None:
            await msg.edit(embed=embed, view=view)
            return "refreshed"
        if view is not None:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)
        return "posted"
    except Exception:
        log.exception("Failed to refresh panel in #%s", channel.name)
        return None


async def refresh_panels(guild, template, bot_user):
    """Bring every bot panel up to date by editing in place (or posting if missing)."""
    results = {}

    rules_ch = discord.utils.get(guild.text_channels, name=CHANNELS["rules"])
    if rules_ch and template.get("rules_message"):
        embed, view = build_rules_embed_view(guild, template)
        results["rules"] = await refresh_or_post(rules_ch, bot_user, embed, view)
        await throttle()

    welcome_ch = discord.utils.get(guild.text_channels, name=CHANNELS["welcome"])
    if welcome_ch and template.get("welcome_message"):
        results["welcome"] = await refresh_or_post(welcome_ch, bot_user, build_welcome_embed(guild, template))
        await throttle()

    roles_ch = discord.utils.get(guild.text_channels, name=CHANNELS["roles"])
    embed, view = build_roles_panel(template)
    if roles_ch and embed is not None:
        results["roles_panel"] = await refresh_or_post(roles_ch, bot_user, embed, view)
        await throttle()

    tickets_ch = discord.utils.get(guild.text_channels, name=CHANNELS["tickets"])
    embed, view = build_ticket_panel(template)
    if tickets_ch and embed is not None:
        results["ticket_panel"] = await refresh_or_post(tickets_ch, bot_user, embed, view)
        await throttle()

    config = template.get("modrinth", {})
    downloads_ch = discord.utils.get(guild.text_channels, name=CHANNELS.get("mod_downloads", ""))
    if downloads_ch and config.get("enabled") and config.get("username"):
        try:
            from cogs.modrinth import _fetch_user_projects
            mods = await _fetch_user_projects(config["username"])
        except Exception:
            log.exception("Failed to fetch Modrinth projects for panel refresh")
            mods = None
        if mods:
            embed, view = build_mod_download_panel(template, mods)
            results["mod_download_panel"] = await refresh_or_post(downloads_ch, bot_user, embed, view)
            await throttle()

    return results


async def post_update_log(guild, embed):
    """Post a setup/update summary embed into the staff bot-updates channel."""
    channel = discord.utils.get(guild.text_channels, name=CHANNELS["bot_updates"])
    if channel is None:
        return False
    try:
        await channel.send(embed=embed)
        return True
    except Exception:
        log.exception("Failed to post update log")
        return False


async def post_welcome_and_rules(guild, template, bot_user):
    rules_ch = discord.utils.get(guild.text_channels, name=CHANNELS["rules"])
    welcome_ch = discord.utils.get(guild.text_channels, name=CHANNELS["welcome"])

    if rules_ch and template.get("rules_message"):
        if not await channel_has_bot_messages(rules_ch, bot_user):
            embed, view = build_rules_embed_view(guild, template)
            try:
                if view is not None:
                    await rules_ch.send(embed=embed, view=view)
                else:
                    await rules_ch.send(embed=embed)
                log.info("Posted rules embed (accept button: %s)", view is not None)
            except Exception:
                log.exception("Failed to post rules embed")

    if welcome_ch and template.get("welcome_message"):
        if not await channel_has_bot_messages(welcome_ch, bot_user):
            embed = build_welcome_embed(guild, template)
            try:
                await welcome_ch.send(embed=embed)
                log.info("Posted welcome embed")
            except Exception:
                log.exception("Failed to post welcome embed")


@bot.tree.command(
    name="setup",
    description="Set up a complete server with roles, channels, and settings.",
)
@app_commands.default_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You need the Administrator permission to run this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command must be used inside a server.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    log.info("Starting setup for guild: %s (ID: %s)", guild.name, guild.id)

    role_lookup, roles_created, roles_updated = await ensure_roles(guild, SERVER_TEMPLATE["roles"])
    ch_result = await ensure_categories_and_channels(
        guild, SERVER_TEMPLATE, role_lookup
    )
    new_channels = ch_result["created"]
    applied_settings = await apply_server_settings(
        guild, SERVER_TEMPLATE.get("server_settings", {})
    )
    await post_welcome_and_rules(guild, SERVER_TEMPLATE, bot.user)
    automod_created = await create_automod_rules(guild, SERVER_TEMPLATE.get("automod", {}))
    panel_posted = await post_reaction_role_panel(guild, SERVER_TEMPLATE, bot.user)
    ticket_panel_posted = await post_ticket_panel(guild, SERVER_TEMPLATE, bot.user)
    mod_download_posted = await post_mod_download_panel(guild, SERVER_TEMPLATE, bot.user)
    welcome_screen_set = await configure_welcome_screen(guild, SERVER_TEMPLATE)
    onboarding_set = await configure_onboarding(guild, SERVER_TEMPLATE, role_lookup)

    summary_lines = [
        "**Server setup complete.**",
        f"- Roles ready: **{len(role_lookup)}**",
        f"- New channels created: **{len(new_channels)}**",
        f"- Server settings applied: **{len(applied_settings)}**"
        + (f" ({', '.join(applied_settings)})" if applied_settings else ""),
        f"- AutoMod rules created: **{len(automod_created)}**",
        f"- Reaction-role panel: **{'posted' if panel_posted else 'skipped (already exists or channel missing)'}**",
        f"- Ticket panel: **{'posted to #' + CHANNELS['tickets'] if ticket_panel_posted else 'skipped (already exists or channel missing)'}**",
        f"- Mod-download panel: **{'posted to #' + CHANNELS['mod_downloads'] if mod_download_posted else 'skipped (already exists or channel missing)'}**",
        f"- Welcome screen: **{'configured' if welcome_screen_set else 'skipped (needs Community Server)'}**",
        f"- Onboarding flow: **{'configured' if onboarding_set else 'skipped (needs Community Server)'}**",
        "- Rules and welcome embeds posted where missing.",
        "",
        "Tip: drag your bot's role above the new roles so it can manage them.",
        "Use `/ticket-panel` to post the multi-button support panel in a channel.",
    ]
    log_embed = discord.Embed(
        title="Server Setup Run",
        description="\n".join(summary_lines),
        color=0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    log_embed.set_footer(text=f"Triggered by {interaction.user}")
    await post_update_log(guild, log_embed)

    await interaction.followup.send("\n".join(summary_lines), ephemeral=True)


@bot.tree.command(
    name="update",
    description="Update the server to the latest template: fix permissions, refresh panels, add what's missing.",
)
@app_commands.default_permissions(administrator=True)
async def update_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You need the Administrator permission to run this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command must be used inside a server.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    log.info("Starting update for guild: %s (ID: %s)", guild.name, guild.id)

    role_lookup, roles_created, roles_updated = await ensure_roles(
        guild, SERVER_TEMPLATE["roles"], update_existing=True
    )
    ch_result = await ensure_categories_and_channels(
        guild, SERVER_TEMPLATE, role_lookup, update_existing=True
    )
    applied_settings = await apply_server_settings(
        guild, SERVER_TEMPLATE.get("server_settings", {})
    )
    automod_created = await create_automod_rules(guild, SERVER_TEMPLATE.get("automod", {}))
    welcome_screen_set = await configure_welcome_screen(guild, SERVER_TEMPLATE)
    onboarding_set = await configure_onboarding(guild, SERVER_TEMPLATE, role_lookup)
    panel_results = await refresh_panels(guild, SERVER_TEMPLATE, bot.user)

    panels_str = ", ".join(
        f"{name} {status}" for name, status in panel_results.items() if status
    ) or "none"
    summary_lines = [
        "**Server update complete.** Nothing was deleted.",
        f"- Roles: **{len(roles_created)} created**, **{len(roles_updated)} updated**",
        f"- Channels: **{len(ch_result['created'])} created**, **{len(ch_result['updated'])} updated**",
        f"- Categories: **{len(ch_result['categories_created'])} created**, **{len(ch_result['categories_updated'])} updated**",
        f"- Server settings applied: **{len(applied_settings)}**"
        + (f" ({', '.join(applied_settings)})" if applied_settings else ""),
        f"- AutoMod rules created: **{len(automod_created)}**",
        f"- Welcome screen: **{'configured' if welcome_screen_set else 'skipped'}**",
        f"- Onboarding: **{'configured' if onboarding_set else 'skipped'}**",
        f"- Panels: {panels_str}",
    ]

    log_embed = discord.Embed(
        title="Server Update Run",
        description="\n".join(summary_lines),
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )
    log_embed.set_footer(text=f"Triggered by {interaction.user}")
    await post_update_log(guild, log_embed)

    await interaction.followup.send("\n".join(summary_lines), ephemeral=True)


class ConfirmResetView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the user who ran the command can confirm this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Resetting server...", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Reset cancelled.", view=self)
        self.stop()


def classify_roles_for_reset(guild: discord.Guild):
    me_top = guild.me.top_role
    deletable, skipped = [], []
    for role in guild.roles:
        if role.is_default():
            continue
        if role.managed or role >= me_top:
            skipped.append(role)
        else:
            deletable.append(role)
    return deletable, skipped


@bot.tree.command(
    name="serverreset",
    description="DANGER: delete all channels and roles (managed bot roles are kept).",
)
@app_commands.default_permissions(administrator=True)
async def serverreset(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You need the Administrator permission to run this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command must be used inside a server.", ephemeral=True
        )
        return

    channels = list(guild.channels)
    deletable_roles, skipped_roles = classify_roles_for_reset(guild)

    view = ConfirmResetView(author_id=interaction.user.id)
    warning = (
        f"**This will delete EVERYTHING on `{guild.name}`:**\n"
        f"- **{len(channels)}** channels (including categories)\n"
        f"- **{len(deletable_roles)}** roles\n\n"
        f"Kept: @everyone, **{len(skipped_roles)}** managed/protected roles "
        f"(bot integrations and any role above the bot's highest role).\n\n"
        f"Click **Confirm reset** within 30 seconds. This cannot be undone."
    )
    await interaction.response.send_message(warning, view=view, ephemeral=True)
    await view.wait()

    if view.confirmed is None:
        await interaction.followup.send("Confirmation timed out. No changes made.", ephemeral=True)
        return
    if view.confirmed is False:
        return

    deleted_channels = 0
    failed_channels = 0
    for channel in channels:
        try:
            await channel.delete(reason=f"Server reset by {interaction.user}")
            deleted_channels += 1
            await throttle()
        except discord.Forbidden:
            failed_channels += 1
            log.warning("Missing permission to delete channel: %s", channel.name)
        except discord.NotFound:
            pass
        except Exception:
            failed_channels += 1
            log.exception("Failed to delete channel: %s", channel.name)

    deleted_roles = 0
    failed_roles = 0
    for role in deletable_roles:
        try:
            await role.delete(reason=f"Server reset by {interaction.user}")
            deleted_roles += 1
            await throttle()
        except discord.Forbidden:
            failed_roles += 1
            log.warning("Missing permission to delete role: %s", role.name)
        except discord.NotFound:
            pass
        except Exception:
            failed_roles += 1
            log.exception("Failed to delete role: %s", role.name)

    summary = (
        "**Server reset complete.**\n"
        f"- Channels deleted: **{deleted_channels}**" + (f" ({failed_channels} failed)" if failed_channels else "") + "\n"
        f"- Roles deleted: **{deleted_roles}**" + (f" ({failed_roles} failed)" if failed_roles else "") + "\n"
        f"- Kept: **{len(skipped_roles)}** managed/protected roles + @everyone\n\n"
        "Run `/setup` to rebuild the server."
    )
    try:
        await interaction.followup.send(summary, ephemeral=True)
    except Exception:
        log.info("Reset done but followup could not be delivered")


@bot.tree.command(
    name="setup-preview",
    description="Show what /setup will create on this server.",
)
async def setup_preview(interaction: discord.Interaction):
    template = SERVER_TEMPLATE
    embed = discord.Embed(
        title="Server Setup Preview",
        description=(
            "Running `/setup` will create the structure below. "
            "Existing roles and channels with the same name are left untouched."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="Roles",
        value=", ".join(r["name"] for r in template["roles"]) or "(none)",
        inline=False,
    )
    for cat in template["categories"]:
        channel_lines = []
        for ch in cat.get("channels", []):
            kind = ch.get("type", "text")
            prefix = {"text": "#", "voice": "[VC]", "stage": "[STAGE]", "forum": "[FORUM]"}.get(kind, "#")
            channel_lines.append(f"{prefix} {ch['name']}")
        embed.add_field(
            name=cat["name"],
            value="\n".join(channel_lines) or "(empty)",
            inline=True,
        )
    settings = template.get("server_settings", {})
    if settings:
        embed.add_field(
            name="Server Settings",
            value="\n".join(f"- {k}: `{v}`" for k, v in settings.items()),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
