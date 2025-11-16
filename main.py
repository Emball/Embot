import asyncio
import discord
from discord.ext import commands
import os
import sys
import traceback
import time
from datetime import datetime
import importlib

# Bot version (auto-managed by version.py)
VERSION = "3.1.0"
# Initialize bot with intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

class Logger:
    """Centralized logging system for all modules"""
    
    @staticmethod
    def log(module_name: str, message: str, level: str = "INFO"):
        """
        Log a message with module name tag
        
        Args:
            module_name: Name of the module logging the message
            message: The message to log
            level: Log level (INFO, WARNING, ERROR)
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{module_name}] [{level}] {message}")

    @staticmethod
    def error(module_name: str, message: str, exception: Exception = None):
        """
        Log an error with optional exception details
        
        Args:
            module_name: Name of the module logging the error
            message: Error message
            exception: Optional exception object
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{module_name}] [ERROR] {message}")
        
        if exception:
            print(f"[{timestamp}] [{module_name}] [ERROR] Exception: {type(exception).__name__}: {str(exception)}")
            tb = ''.join(traceback.format_exception(type(exception), exception, exception.__traceback__))
            print(f"[{timestamp}] [{module_name}] [ERROR] Traceback:\n{tb}")

# Make logger available globally
bot.logger = Logger()

def load_modules():
    """Dynamically load all modules from the current directory"""
    modules_dir = os.path.dirname(os.path.abspath(__file__))
    
    loaded_count = 0
    failed_count = 0
    
    # Clear existing commands before reloading - use None for global commands
    bot.tree.clear_commands(guild=None)
    
    # Discover and load all Python modules in the current directory
    for file in os.listdir(modules_dir):
        if not file.endswith('.py') or file == 'main.py' or file.startswith('_'):
            continue
            
        name = file[:-3]  # Strip .py extension
        
        try:
            # Reload the module to get fresh code
            if name in sys.modules:
                module = importlib.reload(sys.modules[name])
            else:
                module = importlib.import_module(name)
            
            # Look for a setup function in the module
            if hasattr(module, 'setup'):
                module.setup(bot)
                bot.logger.log("MAIN", f"Loaded module: {name}")
                loaded_count += 1
            else:
                bot.logger.log("MAIN", f"Module {name} has no setup() function, skipping", "WARNING")
                
        except discord.app_commands.errors.CommandAlreadyRegistered as e:
            bot.logger.log("MAIN", f"Command already registered in {name}, skipping: {e}", "WARNING")
            failed_count += 1
        except Exception as e:
            bot.logger.error("MAIN", f"Failed to load module: {name}", e)
            failed_count += 1
    
    bot.logger.log("MAIN", f"Successfully loaded {loaded_count} module(s), {failed_count} failed")

@bot.event
async def on_ready():
    # Only run initialization on first ready, not on reconnects
    if not hasattr(bot, 'initialized'):
        bot.initialized = True
        bot.logger.log("MAIN", f"Embot online as {bot.user}")
        
        # Start a background task to monitor heartbeat
        bot.heartbeat_monitor = bot.loop.create_task(monitor_heartbeat())
        
        load_modules()                     # load your real commands
        await bot.tree.sync()              # now push the fresh ones
        bot.logger.log("MAIN", "Fresh commands synced")
    else:
        bot.logger.log("MAIN", f"Embot reconnected as {bot.user}")
        # On reconnect, just sync any potential command changes
        await bot.tree.sync()
        bot.logger.log("MAIN", "Commands resynced after reconnect")

async def monitor_heartbeat():
    """Monitor and log heartbeat health"""
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        try:
            # Check the last heartbeat time
            latency = bot.latency
            if latency > 1.0:  # High latency warning
                bot.logger.log("MAIN", f"High latency detected: {latency:.2f}s", "WARNING")
            
            await asyncio.sleep(60)  # Check every minute
        except Exception as e:
            bot.logger.error("MAIN", "Heartbeat monitor error", e)
            await asyncio.sleep(60)

@bot.event
async def on_error(event, *args, **kwargs):
    """Global error handler for bot events"""
    exc_type, exc_value, exc_traceback = sys.exc_info()
    bot.logger.error("MAIN", f"Error in event {event}", exc_value)

@bot.event
async def on_command_error(ctx, error):
    """Global error handler for commands"""
    if isinstance(error, commands.CommandNotFound):
        return
    
    bot.logger.error("MAIN", f"Command error in {ctx.command}", error)
    await ctx.send(f"An error occurred: {str(error)}")

if __name__ == "__main__":
    # Get token from environment variable
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        bot.logger.error("MAIN", "DISCORD_BOT_TOKEN environment variable not set!")
        bot.logger.log("MAIN", "Please set your Discord bot token:")
        bot.logger.log("MAIN", "export DISCORD_BOT_TOKEN='your-token-here'")
        sys.exit(1)
    
    try:
        bot.logger.log("MAIN", "Starting Embot...")
        bot.run(TOKEN)
    except Exception as e:
        bot.logger.error("MAIN", "Failed to start bot", e)
        sys.exit(1)