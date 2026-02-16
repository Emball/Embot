import asyncio
import discord
from discord.ext import commands
from discord import HTTPException
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
import signal

# Parse command line arguments
parser = argparse.ArgumentParser(description='Embot Discord Bot')
parser.add_argument('-dev', '--development', action='store_true', 
                    help='Enable development mode (hot-reload, versioning, git integration)')
args = parser.parse_args()

# Get script directory and create data folder
script_dir = Path(__file__).parent.absolute()
data_dir = script_dir / "data"
data_dir.mkdir(exist_ok=True)

# Initialize bot with intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=['!', '?'], intents=intents)

class ConsoleLogger:
    """Centralized console and file logging system for all modules"""
    
    def __init__(self):
        self.prompt_active = False
        self.lock = threading.Lock()
        self.session_id = self._generate_session_id()
        self.log_file = None
        self._init_log_file()
    
    def _generate_session_id(self) -> str:
        """Generate a unique session ID based on timestamp"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"session_{timestamp}"
    
    def _init_log_file(self):
        """Initialize the log file for this session"""
        # Create session-specific log file
        log_filename = f"{self.session_id}.log"
        self.log_file = data_dir / log_filename
        
        # Write initial log entry
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write(f"=== Embot Session Log - {self.session_id} ===\n")
            f.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 50 + "\n\n")
        
        print(f"[LOGGER] Session log started: {self.log_file.name}")
    
    def _clear_line(self):
        """Clear the current line"""
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()
    
    def _restore_prompt(self):
        """Restore the > prompt after logging"""
        if self.prompt_active:
            sys.stdout.write('> ')
            sys.stdout.flush()
    
    def _write_to_file(self, message: str):
        """Write a message to the log file"""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(message + '\n')
        except Exception as e:
            # If file writing fails, just print to console
            sys.stderr.write(f"[LOGGER ERROR] Failed to write to log file: {e}\n")
    
    def log(self, module_name: str, message: str, level: str = "INFO"):
        """
        Log a message with module name tag to both console and file
        
        Args:
            module_name: Name of the module logging the message
            message: The message to log
            level: Log level (INFO, WARNING, ERROR)
        """
        with self.lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_message = f"[{timestamp}] [{module_name}] [{level}] {message}"
            
            # Write to console
            self._clear_line()
            print(log_message)
            self._restore_prompt()
            
            # Write to file
            self._write_to_file(log_message)

    def error(self, module_name: str, message: str, exception: Exception = None):
        """
        Log an error with optional exception details to both console and file
        
        Args:
            module_name: Name of the module logging the error
            message: Error message
            exception: Optional exception object
        """
        with self.lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_message = f"[{timestamp}] [{module_name}] [ERROR] {message}"
            
            # Write to console
            self._clear_line()
            print(log_message)
            
            # Prepare error details for file
            file_message = log_message
            
            if exception:
                error_details = f"[{timestamp}] [{module_name}] [ERROR] Exception: {type(exception).__name__}: {str(exception)}"
                print(error_details)
                file_message += f"\n{error_details}"
                
                tb = ''.join(traceback.format_exception(type(exception), exception, exception.__traceback__))
                tb_message = f"[{timestamp}] [{module_name}] [ERROR] Traceback:\n{tb}"
                print(tb_message)
                file_message += f"\n{tb_message}"
            
            self._restore_prompt()
            
            # Write to file
            self._write_to_file(file_message)

# Make console logger available globally
bot.logger = ConsoleLogger()

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
    
    # Initialize command tracking
    if not hasattr(bot, '_module_commands'):
        bot._module_commands = {}
    
    # NEVER clear commands here - only do it once at startup if needed
    # Commands persist across reloads
    
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
            # Track commands before loading
            commands_before = {cmd.name: cmd for cmd in bot.tree.get_commands()}
            
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
                
                # Track commands added by this module
                commands_after = {cmd.name: cmd for cmd in bot.tree.get_commands()}
                new_commands = {cmd_name: cmd for cmd_name, cmd in commands_after.items() if cmd_name not in commands_before}
                if new_commands:
                    bot._module_commands[name] = new_commands
                
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

def start_console_thread():
    """Start the console thread - call this separately from on_ready"""
    console_thread = threading.Thread(target=run_console, daemon=True)
    console_thread.start()
    bot.logger.log("MAIN", "Console thread started")

@bot.event
async def on_ready():
    # Only run initialization on first ready, not on reconnects
    if not hasattr(bot, 'initialized'):
        bot.initialized = True
        
        # Load version from file
        bot.version = load_version()
        
        mode = "DEVELOPMENT MODE" if args.development else "PRODUCTION MODE"
        bot.logger.log("MAIN", f"Embot online as {bot.user} - {mode} - v{bot.version}")
        
        # Start console IMMEDIATELY - don't wait for anything else
        start_console_thread()
        
        # Start a background task to monitor heartbeat
        bot.heartbeat_monitor = bot.loop.create_task(monitor_heartbeat())
        
        # Load modules
        load_modules()
        
        # Wait a moment for modules to initialize
        await asyncio.sleep(2)
        
        # ONLY sync commands ONCE on initial startup
        try:
            synced = await bot.tree.sync()
            bot.logger.log("MAIN", f"Commands synced successfully: {len(synced)} commands")
        except HTTPException as e:
            if e.status == 429 and e.code == 30034:
                bot.logger.log("MAIN", "⚠️ Daily command sync limit reached (200/200). Commands will sync tomorrow.", "WARNING")
            else:
                bot.logger.error("MAIN", "Command sync failed", e)
        except Exception as e:
            bot.logger.error("MAIN", "Command sync failed", e)
            bot.logger.log("MAIN", "Bot will continue running with existing commands")
    else:
        bot.logger.log("MAIN", f"Embot reconnected as {bot.user}")
        # On reconnect, don't sync commands again to avoid rate limits
        bot.logger.log("MAIN", "Reconnected successfully (commands not resynced to avoid rate limits)")

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
    # Small delay to ensure bot is ready
    time.sleep(2)
    bot.logger.log("CONSOLE", "Console ready. Type 'help' for commands.")
    
    # Activate prompt
    bot.logger.prompt_active = True
    
    while True:
        try:
            # Use input with prompt
            cmd = input("> ").strip()
            
            # Clear prompt temporarily during command execution
            bot.logger.prompt_active = False
            
            if not cmd:
                bot.logger.prompt_active = True
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
                print(f"❌ Unknown command: {command}. Type 'help' for available commands.")
            
            # Reactivate prompt
            bot.logger.prompt_active = True
                
        except KeyboardInterrupt:
            print("\nUse 'exit' to shutdown gracefully.")
            bot.logger.prompt_active = True
        except Exception as e:
            print(f"⚠️ Error: {e}")
            bot.logger.prompt_active = True

def print_help():
    """Print available console commands"""
    help_text = """
