"""Anti-spam: deletes consecutive duplicate messages and warns the spammer.

Behavior for a member sending the same text repeatedly in one channel:
- 1st + 2nd identical message: tolerated (kept)
- 3rd identical in a row (within the reset window): the 2nd and 3rd are
  deleted (the first stays), the member gets a short auto-deleting warning,
  and an embed is posted to mod-logs
- further duplicates are deleted silently (the warning has a cooldown)

Requires the **Message Content** privileged intent: enable it in the
Developer Portal (Bot -> Privileged Gateway Intents) AND set
MESSAGE_CONTENT_INTENT=true in .env.

Staff (manage_messages) and bots are exempt. Configured via
SERVER_TEMPLATE["antispam"]. Pause with /maintenance antispam state:on.
"""

import logging
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from server_template import SERVER_TEMPLATE

log = logging.getLogger("setup-bot.antispam")


def _config() -> dict:
    return SERVER_TEMPLATE.get("antispam", {})


class AntiSpam(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # (channel_id, author_id) -> {content, count, ts, pending, warned}
        self.tracker: dict[tuple[int, int], dict] = {}
        if not bot.intents.message_content:
            log.warning(
                "Message Content intent is OFF - anti-spam cannot read messages. "
                "Enable it in the Developer Portal and set MESSAGE_CONTENT_INTENT=true."
            )

    def _prune(self, now: float, reset_s: float) -> None:
        if len(self.tracker) <= 1000:
            return
        stale = [k for k, v in self.tracker.items() if now - v["ts"] > reset_s]
        for k in stale:
            self.tracker.pop(k, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        cfg = _config()
        if not cfg.get("enabled", False):
            return
        if message.guild is None or message.author.bot:
            return
        author = message.author
        if not isinstance(author, discord.Member):
            return
        if author.guild_permissions.manage_messages:
            return
        from cogs.maintenance import is_under_maintenance
        if is_under_maintenance("antispam"):
            return

        key = (message.channel.id, author.id)
        content = (message.content or "").strip().lower()
        if not content:
            # Attachment-only or empty (no Message Content intent) - nothing to compare.
            self.tracker.pop(key, None)
            return

        now = time.monotonic()
        reset_s = float(cfg.get("reset_seconds", 60))
        self._prune(now, reset_s)

        entry = self.tracker.get(key)
        if entry is None or entry["content"] != content or now - entry["ts"] > reset_s:
            self.tracker[key] = {
                "content": content,
                "count": 1,
                "ts": now,
                "pending": [],
                "warned": 0.0,
            }
            return

        entry["count"] += 1
        entry["ts"] = now
        threshold = int(cfg.get("duplicate_threshold", 3))

        if entry["count"] < threshold:
            entry["pending"].append(message)
            return

        # Threshold reached: delete this duplicate plus stored earlier ones (keep the first).
        to_delete = entry["pending"] + [message]
        entry["pending"] = []
        deleted = 0
        for msg in to_delete:
            try:
                await msg.delete()
                deleted += 1
            except (discord.Forbidden, discord.NotFound):
                pass
            except Exception:
                log.exception("Failed to delete spam message")
        if not deleted:
            return
        log.info("Anti-spam: removed %d duplicate(s) from %s in #%s", deleted, author, message.channel)

        warn_cooldown = float(cfg.get("warn_cooldown_seconds", 15))
        if now - entry["warned"] > warn_cooldown:
            entry["warned"] = now
            warn_text = cfg.get(
                "warn_message",
                "{user} please don't send the same message repeatedly - your duplicates were removed.",
            ).format(user=author.mention)
            try:
                await message.channel.send(warn_text, delete_after=10)
            except discord.Forbidden:
                pass

            try:
                from cogs.logging_cog import _get_log_channel, _safe_send
                ch = _get_log_channel(message.guild, "mod")
                if ch:
                    embed = discord.Embed(
                        title="Spam Removed",
                        color=0xE67E22,
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.add_field(name="Member", value=f"{author.mention} ({author})", inline=False)
                    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                    embed.add_field(name="Duplicates removed", value=str(deleted), inline=True)
                    embed.add_field(name="Message", value=content[:300], inline=False)
                    await _safe_send(ch, embed=embed)
            except Exception:
                log.exception("Failed to log spam removal")


async def setup(bot):
    await bot.add_cog(AntiSpam(bot))
