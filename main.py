# main.py
import discord
import logging
import vaulted
from bot import bot
from config import (
    BOT_TOKEN, 
    GUILD_ID,
    VERSION,
    setup_logging
)
import commands
import magic_emball
import levels
import emarchive
import submissions
import voice_messages
import play
import member_management  # New import
from splash import show_console_splash, send_startup_message

# Configure logging first
setup_logging()
logger = logging.getLogger('main')

def setup_extensions():
    """Initialize all bot extensions"""
    logger.info("Setting up extensions...")
    
    # Create the level system instance
    bot.level_system = levels.LevelSystem(bot)
    logger.info("Level system initialized")

    # List of all extensions to load
    extensions = [
        ('commands', commands),
        ('magic_emball', magic_emball),
        ('levels', levels),
        ('emarchive', emarchive),
        ('play', play),
        ('submissions', submissions),
        ('voice_messages', voice_messages),
        ('member_management', member_management),
        ('vaulted', vaulted)  # Add this line
    ]

    print(f"Trying to load vaulted: {hasattr(vaulted, 'setup')}") 

    loaded_extensions = 0
    for name, ext in extensions:
        try:
            if hasattr(ext, 'setup'):
                ext.setup(bot)
                loaded_extensions += 1
                logger.info(f"Loaded extension: {name}")
            else:
                logger.warning(f"Extension {name} has no setup function")
        except Exception as e:
            logger.error(f"Failed to load extension {name}: {e}")

    bot.loaded_extensions_count = loaded_extensions
    logger.info(f"Total extensions loaded: {loaded_extensions}")

async def sync_commands():
    """Sync slash commands with Discord"""
    await bot.wait_until_ready()
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        logger.info(f"Synced {len(synced)} commands: {[cmd.name for cmd in synced]}")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

@bot.event
async def on_connect():
    """Called when bot connects to Discord"""
    logger.info("Connected to Discord")
    await sync_commands()

@bot.event
async def on_ready():
    """Called when bot is fully ready"""
    show_console_splash(VERSION)
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    
    # Ensure we have guild reference
    bot.guild = bot.get_guild(GUILD_ID)
    if not bot.guild:
        logger.error(f"Failed to find guild with ID {GUILD_ID}")
        return
    
    # Get important channels
    bot.project_channel = discord.utils.get(bot.guild.text_channels, name="projects")
    bot.artwork_channel = discord.utils.get(bot.guild.text_channels, name="artwork")
    bot.suggestions_channel = discord.utils.get(bot.guild.text_channels, name="suggestions")
    bot.general_channel = discord.utils.get(bot.guild.text_channels, name="general")
    bot.announcements_channel = discord.utils.get(bot.guild.text_channels, name="announcements")
    bot.bot_logs_channel = discord.utils.get(bot.guild.text_channels, name="bot-logs")
    
    # Send startup message
    await send_startup_message(bot, VERSION)
    logger.info("Bot is fully initialized and ready")

if __name__ == "__main__":
    try:
        setup_extensions()
        logger.info("Starting bot...")
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.critical(f"Fatal error during startup: {e}")
        raise