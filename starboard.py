"""
starboard.py — Dyno-style Starboard module for Embot

Configure everything in the CONFIG block below, then restart/reload the module.
No slash commands — all settings live here in the file.
"""

import discord
from discord.ext import commands
from pathlib import Path
from datetime import datetime, timezone
import json
import asyncio

MODULE_NAME = "STARBOARD"

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                         CONFIGURATION                                   ║
# ╠══════════════════════════════════════════════════════════════════════════╣
# ║  Fill in your settings here. Restart or `reload starboard` to apply.   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

CONFIG = {
    # ID of the channel where starred messages will be posted
    # Example: 1234567890123456789
    "channel_id": 1357896154276429984,

    # Number of ⭐ reactions required to post a message to the starboard
    "threshold": 3,

    # The emoji to watch for (standard unicode or custom emoji string)
    "emoji": "⭐",

    # Allow message authors to star their own messages?
    "self_star": False,
}

# ══════════════════════════════════════════════════════════════════════════════
#  Nothing below this line needs to be changed
# ══════════════════════════════════════════════════════════════════════════════

_data_dir = Path(__file__).parent / "data"
_data_dir.mkdir(exist_ok=True)
_db_path = _data_dir / "starboard_cache.json"

# ── Persistent cache schema ───────────────────────────────────────────────
#
# {
#   "entries": {
#     "<source_msg_id>": {
#       "starboard_msg_id": str,       # ID of the posted starboard message
#       "channel_id":       str,       # source channel ID
#       "author_id":        str,       # source message author ID
#       "author_name":      str,       # display name at time of first star
#       "peak_stars":       int,       # highest star count ever reached
#       "current_stars":    int,       # last known star count
#       "first_starred_at": str,       # ISO timestamp when first posted
#       "last_updated_at":  str,       # ISO timestamp of last edit
#       "content_preview":  str,       # first 100 chars of message content
#     }
#   }
# }

_cache: dict = {"entries": {}}

# Per-message-id asyncio locks so unrelated messages never block each other.
# A single global lock would cause every reaction to queue behind every other.
_msg_locks: dict[str, asyncio.Lock] = {}


def _get_msg_lock(msg_id: str) -> asyncio.Lock:
    if msg_id not in _msg_locks:
        _msg_locks[msg_id] = asyncio.Lock()
    return _msg_locks[msg_id]


# ── Disk I/O ──────────────────────────────────────────────────────────────

