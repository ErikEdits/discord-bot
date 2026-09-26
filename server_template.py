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
    "general":         "\U0001F4AC-general",
    "introductions":   "\U0001F64B-introductions",
    "off_topic":       "\U0001F3B2-off-topic",
    "media":           "\U0001F5BC-media",
    "memes":           "\U0001F602-memes",
    "bot_commands":    "\U0001F916-bot-commands",
    "suggestions":     "\U0001F4A1-suggestions",
    "events":          "\U0001F389-events",
    "polls":           "\U0001F4CA-polls",
    "art":             "\U0001F3A8-art-and-creations",
    "mod_support":     "\U0001F6E0-mod-support",
    "staff_chat":      "\U0001F6E1-staff-chat",
    "mod_logs":        "\U0001F4DC-mod-logs",
    "message_logs":    "\U0001F4AC-message-logs",
    "member_logs":     "\U0001F465-member-logs",
    "voice_logs":      "\U0001F50A-voice-logs",
    "server_logs":     "\U0001F5C4-server-logs",
    "bot_updates":     "\U0001F4E3-bot-updates",
    "ticket_archive":  "\U0001F4C1-ticket-archive",
    "admin_only":      "\U0001F512-admin-only",
    "staff_vc":        "\U0001F6E1 Staff VC",
    "general_vc":      "\U0001F50A General VC",
    "gaming_vc":       "\U0001F3AE Gaming",
    "music_vc":        "\U0001F3B5 Music",
    "chill_vc":        "\U0001F319 Chill",
    "study_vc":        "\U0001F4DA Study Hall",
    "afk_vc":          "\U0001F4A4 AFK",
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
                {"name": CHANNELS["art"],          "type": "text",  "topic": "Show off your creative work.", "slowmode": 15},
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
        "types": [
            {
                "key": "bug",
                "label": "Bug Report",
                "emoji": "\U0001F41B",
                "style": "danger",
                "color": "0xE74C3C",
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

    # Auto up/down voting + discussion threads in the suggestions channel.
    "suggestions": {
        "enabled": True,
        "create_threads": True,
        "thread_archive_minutes": 1440,  # 24h
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
