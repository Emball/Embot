import discord
from bot import bot
from config import *
from bot_token import BOT_TOKEN
from emarchive import setup_emarchive
from play import setup_play
import commands
import magic_emball
from config import GUILD_ID
import levels
import emarchive

# Create the level system instance
bot.level_system = levels.LevelSystem(bot)

# Setup commands
commands.setup_commands(bot)
magic_emball.setup_magic_emball(bot)
levels.setup_level_commands(bot, bot.level_system)
emarchive.setup_emarchive(bot)
setup_play(bot)

async def sync_commands():
    await bot.wait_until_ready()
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"Synced {len(synced)} commands: {[cmd.name for cmd in synced]}")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.event
async def on_connect():
    await sync_commands()

if __name__ == "__main__":
    bot.run(BOT_TOKEN)