def _load_cache() -> None:
    global _cache
    if _db_path.exists():
        try:
            with open(_db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Migrate old flat format (just msg_id → sb_msg_id strings)
                if data and isinstance(next(iter(data.values()), None), str):
                    _cache = {"entries": {k: {"starboard_msg_id": v} for k, v in data.items()}}
                else:
                    _cache = data
                if "entries" not in _cache:
                    _cache = {"entries": _cache}
                return
        except Exception:
            pass
    _cache = {"entries": {}}


def _save_cache() -> None:
    with open(_db_path, "w", encoding="utf-8") as f:
        json.dump(_cache, f, indent=2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Embed / content builders ──────────────────────────────────────────────

def _star_label(count: int) -> str:
    if count >= 15:
        return "🌟"
    elif count >= 10:
        return "💫"
    elif count >= 5:
        return "⭐"
    else:
        return "✨"


def _build_content(count: int, source_channel: discord.TextChannel) -> str:
    return f"{_star_label(count)} **{count}** | {source_channel.mention}"


def _build_embed(message: discord.Message, count: int) -> discord.Embed:
    embed = discord.Embed(
        description=message.content or "",
        color=discord.Color.gold(),
        timestamp=message.created_at,
    )
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)

    # Attach first image attachment
    if message.attachments:
        first = message.attachments[0]
        if first.content_type and first.content_type.startswith("image/"):
            embed.set_image(url=first.url)

    # Fall back to embed image/thumbnail (e.g. Tenor GIFs, link previews)
    if not embed.image and message.embeds:
        for e in message.embeds:
            if e.image:
                embed.set_image(url=e.image.url)
                break
            if e.thumbnail:
                embed.set_image(url=e.thumbnail.url)
                break

    embed.set_footer(text=f"#{message.channel.name}")
    return embed


def _count_reactions(message: discord.Message, emoji: str) -> int:
    for reaction in message.reactions:
        if str(reaction.emoji) == emoji:
            return reaction.count
    return 0


# ── Core handler ──────────────────────────────────────────────────────────

async def _handle_reaction(bot: commands.Bot, payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    if not CONFIG["channel_id"]:
        return
    if str(payload.emoji) != CONFIG["emoji"]:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    starboard_channel = guild.get_channel(CONFIG["channel_id"])
    if starboard_channel is None:
        bot.logger.log(MODULE_NAME, f"Starboard channel {CONFIG['channel_id']} not found", "WARNING")
        return

    # Never process reactions inside the starboard channel itself
    if payload.channel_id == CONFIG["channel_id"]:
        return

    source_channel = guild.get_channel(payload.channel_id)
    if source_channel is None:
        return

    msg_key = str(payload.message_id)

    # Per-message lock — serialises concurrent reactions for the SAME message
    # without blocking reactions on completely different messages.
    async with _get_msg_lock(msg_key):

        # Always fetch fresh so reaction count is accurate at this exact moment.
        try:
            message = await source_channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        # Self-star guard
        if not CONFIG["self_star"] and payload.user_id == message.author.id:
            return

        count = _count_reactions(message, CONFIG["emoji"])
        entry = _cache["entries"].get(msg_key)

        # ── Below threshold ──────────────────────────────────────────────
        if count < CONFIG["threshold"]:
            if entry:
                try:
                    sb_msg = await starboard_channel.fetch_message(int(entry["starboard_msg_id"]))
                    await sb_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                del _cache["entries"][msg_key]
                _save_cache()
            return

        # ── At or above threshold ────────────────────────────────────────
        content = _build_content(count, source_channel)
        embed = _build_embed(message, count)

        if entry:
            # ── Update existing starboard post ───────────────────────────
            # Only edit if the count actually changed to avoid redundant API calls.
            if count == entry.get("current_stars") and entry.get("starboard_msg_id"):
                return

            try:
                sb_msg = await starboard_channel.fetch_message(int(entry["starboard_msg_id"]))
                await sb_msg.edit(content=content, embed=embed)
            except discord.NotFound:
                # Was manually deleted — recreate it, update cache
                sb_msg = await starboard_channel.send(content=content, embed=embed)
                entry["starboard_msg_id"] = str(sb_msg.id)
            except discord.Forbidden:
                bot.logger.log(MODULE_NAME, "Missing permissions to edit starboard message", "WARNING")
                return

            entry["current_stars"] = count
            entry["peak_stars"] = max(entry.get("peak_stars", count), count)
            entry["last_updated_at"] = _now_iso()
            _save_cache()

        else:
            # ── First time hitting threshold — post to starboard ─────────
            try:
                sb_msg = await starboard_channel.send(content=content, embed=embed)
            except discord.Forbidden:
                bot.logger.log(MODULE_NAME, "Missing permissions to post to starboard channel", "WARNING")
                return

            _cache["entries"][msg_key] = {
                "starboard_msg_id": str(sb_msg.id),
                "channel_id":       str(source_channel.id),
                "author_id":        str(message.author.id),
                "author_name":      message.author.display_name,
                "peak_stars":       count,
                "current_stars":    count,
                "first_starred_at": _now_iso(),
                "last_updated_at":  _now_iso(),
                "content_preview":  (message.content or "")[:100],
            }
            _save_cache()
            bot.logger.log(MODULE_NAME, f"Posted to starboard: msg {msg_key} by {message.author} ({count} stars)")


# ── Setup ─────────────────────────────────────────────────────────────────

def setup(bot: commands.Bot):
    _load_cache()

    if not CONFIG["channel_id"]:
        bot.logger.log(MODULE_NAME, "⚠️  channel_id is not set in CONFIG — starboard will not post until configured", "WARNING")
    else:
        bot.logger.log(
            MODULE_NAME,
            f"Starboard → channel {CONFIG['channel_id']} | "
            f"threshold {CONFIG['threshold']} | emoji {CONFIG['emoji']} | "
            f"entries loaded: {len(_cache['entries'])}"
        )

    @bot.listen("on_raw_reaction_add")
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        await _handle_reaction(bot, payload)

    @bot.listen("on_raw_reaction_remove")
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        await _handle_reaction(bot, payload)

    @bot.listen("on_raw_message_delete")
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        """Remove the starboard post when the source message is deleted."""
        if payload.guild_id is None or not CONFIG["channel_id"]:
            return

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return

        msg_key = str(payload.message_id)

        async with _get_msg_lock(msg_key):
            entry = _cache["entries"].get(msg_key)
            if not entry:
                return

            starboard_channel = guild.get_channel(CONFIG["channel_id"])
            if starboard_channel:
                try:
                    sb_msg = await starboard_channel.fetch_message(int(entry["starboard_msg_id"]))
                    await sb_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            del _cache["entries"][msg_key]
            _save_cache()

    bot.logger.log(MODULE_NAME, "Starboard module loaded.")