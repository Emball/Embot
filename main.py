import asyncio
import discord
from discord.ext import commands
import os
import sys
import traceback
import time
from datetime import datetime
import importlib
import argparse
import threading
from pathlib import Path
import json 
import re

# Parse command line arguments
parser = argparse.ArgumentParser(description='Embot Discord Bot')
parser.add_argument('-dev', '--development', action='store_true', 
                    help='Enable development mode (hot-reload, versioning, git integration)')
args = parser.parse_args()

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

# Console command registry - available to all modules
bot.console_commands = {}

def register_console_command(name, description, handler):
    """
    Register a console command that can be called from any module
    
    Args:
        name: Command name
        description: Help text for the command
        handler: Async function that handles the command
    """
    bot.console_commands[name] = {
        'description': description,
        'handler': handler
    }
    bot.logger.log("CONSOLE", f"Registered console command: {name}")

def load_version():
    """Load version from _version.py file"""
    try:
        # Read the file directly without importing to avoid dependency issues
        version_file = Path("_version.py")
        if version_file.exists():
            with open(version_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Extract version using regex to avoid importing
            match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                return match.group(1)
        
        # Fallback to unknown version if file doesn't exist or version not found
        return "0.0.0.0"
        
    except Exception as e:
        bot.logger.error("MAIN", "Failed to load version from _version.py", e)
        return "0.0.0.0"

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
        
        # Skip dev.py if not in development mode
        if file == 'dev.py' and not args.development:
            bot.logger.log("MAIN", "Skipping dev.py (not in development mode)")
            continue
        
        # Skip version.py if in development mode (dev.py replaces it)
        if file == 'version.py' and args.development:
            bot.logger.log("MAIN", "Skipping version.py (using dev.py instead)")
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
                # Only pass register_console_command to dev module, others get just bot
                if name == 'dev':
                    module.setup(bot, register_console_command)
                else:
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
        
        # Load version from file
        bot.version = load_version()
        
        mode = "DEVELOPMENT MODE" if args.development else "PRODUCTION MODE"
        bot.logger.log("MAIN", f"Embot online as {bot.user} - {mode} - v{bot.version}")
        
        # Start a background task to monitor heartbeat
        bot.heartbeat_monitor = bot.loop.create_task(monitor_heartbeat())
        
        load_modules()
        await bot.tree.sync()
        bot.logger.log("MAIN", "Commands synced")
        
        # Start console in all modes
        console_thread = threading.Thread(target=run_console, daemon=True)
        console_thread.start()
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

def run_console():
    """Run interactive console for commands - available in all modes"""
    bot.logger.log("CONSOLE", "Console ready. Type 'help' for commands.")
    
    while True:
        try:
            cmd = input("> ").strip()
            
            if not cmd:
                continue
            
            parts = cmd.split(maxsplit=1)
            command = parts[0].lower()
            args_str = parts[1] if len(parts) > 1 else ""
            
            if command == "help":
                print_help()
            elif command == "status":
                show_status()
            elif command == "version":
                show_version()
            elif command in bot.console_commands:
                # Execute registered console command
                cmd_info = bot.console_commands[command]
                asyncio.run_coroutine_threadsafe(
                    cmd_info['handler'](args_str),
                    bot.loop
                ).result(timeout=30)
            elif command == "exit" or command == "quit":
                print("Shutting down bot...")
                asyncio.run_coroutine_threadsafe(bot.close(), bot.loop)
                break
            else:
                print(f"❓ Unknown command: {command}. Type 'help' for available commands.")
                
        except KeyboardInterrupt:
            print("\nUse 'exit' to shutdown gracefully.")
        except Exception as e:
            print(f"⚠️ Error: {e}")

def print_help():
    """Print available console commands"""
    help_text = """
┌─────────────────────────────────────────────────────────────────────────┐
│                         BOT CONSOLE                                     │
├─────────────────────────────────────────────────────────────────────────┤
│ help              - Show this help message                              │
│ status            - Show bot status                                     │
│ version           - Show current version                                │"""
    
    # Add module-registered commands
    for cmd_name, cmd_info in sorted(bot.console_commands.items()):
        help_text += f"\n│ {cmd_name:<17} - {cmd_info['description']:<35} │"
    
    help_text += """
│ exit / quit       - Shutdown bot gracefully                             │
└─────────────────────────────────────────────────────────────────────────┘
"""
    print(help_text)

def show_status():
    """Show bot status"""
    print("\n┌─────────────────────────────────────────────────────────────────────────┐")
    print(f"│ Bot Status - v{bot.version if hasattr(bot, 'version') else '0.0.0.0':<43} │")
    print("├─────────────────────────────────────────────────────────────────────────┤")
    
    # Basic bot info
    if bot.user:
        print(f"│ Logged in as: {bot.user.name:<45} │")
        print(f"│ User ID: {bot.user.id:<49} │")
    
    # Guild info
    if bot.guilds:
        print(f"│ Servers: {len(bot.guilds):<50} │")
        for guild in list(bot.guilds)[:3]:  # Show first 3 guilds
            guild_name = guild.name[:45] + "..." if len(guild.name) > 45 else guild.name
            print(f"│   • {guild_name:<47} │")
        if len(bot.guilds) > 3:
            print(f"│   ... and {len(bot.guilds) - 3} more{' ' * (40 - len(str(len(bot.guilds) - 3)))}│")
    
    # Latency
    latency = getattr(bot, 'latency', 0) * 1000
    print(f"│ Latency: {latency:.0f}ms{' ' * (48 - len(f'{latency:.0f}ms'))} │")
    
    # Module info - dynamic discovery
    bot_modules = []
    for name, module in sys.modules.items():
        if (hasattr(module, '__file__') and 
            module.__file__ and 
            'site-packages' not in module.__file__ and
            os.path.dirname(os.path.abspath(__file__)) in module.__file__ and
            name not in ['main', '__main__']):
            bot_modules.append(name)
    
    print(f"│ Loaded modules: {len(bot_modules)}{' ' * (45 - len(str(len(bot_modules))))} │")
    
    # Development mode
    mode = "🔧 DEVELOPMENT" if args.development else "⚙️ PRODUCTION"
    print(f"│ Mode: {mode:<47} │")
    
    # Console commands info
    print(f"│ Console commands: {len(bot.console_commands)}{' ' * (42 - len(str(len(bot.console_commands))))} │")
    
    print("└─────────────────────────────────────────────────────────────────────────┘\n")

def show_version():
    """Show version information"""
    version = bot.version if hasattr(bot, 'version') else "0.0.0.0"
    print(f"\n🎯 Embot v{version}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Discord.py: {discord.__version__}")
    print()

# Register core console commands
def setup_console_commands():
    """Setup core console commands available in all modes"""
    async def handle_reload(args):
        """Reload modules command"""
        if not args.strip():
            print("⚠️ Usage: reload <module_name>")
            return
        
        module_name = args.strip()
        file_path = Path(f"{module_name}.py")
        if not file_path.exists():
            print(f"⚠️ Module '{module_name}.py' not found")
            return
        
        print(f"🔄 Reloading {module_name}...")
        try:
            if module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)
            
            if hasattr(module, 'setup'):
                # Clear existing commands for this module
                bot.tree.clear_commands(guild=None)
                
                # Re-setup the module - only pass register_console_command to dev module
                if module_name == 'dev':
                    module.setup(bot, register_console_command)
                else:
                    module.setup(bot)
                
                # Sync commands
                await bot.tree.sync()
                
                print(f"✅ Module '{module_name}' reloaded and synced successfully!")
            else:
                print(f"✅ Module '{module_name}' reloaded (no setup function)")
                
        except Exception as e:
            print(f"❌ Failed to reload {module_name}: {e}")
    
    async def handle_modules(args):
        """List loaded modules command"""
        bot_modules = []
        for name, module in sys.modules.items():
            if (hasattr(module, '__file__') and 
                module.__file__ and 
                'site-packages' not in module.__file__ and
                os.path.dirname(os.path.abspath(__file__)) in module.__file__ and
                name not in ['main', '__main__']):
                bot_modules.append(name)
        
        print(f"\n📦 Loaded Modules ({len(bot_modules)}):")
        for module in sorted(bot_modules):
            print(f"  • {module}")
        print()
    
    register_console_command("reload", "Reload a specific module", handle_reload)
    register_console_command("modules", "List loaded modules", handle_modules)

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
        # Setup console commands before starting
        setup_console_commands()
        
        mode_str = " with development mode" if args.development else ""
        bot.logger.log("MAIN", f"Starting Embot v{load_version()}{mode_str}...")
        bot.run(TOKEN)
    except Exception as e:
        bot.logger.error("MAIN", "Failed to start bot", e)
        sys.exit(1)