# bot.py
import discord
from discord.ext import commands
import logging
from config import (
    GUILD_ID,
    PROJECTS_CHANNEL_NAME,
    ARTWORK_CHANNEL_NAME,
    SUGGESTIONS_CHANNEL_NAME,
    GENERAL_CHANNEL_NAME,
    ANNOUNCEMENTS_CHANNEL_NAME,
    TRACKER_CHANNEL_NAME,
    BOT_LOGS_CHANNEL_NAME,
    setup_logging
)
import submissions
import voice_messages
import levels

logger = logging.getLogger('bot')

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True 
intents.messages = True
intents.reactions = True
intents.voice_states = True

class Embot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="your commands"
            )
        )
        self.VOTE_TRACKER = {}
        self.guild = None
        self.project_channel = None
        self.artwork_channel = None
        self.suggestions_channel = None
        self.general_channel = None
        self.tracker_channel = None
        self.announcements_channel = None
        self.bot_logs_channel = None
        self.level_system = None

bot = Embot()

@bot.event
async def on_ready():
    """Called when bot connects to Discord"""
    try:
        setup_logging()
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        
        bot.guild = bot.get_guild(GUILD_ID)
        if not bot.guild:
            logger.error(f"Failed to find guild with ID {GUILD_ID}")
            return

        # Get all required channels
        bot.project_channel = discord.utils.get(bot.guild.text_channels, name=PROJECTS_CHANNEL_NAME)
        bot.artwork_channel = discord.utils.get(bot.guild.text_channels, name=ARTWORK_CHANNEL_NAME)
        bot.suggestions_channel = discord.utils.get(bot.guild.text_channels, name=SUGGESTIONS_CHANNEL_NAME)
        bot.general_channel = discord.utils.get(bot.guild.text_channels, name=GENERAL_CHANNEL_NAME)
        bot.tracker_channel = discord.utils.get(bot.guild.text_channels, name=TRACKER_CHANNEL_NAME)
        bot.announcements_channel = discord.utils.get(bot.guild.text_channels, name=ANNOUNCEMENTS_CHANNEL_NAME)
        bot.bot_logs_channel = discord.utils.get(bot.guild.text_channels, name=BOT_LOGS_CHANNEL_NAME)

        # Initialize systems
        bot.level_system = levels.LevelSystem(bot)
        voice_messages.random_voice_drop.start(bot)

        # Sync commands
        try:
            guild_obj = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild_obj)
            logger.info(f"Synced {len(synced)} commands")
        except Exception as e:
            logger.error(f"Command sync error: {e}")

        logger.info("Bot is ready")
    except Exception as e:
        logger.critical(f"Startup failed: {e}")

@bot.event 
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    if message.author.bot:
        return

    try:
        # Handle voice messages
        await voice_messages.handle_general_voice_message(bot, message)
        
        # Handle submissions in monitored channels
        if message.channel.name in [PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME, SUGGESTIONS_CHANNEL_NAME]:
            await submissions.handle_submission(bot, message)
        else:
            # Regular message XP
            await bot.level_system.handle_message_xp(message)
            
        await bot.process_commands(message)
    except Exception as e:
        logger.error(f"Message handling error: {e}")

@bot.event
async def on_raw_reaction_add(payload):
    """Handle reaction adds"""
    try:
        channel = bot.get_channel(payload.channel_id)
        if not channel:
            return
            
        if channel.name in [PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME, SUGGESTIONS_CHANNEL_NAME]:
            await submissions.handle_submission_reaction(bot, payload)
    except Exception as e:
        logger.error(f"Reaction handling error: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes"""
    try:
        await bot.level_system.handle_voice_xp(member, before, after)
    except Exception as e:
        logger.error(f"Voice state update error: {e}")

@bot.event
async def on_error(event, *args, **kwargs):
    """Global error handler"""
    logger.error(f"Unhandled error in {event}: {args} {kwargs}")

if __name__ == "__main__":
    try:
        bot.run()
    except Exception as e:
        logger.critical(f"Fatal bot error: {e}")