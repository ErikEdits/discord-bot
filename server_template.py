"""Declarative description of the server.

CHANNELS is the central name map. Code (cogs, bot.py) imports this dict
to resolve well-known channels, so renaming a channel in CHANNELS
propagates to every lookup automatically.
"""

# Display names for well-known channels. Any rename happens here.
CHANNELS = {
    "welcome":         "\U0001F44B-welcome",
    "rules":           "\U0001F4CB-rules",
    "announcements":   "\U0001F4E2-announcements",
    "server_updates":  "\U0001F514-server-updates",
    "roles":           "\U0001F3AD-roles",
    "tickets":         "\U0001F3AB-tickets",
    "mod_releases":    "\U0001F514-mod-releases",
    "mod_downloads":   "\U0001F4E6-mod-downloads",
    "mod_stats":       "\U0001F4C8-mod-stats",
    "crash_analyzer":  "\U0001F50D-crash-analyzer",
    "roadmap":         "\U0001F5FA-roadmap",
    "compatibility":   "\U0001F9E9-compatibility",
    "staff_team":      "\U0001F46E-staff-team",
    "counting":        "\U0001F522-counting",
    "beta_program":    "\U0001F9EA-beta-program",
    "beta_testing":    "\U0001F9EA-beta-testing",
    "beta_applications": "\U0001F4DD-beta-applications",
    "general":         "\U0001F4AC-general",
    "introductions":   "\U0001F64B-introductions",
    "off_topic":       "\U0001F3B2-off-topic",
    "media":           "\U0001F5BC-media",
    "memes":           "\U0001F602-memes",
    "bot_commands":    "\U0001F916-bot-commands",
    "suggestions":     "\U0001F4A1-suggestions",
    "events":          "\U0001F389-events",
    "polls":           "\U0001F4CA-polls",
    "level_ups":       "\U0001F199-level-ups",
    "art":             "\U0001F3A8-art-and-creations",
    "mod_support":     "\U0001F6E0-mod-support",
    "staff_chat":      "\U0001F6E1-staff-chat",
    "mod_logs":        "\U0001F4DC-mod-logs",
    "message_logs":    "\U0001F4AC-message-logs",
    "member_logs":     "\U0001F465-member-logs",
    "voice_logs":      "\U0001F50A-voice-logs",
    "server_logs":     "\U0001F5C4-server-logs",
    "bot_updates":     "\U0001F4E3-bot-updates",
    "avatar_logs":     "\U0001F5BC-avatar-logs",
    "ticket_archive":  "\U0001F4C1-ticket-archive",
    "admin_only":      "\U0001F512-admin-only",
    "staff_vc":        "\U0001F6E1 Staff VC",
    "general_vc":      "\U0001F50A General VC",
    "gaming_vc":       "\U0001F3AE Gaming",
    "music_vc":        "\U0001F3B5 Music",
    "chill_vc":        "\U0001F319 Chill",
    "study_vc":        "\U0001F4DA Study Hall",
    "afk_vc":          "\U0001F4A4 AFK",
    "create_vc":       "\u2795 Create Voice",
}


# Profile picture changes are only for administrators (not moderators), so this log
# channel overrides the LOGS category permissions. Also used by cogs/avatar_log.py
# when it has to create the channel itself.
AVATAR_LOG_OVERWRITES = {
    "@everyone": {"view_channel": False},
    "Moderator": {"view_channel": False},
    "Admin":     {"view_channel": True, "send_messages": False, "read_message_history": True},
    "Owner":     {"view_channel": True, "send_messages": True,  "read_message_history": True},
}