┌─────────────────────────────────────────────────────────────────────┐
│                         BOT CONSOLE                                 │
├─────────────────────────────────────────────────────────────────────┤
│ help              - Show this help message                          │
│ status            - Show bot status                                 │
│ version           - Show current version                            │"""
    
    # Add module-registered commands
    for cmd_name, cmd_info in sorted(bot.console_commands.items()):
        help_text += f"\n│ {cmd_name:<17} - {cmd_info['description']:<35} │"
    
    help_text += """
│ exit / quit       - Shutdown bot gracefully                         │
└─────────────────────────────────────────────────────────────────────┘
"""
    print(help_text)

def show_status():
    """Show bot status"""
    print("\n┌─────────────────────────────────────────────────────────────────────┐")
    print(f"│ Bot Status - v{bot.version if hasattr(bot, 'version') else '0.0.0.0':<43} │")
    print("├─────────────────────────────────────────────────────────────────────┤")
    
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
    
    # Log file info
    if hasattr(bot.logger, 'log_file'):
        log_file_name = bot.logger.log_file.name
        print(f"│ Log file: {log_file_name:<46} │")
    
    print("└─────────────────────────────────────────────────────────────────────┘\n")

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
            # First, get list of existing commands from this module
            existing_commands = {}
            if hasattr(bot, '_module_commands'):
                existing_commands = bot._module_commands.get(module_name, {})
            else:
                bot._module_commands = {}
            
            # Remove existing commands from tree to prevent "already registered" errors
            removed_count = 0
            if existing_commands:
                for cmd_name in list(existing_commands.keys()):
                    try:
                        bot.tree.remove_command(cmd_name)
                        removed_count += 1
                    except:
                        pass
                print(f"  Removed {removed_count} existing command(s)")
            
            # Reload the module
            if module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)
            
            if hasattr(module, 'setup'):
                # Track commands before setup
                commands_before = {cmd.name: cmd for cmd in bot.tree.get_commands()}
                
                # Re-setup the module
                if module_name == 'dev':
                    module.setup(bot, register_console_command)
                else:
                    module.setup(bot)
                
                # Track new commands from this module
                commands_after = {cmd.name: cmd for cmd in bot.tree.get_commands()}
                new_commands = {name: cmd for name, cmd in commands_after.items() if name not in commands_before}
                bot._module_commands[module_name] = new_commands
                
                if new_commands:
                    print(f"  Registered {len(new_commands)} command(s): {', '.join(new_commands.keys())}")
                
                print(f"✅ Module '{module_name}' reloaded successfully (commands updated, no sync needed)")
            else:
                print(f"✅ Module '{module_name}' reloaded (no setup function)")
                
        except Exception as e:
            print(f"❌ Failed to reload {module_name}: {e}")
            import traceback
            traceback.print_exc()
    
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
    
    async def handle_logs(args):
        """Show log file information"""
        if hasattr(bot.logger, 'log_file') and bot.logger.log_file.exists():
            file_size = bot.logger.log_file.stat().st_size
            print(f"\n📋 Current log file: {bot.logger.log_file.name}")
            print(f"   Size: {file_size:,} bytes")
            print(f"   Location: {bot.logger.log_file}")
            
            # List other log files in data directory
            log_files = list(data_dir.glob("session_*.log"))
            if len(log_files) > 1:
                print(f"\n📁 Other log files in {data_dir}:")
                for log_file in sorted(log_files, key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                    if log_file != bot.logger.log_file:
                        size = log_file.stat().st_size
                        mtime = datetime.fromtimestamp(log_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                        print(f"   • {log_file.name} ({size:,} bytes, {mtime})")
        else:
            print("❌ No log file found")
        print()
    
    register_console_command("reload", "Reload a specific module", handle_reload)
    register_console_command("modules", "List loaded modules", handle_modules)
    register_console_command("logs", "Show log file information", handle_logs)

@bot.event
async def on_error(event, *args, **kwargs):
    """Global error handler for bot events"""
    exc_type, exc_value, exc_traceback = sys.exc_info()
    
    # Handle rate limits specifically
    if isinstance(exc_value, HTTPException) and exc_value.status == 429:
        retry_after = getattr(exc_value, 'retry_after', 0)
        bot.logger.log("MAIN", f"Rate limited on event {event}. Retry after: {retry_after}s", "WARNING")
        # Wait out the rate limit
        await asyncio.sleep(retry_after)
    else:
        bot.logger.error("MAIN", f"Error in event {event}", exc_value)

@bot.event
async def on_command_error(ctx, error):
    """Global error handler for commands"""
    if isinstance(error, commands.CommandNotFound):
        return
    
    # Handle rate limits
    if isinstance(error, HTTPException) and error.status == 429:
        retry_after = getattr(error, 'retry_after', 0)
        bot.logger.log("MAIN", f"Rate limited on command {ctx.command}. Retry after: {retry_after}s", "WARNING")
        await asyncio.sleep(retry_after)
        # Retry the command after rate limit
        await ctx.reinvoke()
    else:
        bot.logger.error("MAIN", f"Command error in {ctx.command}", error)
        await ctx.send(f"An error occurred: {str(error)}")

# ==================== GRACEFUL SHUTDOWN HANDLER ====================

async def shutdown_bot(signame):
    """Gracefully shutdown the bot"""
    bot.logger.log("MAIN", f"Received signal {signame}, shutting down gracefully...")
    
    # Save any in-memory state that needs flushing
    # (Most modules already save atomically on changes, but this provides a safety net)
    
    try:
        await bot.close()
        bot.logger.log("MAIN", "Bot closed successfully")
    except Exception as e:
        bot.logger.error("MAIN", "Error during shutdown", e)

def handle_signal(signum, frame):
    """Handle termination signals"""
    signame = signal.Signals(signum).name
    
    # Create task to shutdown gracefully
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown_bot(signame))

if __name__ == "__main__":
    # Get token from environment variable
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        bot.logger.error("MAIN", "DISCORD_BOT_TOKEN environment variable not set!")
        bot.logger.log("MAIN", "Please set your Discord bot token:")
        bot.logger.log("MAIN", "export DISCORD_BOT_TOKEN='your-token-here'")
        sys.exit(1)
    
    try:
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        
        # Setup console commands before starting
        setup_console_commands()
        
        mode_str = " with development mode" if args.development else ""
        bot.logger.log("MAIN", f"Starting Embot v{load_version()}{mode_str}...")
        bot.logger.log("MAIN", f"Log files will be saved to: {data_dir}")
        bot.logger.log("MAIN", "Signal handlers registered for graceful shutdown")
        bot.run(TOKEN)
    except Exception as e:
        bot.logger.error("MAIN", "Failed to start bot", e)
        sys.exit(1)