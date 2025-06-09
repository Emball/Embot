import discord
from discord.ext import commands
from config import *
from config import GUILD_ID, PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME
import projects
import artwork
import voice_messages
import tracker_updates
import spotlight_friday
import levels

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True
intents.reactions = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global state
bot.project_channel = None
bot.artwork_channel = None
bot.general_channel = None
bot.tracker_channel = None
bot.announcements_channel = None
bot.VOTE_TRACKER = {}
bot.gc = None
bot.sheet = None
bot.cached_values = {}
bot.level_system = levels.LevelSystem(bot)  # Initialize here

@bot.event
async def on_ready():
    from config import (
        VERSION,
        GUILD_ID,
        PROJECTS_CHANNEL_NAME,
        ARTWORK_CHANNEL_NAME,
        GENERAL_CHANNEL_NAME,
        TRACKER_CHANNEL_NAME,
        ANNOUNCEMENTS_CHANNEL_NAME,
    )

    print(f"[v{VERSION}] Logged in as {bot.user}")
    guild = bot.get_guild(GUILD_ID)

    if not guild:
        print(f"Failed to find guild with ID {GUILD_ID}")
        return

    bot.project_channel = discord.utils.get(guild.text_channels, name=PROJECTS_CHANNEL_NAME)
    bot.artwork_channel = discord.utils.get(guild.text_channels, name=ARTWORK_CHANNEL_NAME)
    bot.general_channel = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL_NAME)
    bot.tracker_channel = discord.utils.get(guild.text_channels, name=TRACKER_CHANNEL_NAME)
    bot.announcements_channel = discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CHANNEL_NAME)

    try:
        guild_obj = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"Commands synced: {[cmd.name for cmd in synced]}")
    except Exception as e:
        print(f"Sync error: {str(e)}")

    # Initialize modules (removed level command setup from here)
    if await tracker_updates.setup_google_sheets(bot):
        tracker_updates.tracker_update_loop.start(bot)
    spotlight_friday.spotlight_friday.start(bot)
    voice_messages.random_voice_drop.start(bot)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await voice_messages.handle_general_voice_message(bot, message)

    if message.channel == bot.project_channel:
        await projects.handle_project_submission(bot, message)
    elif message.channel == bot.artwork_channel:
        await artwork.handle_artwork_submission(bot, message)
    else:
        await bot.level_system.handle_message_xp(message)

    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload):
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
        
    if channel.name == PROJECTS_CHANNEL_NAME:
        await projects.handle_project_reaction(bot, payload)
    elif channel.name == ARTWORK_CHANNEL_NAME:
        await artwork.handle_artwork_reaction(bot, payload)

@bot.event
async def on_voice_state_update(member, before, after):
    await bot.level_system.handle_voice_xp(member, before, after)