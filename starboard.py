"""
starboard.py — Dyno-style Starboard module for Embot

Configure everything in the CONFIG block below, then restart/reload the module.
No slash commands — all settings live here in the file.
"""

import discord
from discord.ext import commands
from pathlib import Path
import json

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
_db_path = _data_dir / "starboard_entries.json"


def _load_entries() -> dict:
    """Load the message-id → starboard-id mapping from disk."""
    if _db_path.exists():
        try:
            with open(_db_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_entries(entries: dict) -> None:
    with open(_db_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _count_reactions(message: discord.Message, emoji: str) -> int:
    for reaction in message.reactions:
        if str(reaction.emoji) == emoji:
            return reaction.count
    return 0


def _star_label(count: int) -> str:
    """Progressive star emoji based on reaction count (mirrors Dyno behaviour)."""
    if count >= 15:
        return "🌟"
    elif count >= 10:
        return "💫"
    elif count >= 5:
        return "⭐"
    else:
        return "✨"


def _build_embed(message: discord.Message) -> discord.Embed:
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


def _build_content(count: int, source_channel: discord.TextChannel) -> str:
    return f"{_star_label(count)} **{count}** | {source_channel.mention}"


async def _handle_reaction(bot: commands.Bot, payload: discord.RawReactionActionEvent):
    """Called on every reaction add or remove."""
    if payload.guild_id is None:
        return

    # Validate config
    if not CONFIG["channel_id"]:
        return
    if str(payload.emoji) != CONFIG["emoji"]:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    starboard_channel = guild.get_channel(CONFIG["channel_id"])
    if starboard_channel is None:
        bot.logger.log(MODULE_NAME, f"Starboard channel {CONFIG['channel_id']} not found in guild {guild}", "WARNING")
        return

    # Ignore reactions that happen inside the starboard channel itself
    if payload.channel_id == CONFIG["channel_id"]:
        return

    source_channel = guild.get_channel(payload.channel_id)
    if source_channel is None:
        return

    try:
        message = await source_channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return

    # Self-star guard
    if not CONFIG["self_star"] and payload.user_id == message.author.id:
        return

    count = _count_reactions(message, CONFIG["emoji"])
    entries = _load_entries()
    msg_key = str(message.id)
    existing_id = entries.get(msg_key)

    # ── Below threshold: remove if already posted ──
    if count < CONFIG["threshold"]:
        if existing_id:
            try:
                sb_msg = await starboard_channel.fetch_message(int(existing_id))
                await sb_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            del entries[msg_key]
            _save_entries(entries)
        return

    # ── At or above threshold ──
    content = _build_content(count, source_channel)
    embed = _build_embed(message)

    if existing_id:
        try:
            sb_msg = await starboard_channel.fetch_message(int(existing_id))
            await sb_msg.edit(content=content, embed=embed)
        except discord.NotFound:
            # Was manually deleted — recreate
            sb_msg = await starboard_channel.send(content=content, embed=embed)
            entries[msg_key] = str(sb_msg.id)
            _save_entries(entries)
        except discord.Forbidden:
            bot.logger.log(MODULE_NAME, "Missing permissions to edit starboard message", "WARNING")
    else:
        try:
            sb_msg = await starboard_channel.send(content=content, embed=embed)
            entries[msg_key] = str(sb_msg.id)
            _save_entries(entries)
        except discord.Forbidden:
            bot.logger.log(MODULE_NAME, "Missing permissions to post to starboard channel", "WARNING")


def setup(bot: commands.Bot):
    # Validate config at load time
    if not CONFIG["channel_id"]:
        bot.logger.log(MODULE_NAME, "⚠️  channel_id is not set in CONFIG — starboard will not post until configured", "WARNING")
    else:
        bot.logger.log(MODULE_NAME, f"Starboard → channel {CONFIG['channel_id']} | threshold {CONFIG['threshold']} | emoji {CONFIG['emoji']}")

    @bot.listen("on_raw_reaction_add")
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        await _handle_reaction(bot, payload)

    @bot.listen("on_raw_reaction_remove")
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        await _handle_reaction(bot, payload)

    @bot.listen("on_raw_message_delete")
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        """Clean up the starboard post when the source message is deleted."""
        if payload.guild_id is None or not CONFIG["channel_id"]:
            return

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return

        entries = _load_entries()
        msg_key = str(payload.message_id)
        if msg_key not in entries:
            return

        starboard_channel = guild.get_channel(CONFIG["channel_id"])
        if starboard_channel:
            try:
                sb_msg = await starboard_channel.fetch_message(int(entries[msg_key]))
                await sb_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        del entries[msg_key]
        _save_entries(entries)

    bot.logger.log(MODULE_NAME, "Starboard module loaded.")