SERVER_TEMPLATE = {
    "roles": [
        {"name": "Owner",      "color": "0xE74C3C", "hoist": True,  "mentionable": True,  "permissions": {"administrator": True}},
        {"name": "Admin",      "color": "0xE67E22", "hoist": True,  "mentionable": True,  "permissions": {"administrator": True}},
        {"name": "Moderator",  "color": "0xF1C40F", "hoist": True,  "mentionable": True,
         "permissions": {
             "kick_members": True, "ban_members": True, "manage_messages": True,
             "manage_nicknames": True, "moderate_members": True, "view_audit_log": True,
             "manage_threads": True, "mute_members": True, "deafen_members": True,
             "move_members": True, "view_channel": True, "send_messages": True,
             "read_message_history": True, "connect": True, "speak": True,
             "embed_links": True, "attach_files": True, "add_reactions": True,
             "external_emojis": True, "use_external_stickers": True,
             "change_nickname": True, "use_application_commands": True,
         }},
        {"name": "Bot",        "color": "0x7289DA", "hoist": True,  "mentionable": False,
         "permissions": {
             "view_channel": True, "send_messages": True, "embed_links": True,
             "attach_files": True, "read_message_history": True, "add_reactions": True,
             "use_external_emojis": True, "use_application_commands": True,
         }},
        {"name": "VIP",        "color": "0x9B59B6", "hoist": True,  "mentionable": True,
         "permissions": {
             "view_channel": True, "send_messages": True, "embed_links": True,
             "attach_files": True, "external_emojis": True, "use_external_stickers": True,
             "add_reactions": True, "connect": True, "speak": True, "stream": True,
             "use_voice_activation": True, "change_nickname": True,
             "use_application_commands": True, "create_public_threads": True,
             "send_messages_in_threads": True,
         }},
        {"name": "Beta Tester", "color": "0x1ABC9C", "hoist": True,  "mentionable": True,  "permissions": {}},
        {"name": "Member",     "color": "0x2ECC71", "hoist": True,  "mentionable": False,
         "permissions": {
             "view_channel": True, "send_messages": True, "embed_links": True,
             "attach_files": True, "add_reactions": True, "read_message_history": True,
             "external_emojis": True, "connect": True, "speak": True,
             "use_voice_activation": True, "stream": True, "change_nickname": True,
             "use_application_commands": True, "create_public_threads": True,
             "send_messages_in_threads": True,
         }},
        {"name": "Muted",      "color": "0x546E7A", "hoist": False, "mentionable": False,
         "permissions": {"view_channel": True, "read_message_history": True}},
        # Self-assignable (below Muted in hierarchy, given out via the role panel).
        {"name": "Announcements", "color": "0x3498DB", "hoist": False, "mentionable": True,  "permissions": {}},
        {"name": "Events",        "color": "0xE91E63", "hoist": False, "mentionable": True,  "permissions": {}},
        {"name": "He/Him",        "color": "0x607D8B", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "She/Her",       "color": "0x607D8B", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "They/Them",     "color": "0x607D8B", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "Gaming",        "color": "0x9B59B6", "hoist": False, "mentionable": True,  "permissions": {}},
        {"name": "Music",         "color": "0x1ABC9C", "hoist": False, "mentionable": True,  "permissions": {}},
        {"name": "Art",           "color": "0xF39C12", "hoist": False, "mentionable": True,  "permissions": {}},
        {"name": "German",        "color": "0xD35400", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "English",       "color": "0x2980B9", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "Japanese",      "color": "0xC0392B", "hoist": False, "mentionable": False, "permissions": {}},
        # Level roles, handed out automatically by the level system (see "levels" below).
        {"name": "Level 5",       "color": "0x95A5A6", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "Level 10",      "color": "0x1ABC9C", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "Level 20",      "color": "0x3498DB", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "Level 30",      "color": "0x9B59B6", "hoist": False, "mentionable": False, "permissions": {}},
        {"name": "Level 50",      "color": "0xF1C40F", "hoist": False, "mentionable": False, "permissions": {}},
    ],

    "categories": [
        {
            # Information is visible to everyone (read-only). Unverified members land here first.
            "name": "\U0001F4CB INFORMATION",
            "overwrites": {
                "@everyone": {"view_channel": True, "send_messages": False, "add_reactions": False, "read_message_history": True},
                "Moderator": {"send_messages": True, "manage_messages": True, "add_reactions": True},
                "Admin":     {"send_messages": True, "manage_messages": True, "add_reactions": True},
                "Owner":     {"send_messages": True, "manage_messages": True, "add_reactions": True},
            },
            "channels": [
                {"name": CHANNELS["welcome"],         "type": "text", "topic": "Welcome to the server!"},
                {"name": CHANNELS["rules"],           "type": "text", "topic": "Read and accept the rules to gain full access.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
                {"name": CHANNELS["announcements"],   "type": "text", "topic": "Important announcements from the staff."},
                {"name": CHANNELS["server_updates"],  "type": "text", "topic": "Server changes, new features, and updates."},
                {"name": CHANNELS["mod_releases"],    "type": "text", "topic": "Automatic release notifications from Modrinth.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": True},
                 }},
                {"name": CHANNELS["mod_downloads"],   "type": "text", "topic": "Pick a mod from the dropdown to get a download link to the latest version.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
                {"name": CHANNELS["roles"],           "type": "text", "topic": "Pick your roles. Click a button below.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                     "Member":    {"view_channel": True, "send_messages": False, "read_message_history": True},
                 }},
                {"name": CHANNELS["tickets"],         "type": "text", "topic": "Open a private support ticket - bug report, feature request, general help.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
                {"name": CHANNELS["crash_analyzer"],  "type": "text", "topic": "Game crashed? Click the button, upload your log and the bot tells you what went wrong.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
                {"name": CHANNELS["mod_stats"],       "type": "text", "topic": "Weekly download statistics for all mods.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": True},
                 }},
                {"name": CHANNELS["roadmap"],         "type": "text", "topic": "What's planned, in progress and done for each mod.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": True},
                 }},
                {"name": CHANNELS["compatibility"],   "type": "text", "topic": "Which mod supports which Minecraft version and loader. Updated automatically.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
                {"name": CHANNELS["staff_team"],      "type": "text", "topic": "Who is on the team. Updated automatically.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
                {"name": CHANNELS["beta_program"],    "type": "text", "topic": "Apply to become a beta tester while applications are open.",
                 "overwrites": {
                     "@everyone": {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": False},
                 }},
            ],
        },
        {
            # General requires Member role (granted by accepting the rules).
            "name": "\U0001F4AC GENERAL",
            "overwrites": {
                "@everyone": {"view_channel": False, "read_message_history": True},
                "Member":    {"view_channel": True, "send_messages": True, "read_message_history": True, "embed_links": True, "attach_files": True, "add_reactions": True},
                "VIP":       {"view_channel": True, "send_messages": True, "read_message_history": True, "embed_links": True, "attach_files": True, "add_reactions": True},
                "Moderator": {"view_channel": True, "send_messages": True, "manage_messages": True, "read_message_history": True},
                "Admin":     {"view_channel": True, "send_messages": True, "manage_messages": True, "read_message_history": True},
                "Owner":     {"view_channel": True, "send_messages": True, "manage_messages": True, "read_message_history": True},
                "Muted":     {"send_messages": False, "add_reactions": False, "create_public_threads": False},
            },
            "channels": [
                {"name": CHANNELS["general"],       "type": "text", "topic": "General chat for everyone."},
                {"name": CHANNELS["introductions"], "type": "text", "topic": "Introduce yourself to the community.", "slowmode": 60},
                {"name": CHANNELS["off_topic"],     "type": "text", "topic": "Off-topic chat - anything goes."},
                {"name": CHANNELS["media"],         "type": "text", "topic": "Share images, videos, and media.", "slowmode": 10},
                {"name": CHANNELS["memes"],         "type": "text", "topic": "Share your best memes.", "slowmode": 5},
                {"name": CHANNELS["bot_commands"],  "type": "text", "topic": "Use bot commands here.", "slowmode": 3},
            ],
        },
        {
            "name": "✨ COMMUNITY",
            "overwrites": {
                "@everyone": {"view_channel": False, "read_message_history": True},
                "Member":    {"view_channel": True, "send_messages": True, "read_message_history": True, "add_reactions": True},
                "VIP":       {"view_channel": True, "send_messages": True, "read_message_history": True, "add_reactions": True},
                "Moderator": {"view_channel": True, "send_messages": True, "read_message_history": True, "manage_messages": True},
                "Admin":     {"view_channel": True, "send_messages": True, "read_message_history": True, "manage_messages": True},
                "Owner":     {"view_channel": True, "send_messages": True, "read_message_history": True, "manage_messages": True},
                "Muted":     {"send_messages": False, "add_reactions": False, "create_public_threads": False},
            },
            "channels": [
                {"name": CHANNELS["suggestions"],  "type": "text",  "topic": "Suggest improvements for the server.", "slowmode": 30},
                {"name": CHANNELS["events"],       "type": "text",  "topic": "Upcoming community events."},
                {"name": CHANNELS["polls"],        "type": "text",  "topic": "Vote in community polls by reacting. Reaction votes are NOT anonymous - others can see who voted. Please pick only one option."},
                {"name": CHANNELS["level_ups"],    "type": "text",  "topic": "Level-up announcements. Check your rank with /rank, the top list with /leaderboard.",
                 "overwrites": {
                     "@everyone": {"view_channel": False},
                     "Member":    {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": True},
                     "VIP":       {"view_channel": True, "send_messages": False, "read_message_history": True, "add_reactions": True},
                     "Moderator": {"view_channel": True, "send_messages": False, "read_message_history": True, "manage_messages": True},
                     "Admin":     {"view_channel": True, "send_messages": True,  "read_message_history": True, "manage_messages": True},
                     "Owner":     {"view_channel": True, "send_messages": True,  "read_message_history": True, "manage_messages": True},
                 }},
                {"name": CHANNELS["art"],          "type": "text",  "topic": "Show off your creative work.", "slowmode": 15},
                {"name": CHANNELS["counting"],     "type": "text",  "topic": "Count up together, one number per message. The same person can't count twice in a row - a wrong number resets to 0!"},
                {"name": CHANNELS["beta_testing"], "type": "text",  "topic": "Beta builds, test instructions and feedback - beta testers only.",
                 "overwrites": {
                     "@everyone":   {"view_channel": False},
                     "Member":      {"view_channel": False},
                     "VIP":         {"view_channel": False},
                     "Beta Tester": {"view_channel": True, "send_messages": True, "read_message_history": True, "attach_files": True, "embed_links": True, "add_reactions": True},
                     "Moderator":   {"view_channel": True, "send_messages": True, "read_message_history": True, "manage_messages": True},
                     "Admin":       {"view_channel": True, "send_messages": True, "read_message_history": True, "manage_messages": True},
                     "Owner":       {"view_channel": True, "send_messages": True, "read_message_history": True, "manage_messages": True},
                     "Muted":       {"send_messages": False, "add_reactions": False},
                 }},
                {"name": CHANNELS["mod_support"],  "type": "forum", "topic": "Get help with the mods. Open a post per question."},
            ],
        },
        {
            "name": "\U0001F50A VOICE",
            "overwrites": {
                "@everyone": {"view_channel": False, "connect": False, "read_message_history": True},
                "Member":    {"view_channel": True, "connect": True, "speak": True, "use_voice_activation": True, "stream": True, "read_message_history": True},
                "VIP":       {"view_channel": True, "connect": True, "speak": True, "use_voice_activation": True, "stream": True, "priority_speaker": True},
                "Moderator": {"view_channel": True, "connect": True, "speak": True, "mute_members": True, "deafen_members": True, "move_members": True},
                "Admin":     {"view_channel": True, "connect": True, "speak": True, "mute_members": True, "deafen_members": True, "move_members": True},
                "Owner":     {"view_channel": True, "connect": True, "speak": True},
                "Muted":     {"speak": False, "stream": False, "send_messages": False},
            },
            "channels": [
                {"name": CHANNELS["create_vc"],  "type": "voice"},
                {"name": CHANNELS["general_vc"], "type": "voice"},
                {"name": CHANNELS["gaming_vc"],  "type": "voice", "user_limit": 6},
                {"name": CHANNELS["music_vc"],   "type": "voice", "user_limit": 10},
                {"name": CHANNELS["chill_vc"],   "type": "voice"},
                {"name": CHANNELS["study_vc"],   "type": "voice", "user_limit": 8},
                {"name": CHANNELS["afk_vc"],     "type": "voice"},
            ],
        },
        {
            "name": "\U0001F6E1 STAFF ONLY",
            "overwrites": {
                "@everyone": {"view_channel": False},
                "Moderator": {"view_channel": True, "send_messages": True, "read_message_history": True, "connect": True, "speak": True},
                "Admin":     {"view_channel": True, "send_messages": True, "read_message_history": True, "connect": True, "speak": True},
                "Owner":     {"view_channel": True, "send_messages": True, "read_message_history": True, "connect": True, "speak": True},
            },
            "channels": [
                {"name": CHANNELS["staff_chat"],     "type": "text", "topic": "Staff-only discussion."},
                {"name": CHANNELS["admin_only"], "type": "text", "topic": "Admin and Owner only discussion.",
                 "overwrites": {
                     "@everyone": {"view_channel": False},
                     "Moderator": {"view_channel": False},
                     "Admin":     {"view_channel": True, "send_messages": True, "read_message_history": True},
                     "Owner":     {"view_channel": True, "send_messages": True, "read_message_history": True},
                 }},
                {"name": CHANNELS["beta_applications"], "type": "text", "topic": "Beta tester applications. Only administrators can accept or reject."},
                {"name": CHANNELS["staff_vc"], "type": "voice"},
            ],
        },
        {
            # All log channels inherit these category permissions (staff read-only).
            "name": "\U0001F5D2 LOGS",
            "overwrites": {
                "@everyone": {"view_channel": False},
                "Moderator": {"view_channel": True, "send_messages": False, "read_message_history": True},
                "Admin":     {"view_channel": True, "send_messages": False, "read_message_history": True},
                "Owner":     {"view_channel": True, "send_messages": True,  "read_message_history": True},
            },
            "channels": [
                {"name": CHANNELS["mod_logs"],       "type": "text", "topic": "Moderation actions: bans, kicks, timeouts, warnings, purges, AutoMod alerts."},
                {"name": CHANNELS["message_logs"],   "type": "text", "topic": "Deleted and edited messages."},
                {"name": CHANNELS["member_logs"],    "type": "text", "topic": "Joins, leaves, role and nickname changes, bans and unbans."},
                {"name": CHANNELS["voice_logs"],     "type": "text", "topic": "Voice channel joins, leaves, and moves."},
                {"name": CHANNELS["server_logs"],    "type": "text", "topic": "Channel and role changes."},
                {"name": CHANNELS["ticket_archive"], "type": "text", "topic": "Transcripts of closed tickets."},
                {"name": CHANNELS["bot_updates"],    "type": "text", "topic": "Automatic log of bot and server updates."},
                {"name": CHANNELS["avatar_logs"],    "type": "text", "topic": "Profile picture changes (before / after). Administrators only.",
                 "overwrites": AVATAR_LOG_OVERWRITES},
            ],
        },
    ],

    "server_settings": {
        "description": "Community server for ErikEdits' Minecraft mods - support, release news, and community chat.",
        "verification_level": "medium",
        "default_notifications": "only_mentions",
        "explicit_content_filter": "all_members",
        "afk_channel": CHANNELS["afk_vc"],
        "afk_timeout": 300,
        "system_channel": CHANNELS["welcome"],
    },

    "welcome_screen": {
        "enabled": True,
        "description": "Welcome to our community! Get started below.",
        "channels": [
            {"channel": CHANNELS["rules"],         "description": "Read and accept the server rules", "emoji": "\U0001F4CB"},
            {"channel": CHANNELS["introductions"], "description": "Introduce yourself",               "emoji": "\U0001F44B"},
            {"channel": CHANNELS["roles"],         "description": "Pick your roles",                  "emoji": "\U0001F3AD"},
            {"channel": CHANNELS["general"],       "description": "Say hi in general chat",           "emoji": "\U0001F4AC"},
            {"channel": CHANNELS["announcements"], "description": "Stay in the loop",                 "emoji": "\U0001F4E2"},
        ],
    },

    "automod": {
        "enabled": True,
        "alert_channel": CHANNELS["mod_logs"],
        "rules": {
            "spam": True,
            "mention_spam_limit": 5,
            "keyword_presets": ["profanity", "slurs", "sexual_content"],
        },
    },

    "reaction_role_panel": {
        "channel": CHANNELS["roles"],
        "title": "Pick Your Roles",
        "description": (
            "Click a button to toggle a role on or off.\n\n"
            "**Notifications** - get pinged for these topics.\n"
            "**Pronouns** - let others know how to refer to you.\n"
            "**Interests** - find people with shared interests.\n"
            "**Languages** - the languages you speak."
        ),
        "buttons": [
            {"role": "Announcements", "label": "Announcements", "emoji": "\U0001F4E2", "style": "primary",   "row": 0},
            {"role": "Events",        "label": "Events",        "emoji": "\U0001F389", "style": "primary",   "row": 0},
            {"role": "He/Him",        "label": "He/Him",                                "style": "secondary", "row": 1},
            {"role": "She/Her",       "label": "She/Her",                               "style": "secondary", "row": 1},
            {"role": "They/Them",     "label": "They/Them",                             "style": "secondary", "row": 1},
            {"role": "Gaming",        "label": "Gaming",        "emoji": "\U0001F3AE", "style": "success",   "row": 2},
            {"role": "Music",         "label": "Music",         "emoji": "\U0001F3B5", "style": "success",   "row": 2},
            {"role": "Art",           "label": "Art",           "emoji": "\U0001F3A8", "style": "success",   "row": 2},
            {"role": "German",        "label": "German",        "emoji": "\U0001F1E9\U0001F1EA", "style": "secondary", "row": 3},
            {"role": "English",       "label": "English",       "emoji": "\U0001F1EC\U0001F1E7", "style": "secondary", "row": 3},
            {"role": "Japanese",      "label": "Japanese",      "emoji": "\U0001F1EF\U0001F1F5", "style": "secondary", "row": 3},
        ],
    },

    # Ticket system: multiple categories, each with its own panel button.
    "tickets": {
        "category_name": "TICKETS",
        "archive_channel": CHANNELS["ticket_archive"],
        "support_role_names": ["Moderator", "Admin", "Owner"],
        "auto_close_hours": 48,  # close tickets with no activity after N hours; set to 0 to disable
        "auto_close_check_minutes": 30,
        # Ticket types whose tickets get a staff-only "Forward bug" button. It sends a
        # summary + transcript to the webhook set with /settings bug-webhook.
        "forward_types": ["bug"],
        # After a ticket is closed the opener gets a DM asking for a 1-5 star rating.
        "rating_enabled": True,
        "types": [
            {
                "key": "bug",
                "label": "Bug Report",
                "emoji": "\U0001F41B",
                "style": "danger",
                "color": "0xE74C3C",
                # Asked in a form before the ticket opens (max. 5 fields).
                "form": [
                    {"label": "Mod and mod version", "placeholder": "e.g. SmiteMod 1.2.0", "max_length": 100},
                    {"label": "Minecraft version and loader", "placeholder": "e.g. 1.21.1 NeoForge", "max_length": 100},
                    {"label": "What happened?", "style": "long", "placeholder": "What did you expect, what happened instead?", "max_length": 1000},
                    {"label": "Steps to reproduce", "style": "long", "required": False, "max_length": 1000},
                    {"label": "Crash log link (mclo.gs etc.)", "required": False, "max_length": 300},
                ],
                "intro": (
                    "Hi {user}, thanks for reporting a bug.\n\n"
                    "**Please include:**\n"
                    "- Mod name and version\n"
                    "- Minecraft / loader version\n"
                    "- What you expected vs what happened\n"
                    "- Steps to reproduce (if possible)\n"
                    "- A crash log or video, if you have one"
                ),
            },
            {
                "key": "feature",
                "label": "Feature Request",
                "emoji": "\U0001F4A1",
                "style": "primary",
                "color": "0x3498DB",
                "intro": (
                    "Hi {user}, thanks for the suggestion!\n\n"
                    "Describe the feature you'd like and how it would improve the mod or server."
                ),
            },
            {
                "key": "help",
                "label": "General Help",
                "emoji": "\U0001F198",
                "style": "secondary",
                "color": "0x95A5A6",
                "intro": (
                    "Hi {user}, what do you need help with? A staff member will be with you shortly."
                ),
            },
        ],
    },

    # Discord native Onboarding flow. Requires Community Server feature.
    "onboarding": {
        "enabled": True,
        "mode": "default",  # "default" or "advanced"
        "default_channel_keys": ["welcome", "rules", "announcements", "general"],
        "prompts": [
            {
                "type": "multiple_choice",
                "title": "What are you interested in?",
                "single_select": False,
                "required": False,
                "options": [
                    {"title": "Gaming",        "description": "Talk games, find squads", "emoji": "\U0001F3AE", "role": "Gaming"},
                    {"title": "Music",         "description": "Share what you're listening to", "emoji": "\U0001F3B5", "role": "Music"},
                    {"title": "Art",           "description": "Show off your art", "emoji": "\U0001F3A8", "role": "Art"},
                ],
            },
            {
                "type": "multiple_choice",
                "title": "What pronouns should we use?",
                "single_select": True,
                "required": False,
                "options": [
                    {"title": "He/Him",    "emoji": "\U0001F468", "role": "He/Him"},
                    {"title": "She/Her",   "emoji": "\U0001F469", "role": "She/Her"},
                    {"title": "They/Them", "emoji": "\U0001F9D1", "role": "They/Them"},
                ],
            },
            {
                "type": "multiple_choice",
                "title": "Which notifications do you want?",
                "single_select": False,
                "required": False,
                "options": [
                    {"title": "Announcements", "description": "Important server news",  "emoji": "\U0001F4E2", "role": "Announcements"},
                    {"title": "Events",        "description": "Community events", "emoji": "\U0001F389", "role": "Events"},
                ],
            },
            {
                "type": "multiple_choice",
                "title": "Which languages do you speak?",
                "single_select": False,
                "required": False,
                "options": [
                    {"title": "German",   "emoji": "\U0001F1E9\U0001F1EA", "role": "German"},
                    {"title": "English",  "emoji": "\U0001F1EC\U0001F1E7", "role": "English"},
                    {"title": "Japanese", "emoji": "\U0001F1EF\U0001F1F5", "role": "Japanese"},
                ],
            },
        ],
    },

    # Predefined polls, selectable via /poll preset. Edit/extend freely.
    "poll_presets": [
        {
            "key": "menu_open",
            "label": "Q10 - How to open the JustQuests menu?",
            "question": "How do you want to open the JustQuests menu?",
            "options": ["Command (/quest)", "A keybind", "A quest book item", "A button in the inventory screen"],
            "duration_hours": 24,
        },
        {
            "key": "quest_book_item",
            "label": "Q11 - Should there be a quest book item?",
            "question": "Should there be a quest book item?",
            "options": ["Yes - auto-given book (undroppable)", "No - command/button is enough"],
            "duration_hours": 24,
        },
        {
            "key": "notifications",
            "label": "Q12 - How should progress be shown?",
            "question": "How should JustQuests notify you about quest progress and completion?",
            "options": ["Chat only", "Chat + toast pop-ups", "Chat + sounds", "Everything (chat+toasts+sounds)"],
            "duration_hours": 24,
        },
        {
            "key": "gui_look",
            "label": "Q37 - Which look should the GUI have?",
            "question": "Which look should the JustQuests GUI have?",
            "options": ["Book style (two pages)", "Modern single panel list"],
            "duration_hours": 24,
        },
        {
            "key": "version_notice",
            "label": "Q36 - Notify OPs about new versions?",
            "question": "Should JustQuests tell OPs at login when a newer version is out?",
            "options": ["Yes - notify on login (toggleable)", "No - no version check"],
            "duration_hours": 24,
        },
    ],

    # Suggestions: every message in the suggestions channel is reposted by the bot
    # as an embed with up/down votes, a discussion thread and staff buttons
    # (Accept / Reject / In progress - usable only by members with Administrator).
    "suggestions": {
        "enabled": True,
        "create_threads": True,
        "thread_archive_minutes": 1440,  # 24h
        "dm_author_on_status": True,
    },

    # Crash-log analyzer: panel in CHANNELS["crash_analyzer"] opens a private channel
    # where the user uploads latest.log / a crash report. Files are only read in
    # memory, never stored.
    "crash_analyzer": {
        "enabled": True,
        "category_name": "CRASH REPORTS",
        "max_file_mb": 8,
        "auto_close_hours": 6,        # delete idle analyzer channels after N hours
    },

    # Download statistics posted to CHANNELS["mod_stats"].
    "mod_stats": {
        "enabled": True,
        "interval": "weekly",         # "daily" or "weekly"
        "weekday": 0,                 # 0 = Monday (only for weekly)
        "hour": 10,                   # local hour in "timezone"
        "timezone": "Europe/Berlin",
    },

    # Link filter: removes invites to other Discord servers and scam links.
    # Staff (Manage Messages) are exempt. Pause with /maintenance linkfilter.
    "link_filter": {
        "enabled": True,
        "block_invites": True,
        "block_scam_links": True,
        # Never touched by the scam check (subdomains included).
        "allowed_domains": [
            "modrinth.com", "cdn.modrinth.com", "github.com", "githubusercontent.com",
            "curseforge.com", "forgecdn.net", "minecraft.net", "mojang.com",
            "fabricmc.net", "neoforged.net", "minecraftforge.net", "quiltmc.org",
            "youtube.com", "youtu.be", "twitch.tv", "imgur.com", "mclo.gs",
            "pastebin.com", "discord.com", "discord.gg", "discordapp.com",
            "discordapp.net", "tenor.com", "giphy.com",
        ],
        # Always removed, in addition to the built-in lookalike detection.
        "blocked_domains": [],
        "warn_message": "{user} that link isn't allowed here and was removed.",
    },

    # Level system. Level-ups are announced in CHANNELS["level_ups"].
    "levels": {
        "enabled": True,
        # Voice XP: per minute in a voice channel with at least one other person
        # (not muted/deafened by yourself, not in the AFK channel).
        "voice_xp_per_minute": 4,
        "xp_min": 15,
        "xp_max": 25,
        "cooldown_seconds": 60,       # XP at most once per minute per member
        "min_message_length": 3,
        "no_xp_channels": [CHANNELS["bot_commands"], CHANNELS["level_ups"], CHANNELS["counting"]],
        # level -> role name (roles are created by /setup). Only the highest earned
        # level role is kept unless stack_roles is True.
        "level_roles": {5: "Level 5", 10: "Level 10", 20: "Level 20", 30: "Level 30", 50: "Level 50"},
        "stack_roles": False,
    },

    # Voice channel at the top of INFORMATION that shows the member count.
    # Discord allows only 2 renames per 10 minutes, so it updates every 10 min.
    "member_counter": {
        "enabled": True,
        "name_format": "\U0001F465 Members: {count}",
        "category": "\U0001F4CB INFORMATION",
        "count_bots": True,
    },

    # Reminders (/remind), delivered by DM.
    "reminders": {
        "max_per_user": 25,
        "max_days": 365,
    },

    # Download panel: "Filter by Minecraft version" menu -> loader -> matching files.
    "download_filter": {
        "enabled": True,
        "max_versions": 25,           # Discord allows 25 entries in a menu
        "include_snapshots": False,
    },

    # Beta tester program. Admins open applications for a time window with
    # /beta open; the panel in CHANNELS["beta_program"] then shows an Apply button.
    "beta": {
        "enabled": True,
        "role": "Beta Tester",
        "applications_channel": CHANNELS["beta_applications"],
        "testing_channel": CHANNELS["beta_testing"],
    },

    # Scheduled announcements (/schedule and the web panel).
    "scheduled_announcements": {
        "timezone": "Europe/Berlin",
    },

    # Roadmap embeds in CHANNELS["roadmap"], one per mod. Also accepts commands sent
    # through the channel webhook (see /roadmap webhook), e.g. from scripts.
    "roadmap": {
        "enabled": True,
        "max_done_shown": 10,
    },

    # Join-to-create voice: joining CHANNELS["create_vc"] creates your own channel.
    "temp_voice": {
        "enabled": True,
        "name_format": "\U0001F50A {user}'s channel",
        "default_limit": 0,           # 0 = unlimited
    },

    # Minecraft server logs (EntityLagFix plugin webhook). Channel: /mclog channel.
    # Lines look like "BLOCK_PLACE | ColinTK | GRASS_BLOCK @ Location{world=...,x=..,y=..,z=..}".
    "mc_logs": {
        "retention_days": 4,
        "max_db_mb": 100,               # hard size limit of data/mc_logs.db, oldest data goes first
        "timezone": "Europe/Berlin",
        # Never reported as suspicious. Teleports close to them are reported.
        "trusted_players": ["ErikEdits", "ColinTK"],
        # Get suspicious-activity DMs (Discord usernames), plus the server owner.
        "alert_users": ["mini_paluten056"],
        "alert_cooldown_seconds": 120,  # same player + same kind at most once in this time
        "alerts_from_spectators_only": False,
        "teleport_alert_radius": 64,    # blocks around a trusted player
        "position_max_age_minutes": 10, # how old a trusted player's last known position may be
        "mass_break_blocks": 300,       # blocks broken within mass_break_minutes
        "mass_break_minutes": 5,
        "ore_alert_count": 20,          # ores broken within ore_alert_minutes
        "ore_alert_minutes": 10,
        "ores": ["DIAMOND_ORE", "DEEPSLATE_DIAMOND_ORE", "ANCIENT_DEBRIS", "EMERALD_ORE",
                 "DEEPSLATE_EMERALD_ORE", "GOLD_ORE", "DEEPSLATE_GOLD_ORE", "NETHER_GOLD_ORE"],
        # Commands (first word) that are reported when someone untrusted runs them.
        "suspicious_commands": ["op", "deop", "gamemode", "gm", "gmc", "gms", "gmsp", "gma", "give", "i", "item",
                                "tp", "tphere", "tpo", "tpall", "teleport", "vanish", "v", "sv", "pv", "fly", "god",
                                "kill", "ban", "ban-ip", "pardon", "kick", "whitelist", "lp", "luckperms", "pex",
                                "perm", "perms", "sudo", "execute", "summon", "effect", "enchant", "xp",
                                "experience", "setblock", "fill", "clone", "/set", "/wand", "stop", "reload",
                                "rl", "plugman", "invsee", "openinv", "ec", "enderchest", "speed", "heal", "feed",
                                "nick", "spectate"],
        # Event types that are only counted (per 10 minutes and area), not stored one by one.
        "count_only_types": ["ENTITY_SPAWN", "ITEM_SPAWN", "CREATURE_SPAWN", "SPAWNER_SPAWN", "CHUNK_LOAD",
                             "CHUNK_UNLOAD", "ENTITY_DEATH", "ITEM_DESPAWN"],
        # At most one stored event per player and this many seconds (movement spam).
        "sample_seconds": {"PLAYER_MOVE": 30, "PLAYER_ROTATE": 60, "PLAYER_TOGGLE_SNEAK": 30,
                           "PLAYER_TOGGLE_SPRINT": 30, "PLAYER_ANIMATION": 30, "PLAYER_VELOCITY": 30},
    },

    # Profile picture changes with a before/after image in CHANNELS["avatar_logs"].
    "avatar_log": {
        "enabled": True,
        "server_avatars": True,       # also log server-specific profile pictures
    },

    # Welcome image with the member's avatar, posted in CHANNELS["welcome"].
    "welcome_image": {
        "enabled": True,
        "accent_color": "0x5865F2",
    },

    # Statistics for the web panel (members, messages, tickets). Kept for N days.
    "stats": {
        "enabled": True,
        "keep_days": 180,
    },

    # /mod, /changelog, compatibility table, download milestones, release feedback polls.
    "mod_info": {
        "compatibility_refresh_hours": 6,
        "milestones": [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000],
        "milestone_channel": CHANNELS["mod_stats"],
        # N days after a new release, a feedback poll is posted in the polls channel.
        "feedback_after_days": 3,
        "feedback_poll_hours": 72,
        "feedback_options": ["Works great", "Small issues", "Crashes / broken", "Haven't tried it yet"],
    },

    # Automatic FAQ suggestions: when someone asks a question that matches a FAQ's
    # name or keywords (/faq add ... keywords:...), the bot replies with the answer.
    "faq_suggestions": {
        "enabled": True,
        "channels": [CHANNELS["general"], CHANNELS["mod_support"], CHANNELS["bot_commands"], CHANNELS["off_topic"]],
        "cooldown_minutes": 10,       # same FAQ at most once per channel in this time
    },

    # Counting channel.
    "counting": {
        "enabled": True,
    },

    # "Did you know?" tip once a day. Manage tips with /tip add / remove / list.
    "tips": {
        "enabled": True,
        "channel": CHANNELS["general"],
        "hour": 17,
        "timezone": "Europe/Berlin",
        "defaults": [
            "Your game crashed? Upload your `latest.log` in the crash analyzer channel - the bot tells you what's wrong.",
            "Use the Minecraft-version menu in the mod downloads channel to get the right file for your setup.",
            "Always check that Fabric API is installed when a Fabric mod doesn't load.",
            "Allocating more than half of your PC's RAM to Minecraft usually makes it slower, not faster.",
            "You can pick notification roles in the roles channel to get pinged for new releases.",
            "Found a bug? Open a Bug Report ticket - the form makes sure we get all the details.",
            "Check the roadmap channel to see what's coming next for each mod.",
        ],
    },

    # Automatic slowmode: the busier a channel gets, the longer the slowmode.
    # Rules are checked from the top; "messages" = messages in the last 60 seconds.
    "auto_slowmode": {
        "enabled": True,
        "channels": [CHANNELS["general"], CHANNELS["off_topic"], CHANNELS["memes"], CHANNELS["media"],
                     CHANNELS["introductions"], CHANNELS["art"], CHANNELS["suggestions"]],
        "levels": [
            {"messages": 80, "slowmode": 30},
            {"messages": 50, "slowmode": 15},
            {"messages": 35, "slowmode": 10},
            {"messages": 20, "slowmode": 5},
        ],
        "calm_minutes": 3,            # back to normal after this long below the lowest level
    },

    # Staff list in CHANNELS["staff_team"] (highest role first).
    "staff_list": {
        "enabled": True,
        "roles": ["Owner", "Admin", "Moderator"],
    },

    # Invite tracking. The leaderboard is only visible to administrators.
    "invites": {
        "enabled": True,
    },

    # Error alerts: errors in the bot are sent by DM to every member with the
    # Administrator permission (turn off for yourself with /error-alerts off).
    "error_alerts": {
        "enabled": True,
        "min_minutes_between_same_error": 30,
    },

    # Automatic backups of the bot data (warnings, levels, FAQs, ...) and the server
    # structure, sent to the webhook set with /settings backup-webhook.
    # Nothing is kept on the bot host.
    "backups": {
        "enabled": True,
        "hour": 4,
        "timezone": "Europe/Berlin",
    },

    # Consecutive-duplicate message protection (needs Message Content intent).
    "antispam": {
        "enabled": True,
        "duplicate_threshold": 3,       # identical messages in a row before action
        "reset_seconds": 60,            # counter resets after this much silence
        "warn_cooldown_seconds": 15,    # don't re-warn the same user faster than this
        "warn_message": "{user} please don't send the same message repeatedly - your duplicates were removed.",
    },

    # Modrinth release-watcher. Polls the user's projects and posts new versions.
    "modrinth": {
        "enabled": True,
        "username": "ErikEdits",
        "channel": CHANNELS["mod_releases"],
        "poll_minutes": 15,
        "mention_role": "Announcements",
    },

    # Welcome DM sent to each new member on join. Channel mention placeholders
    # ({rules}, {tickets}, {roles}, {general}, {introductions}) are auto-resolved.
    "welcome_dm": {
        "enabled": True,
        "message": (
            "Hey {user}, welcome to **{guild}**!\n\n"
            "Quick tour:\n"
            "- Read and accept the rules in {rules}\n"
            "- Pick your roles in {roles}\n"
            "- Open a support ticket in {tickets} if you need help\n"
            "- Say hi in {general}\n"
        ),
    },

    # Rules-acceptance button. Clicking it grants the role named below.
    "rules_acceptance": {
        "enabled": True,
        "channel": CHANNELS["rules"],
        "role_granted": "Member",
        "button_label": "I Accept the Rules",
        "button_emoji": "✅",
    },

    "welcome_message": (
        "Hey there, welcome to our community!\n\n"
        "**Get started:**\n"
        f"- Read and accept the rules in <#{CHANNELS['rules']}>\n"
        f"- Introduce yourself in <#{CHANNELS['introductions']}>\n"
        f"- Grab your roles in <#{CHANNELS['roles']}>\n"
        f"- Need help? Open a ticket in <#{CHANNELS['tickets']}>\n"
        f"- Say hi in <#{CHANNELS['general']}>\n\n"
        "Have fun and be kind to each other!"
    ),

    "rules_message": (
        "**1. Be respectful**\n"
        "Treat everyone with respect. No harassment, hate speech, or personal attacks.\n\n"
        "**2. No spam**\n"
        "Do not spam messages, mentions, emojis, or links.\n\n"
        "**3. Keep it SFW**\n"
        "No NSFW, gore, or other disturbing content anywhere on the server.\n\n"
        "**4. Use the right channels**\n"
        "Post on-topic in the channel that fits your message.\n\n"
        "**5. No advertising**\n"
        "Do not promote other servers, services, or products without staff permission.\n\n"
        "**6. Listen to staff**\n"
        "Follow staff instructions. Argue moderation in DMs, not in public.\n\n"
        "**7. Follow Discord ToS**\n"
        "Stick to the Discord Terms of Service and Community Guidelines.\n\n"
        "Click the button below to confirm you've read and accept these rules."
    ),
}
