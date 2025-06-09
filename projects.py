import re
import discord
from config import *
from config import REACTION_EMOJIS, PROJECT_DISCUSSION_CHANNEL_NAME
from levels import REACTION_WEIGHTS

async def handle_project_submission(bot, message):
    has_attachment = bool(message.attachments)
    has_link = re.search(r"https?://\S+", message.content) is not None
    is_valid = has_attachment or has_link

    if is_valid:
        try:
            for emoji in REACTION_EMOJIS:
                await message.add_reaction(emoji)
            thread_title = message.content[:50] if message.content else f"{message.author.display_name}'s post"
            await message.create_thread(name=thread_title, auto_archive_duration=1440)
            bot.VOTE_TRACKER[message.id] = message.created_at

            # Add base XP for posting project
            await bot.level_system.add_xp(message.author.id, 20, message)
        except discord.HTTPException as e:
            print(f"Error creating thread: {e}")
    else:
        try:
            await message.delete()
            warning_msg = (
                f"Your message in #projects was removed because it didn't meet submission requirements.\n"
                f"Please include a valid link/attachment.\n"
                f"Discuss projects in #{PROJECT_DISCUSSION_CHANNEL_NAME}."
            )
            await message.author.send(warning_msg)
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"Error handling invalid project submission: {e}")

async def handle_project_reaction(bot, payload):
    channel = await bot.fetch_channel(payload.channel_id)
    if channel.name != PROJECTS_CHANNEL_NAME:
        return

    if payload.user_id == bot.user.id:
        return

    if str(payload.emoji) not in REACTION_WEIGHTS:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    reaction_key = f"{payload.message_id}-{payload.emoji}-{payload.user_id}"
    if reaction_key in bot.level_system.reaction_tracker:
        return

    bot.level_system.reaction_tracker.add(reaction_key)
    xp = REACTION_WEIGHTS[str(payload.emoji)] * 5
    await bot.level_system.add_xp(message.author.id, xp, message)

    user_id = str(message.author.id)
    if user_id not in bot.level_system.data:
        bot.level_system.data[user_id] = {
            "xp": 0,
            "last_message": None,
            "voice_time": 0,
            "reactions_received": defaultdict(int)
        }

    bot.level_system.data[user_id]["reactions_received"][str(payload.emoji)] += 1
    bot.level_system._save_data()
