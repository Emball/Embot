# submissions.py
import re
import discord
import logging
from config import (
    REACTION_EMOJIS, 
    PROJECT_DISCUSSION_CHANNEL_NAME,
    SUGGESTIONS_CHANNEL_NAME,
    PROJECTS_CHANNEL_NAME,
    ARTWORK_CHANNEL_NAME
)
from levels import REACTION_WEIGHTS
from collections import defaultdict

def setup(bot):  # Changed from nothing to this
    """Setup submissions extension"""
    logger.info("Submissions extension loaded (no setup required)")

logger = logging.getLogger('submissions')

CHANNEL_CONFIG = {
    PROJECTS_CHANNEL_NAME: {
        "reaction_emojis": REACTION_EMOJIS,
        "warning": "projects",
        "xp_reward": 20,
        "content_check": lambda msg: bool(msg.attachments) or bool(re.search(r"https?://\S+", msg.content))
    },
    ARTWORK_CHANNEL_NAME: {
        "reaction_emojis": REACTION_EMOJIS,
        "warning": "artwork",
        "xp_reward": 20,
        "content_check": lambda msg: any(
            att.content_type and "image" in att.content_type
            for att in msg.attachments
        ) or bool(re.search(
            r"https?://\S+\.(png|jpg|jpeg|gif|webp)(\?|$)",
            msg.content,
            re.IGNORECASE
        ))
    },
    SUGGESTIONS_CHANNEL_NAME: {
        "reaction_emojis": ["👍", "👎"],
        "warning": "suggestions",
        "xp_reward": 10,
        "content_check": lambda msg: True  # No content requirements for suggestions
    }
}


async def handle_submission(bot, message):
    """Handle submissions in projects, artwork, and suggestions channels"""
    channel_name = message.channel.name
    config = CHANNEL_CONFIG.get(channel_name)
    
    if not config:
        logger.debug(f"Ignoring message in non-submission channel: {channel_name}")
        return
    
    has_content = bool(message.attachments) or re.search(r"https?://\S+", message.content)
    is_valid = has_content and config["content_check"](message)

    if is_valid:
        try:
            for emoji in config["reaction_emojis"]:
                await message.add_reaction(emoji)
            
            thread_title = message.content[:50] if message.content else f"{message.author.display_name}'s post"
            await message.create_thread(name=thread_title, auto_archive_duration=1440)
            
            bot.VOTE_TRACKER[message.id] = message.created_at
            await bot.level_system.add_xp(message.author.id, config["xp_reward"], message)
            logger.info(f"Processed valid submission in {channel_name} by {message.author}")
        except discord.HTTPException as e:
            logger.error(f"Error processing submission: {e}")
    else:
        try:
            await message.delete()
            warning_msg = (
                f"Your message in #{channel_name} was removed because it didn't meet submission requirements.\n"
                f"Please include a valid {'image ' if channel_name == ARTWORK_CHANNEL_NAME else ''}link/attachment.\n"
                f"Discuss {config['warning']} in #{PROJECT_DISCUSSION_CHANNEL_NAME if channel_name != SUGGESTIONS_CHANNEL_NAME else SUGGESTIONS_CHANNEL_NAME}."
            )
            await message.author.send(warning_msg)
            logger.info(f"Removed invalid submission in {channel_name} by {message.author}")
        except discord.Forbidden:
            logger.warning(f"Couldn't DM user about removed submission: {message.author}")
        except Exception as e:
            logger.error(f"Error handling invalid submission: {e}")

async def handle_submission_reaction(bot, payload):
    """Handle reactions in submission channels"""
    channel = await bot.fetch_channel(payload.channel_id)
    config = CHANNEL_CONFIG.get(channel.name)
    
    if not config:
        logger.debug(f"Ignoring reaction in non-submission channel: {channel.name}")
        return

    if payload.user_id == bot.user.id:
        logger.debug("Ignoring bot's own reaction")
        return

    emoji_str = str(payload.emoji)
    if emoji_str not in REACTION_WEIGHTS:
        logger.debug(f"Ignoring unweighted emoji: {emoji_str}")
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        logger.warning(f"Message not found for reaction: {payload.message_id}")
        return

    reaction_key = f"{payload.message_id}-{emoji_str}-{payload.user_id}"
    if reaction_key in bot.level_system.reaction_tracker:
        logger.debug(f"Ignoring duplicate reaction: {reaction_key}")
        return

    bot.level_system.reaction_tracker.add(reaction_key)
    xp = REACTION_WEIGHTS[emoji_str] * 5
    await bot.level_system.add_xp(message.author.id, xp, message)

    user_id = str(message.author.id)
    if user_id not in bot.level_system.data:
        bot.level_system.data[user_id] = {
            "xp": 0,
            "last_message": None,
            "voice_time": 0,
            "reactions_received": defaultdict(int)
        }

    bot.level_system.data[user_id]["reactions_received"][emoji_str] += 1
    bot.level_system._save_data()
    logger.info(f"Processed reaction {emoji_str} on message {message.id} by {payload.user_id}")