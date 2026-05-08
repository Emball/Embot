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
import subprocess
from pathlib import Path
import json 
import re
import signal

# Parse command line arguments
parser = argparse.ArgumentParser(description='Embot Discord Bot')
parser.add_argument('-dev', '--development', action='store_true', 
                    help='Enable development mode (versioning, git integration)')
parser.add_argument('-t', '--test', action='store_true',
                    help='Dry-run: validate startup, sync commands, then exit')
args = parser.parse_args()

# Get script directory and create logs folder
script_dir = Path(__file__).parent.absolute()
data_dir = script_dir / "logs"
data_dir.mkdir(exist_ok=True)

_CONFIG_PATH = script_dir / "config"/ "embot.json"
_CONFIG_DEFAULTS = {
    "command_prefixes":           ["!", "?"],
    "home_guild_id":              0,
    "latency_warning_threshold":  1.0,
    "heartbeat_interval_seconds": 60,
    "network": {
        "auto_update": True,
        "auto_update_interval_minutes": 5,
        "auto_update_git_remote": "",
    },
}

def load_config() -> dict:
    """Load config/embot.json, filling in defaults for any missing keys."""
    cfg = dict(_CONFIG_DEFAULTS)
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception as e:
            print(f"[MAIN] [WARNING] Failed to read {_CONFIG_PATH}, using defaults: {e}")
    else:
        # Write a starter config so the user knows what's available
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_CONFIG_DEFAULTS, f, indent=4)
        print(f"[MAIN] [INFO] Created default config at {_CONFIG_PATH}")
    return cfg

_cfg = load_config()

# Initialize bot with intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # Required for Member lookup in prefix commands

bot = commands.Bot(command_prefix=_cfg["command_prefixes"], intents=intents)

class ConsoleLogger:
    """Centralized console and file logging system for all modules"""
    
    def __init__(self):
        self.prompt_active = False
        self.prompt_lock = threading.Lock()
        self.lock = threading.Lock()
        self.session_id = self._generate_session_id()
        self.log_file = None
        self._init_log_file()
        self._cleanup_old_logs(retention_days=30)
    
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
            f.write("="* 50 + "\n\n")
        
        print(f"[LOGGER] Session log started: {self.log_file.name}")
    
    def _cleanup_old_logs(self, retention_days=30):
        try:
            cutoff = datetime.now().timestamp() - (retention_days * 86400)
            for log_file in data_dir.glob("session_*.log"):
                try:
                    if log_file.stat().st_mtime < cutoff:
                        log_file.unlink()
                except Exception:
                    pass
        except Exception as e:
            print(f"[LOGGER] Log cleanup failed: {e}")

    def _clear_line(self):
        """Clear the current line"""
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()
    
    def _restore_prompt(self):
        """Restore the > prompt after logging"""
        with self.prompt_lock:
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
        version_file = Path(__file__).parent / "_version.py"
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
    """Dynamically load all modules from the modules/ subdirectory"""
    modules_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules")
    os.makedirs(modules_dir, exist_ok=True)

    # Add modules/ to sys.path so modules can import each other
    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)

    loaded_count = 0
    failed_count = 0
    
    # Initialize command tracking
    if not hasattr(bot, '_module_commands'):
        bot._module_commands = {}
    
    # NEVER clear commands here - only do it once at startup if needed
    # Commands persist across reloads
    
    # Discover and load all Python modules in the modules/ directory
    for file in os.listdir(modules_dir):
        if not file.endswith('.py') or file.startswith('_'):
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

def _parse_version_tuple(v: str):
    """Parse '4.2.1.1' into (4,2,1,1). Returns (0,0,0,0) on failure."""
    try:
        parts = v.strip().split('.')
        return tuple(int(p) for p in parts[:4])
    except Exception:
        return (0, 0, 0, 0)

def _ensure_git_for_update(bot, logger) -> bool:
    """Ensure git is installed and the working directory is a proper git repo with a remote."""
    try:
        result = subprocess.run(
            ['git', '--version'], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            logger.log("AUTO-UPDATE", "Git is not installed — auto-update disabled", "WARNING")
            return False
        result = subprocess.run(
            ['git', '-C', str(script_dir), 'rev-parse', '--git-dir'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            remote_url = bot.config.get("network", {}).get("auto_update_git_remote", "")
            if not remote_url:
                logger.log("AUTO-UPDATE",
                    "Not a git repo and no auto_update_git_remote — auto-update disabled", "WARNING")
                return False
            logger.log("AUTO-UPDATE", "Initialising git repository for auto-update...")
            r1 = subprocess.run(['git', '-C', str(script_dir), 'init'],
                          capture_output=True, text=True, timeout=10)
            r2 = subprocess.run(['git', '-C', str(script_dir), 'remote', 'add', 'origin', remote_url],
                          capture_output=True, text=True, timeout=10)
            if r1.returncode != 0 or r2.returncode != 0:
                logger.log("AUTO-UPDATE",
                    f"Git init/remote failed (init={r1.returncode}, remote={r2.returncode})", "WARNING")
                return False
            logger.log("AUTO-UPDATE", f"Git repo initialised with remote: {remote_url}")
        else:
            result = subprocess.run(
                ['git', '-C', str(script_dir), 'remote', 'get-url', 'origin'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                remote_url = bot.config.get("network", {}).get("auto_update_git_remote", "")
                if remote_url:
                    logger.log("AUTO-UPDATE", f"Adding remote origin: {remote_url}")
                    subprocess.run(
                        ['git', '-C', str(script_dir), 'remote', 'add', 'origin', remote_url],
                        capture_output=True, text=True, timeout=10
                    )
                else:
                    logger.log("AUTO-UPDATE",
                        "No remote origin and no auto_update_git_remote — auto-update disabled", "WARNING")
                    return False
        return True
    except Exception as e:
        logger.log("AUTO-UPDATE", f"Git pre-flight check failed: {e}", "WARNING")
        return False

async def _check_for_update(bot) -> bool:
    """One-shot: fetch remote, compare _version.py versions, pull if newer.
    Returns True if a pull happened (bot should restart)."""
    git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
    local_ver = _parse_version_tuple(bot.version if hasattr(bot, 'version') else load_version())
    bot.logger.log("AUTO-UPDATE", f"Pre-flight: local v{'.'.join(map(str,local_ver))}")
    proc = await asyncio.create_subprocess_exec(
        'git', '-C', str(script_dir), '-c', 'credential.helper=',
        'fetch', 'origin', 'main',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=git_env
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        bot.logger.log("AUTO-UPDATE", f"Fetch failed (rc={proc.returncode}): {stderr.decode()[:200]}", "WARNING")
        return False
    proc = await asyncio.create_subprocess_exec(
        'git', '-C', str(script_dir), '-c', 'credential.helper=',
        'show', 'FETCH_HEAD:_version.py',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=git_env
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        bot.logger.log("AUTO-UPDATE",
            f"Could not read remote _version.py (rc={proc.returncode}): {stderr.decode()[:200]}", "WARNING")
        return False
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', stdout.decode())
    if not match:
        bot.logger.log("AUTO-UPDATE", "Remote _version.py has no version string", "WARNING")
        return False
    remote_ver = _parse_version_tuple(match.group(1))
    bot.logger.log("AUTO-UPDATE", f"Remote v{match.group(1)}")
    if remote_ver <= local_ver:
        if remote_ver < local_ver:
            bot.logger.log("AUTO-UPDATE", "Remote is older — skipping")
        return False
    bot.logger.log("AUTO-UPDATE", "Remote is newer — fast-forwarding...")
    proc = await asyncio.create_subprocess_exec(
        'git', '-C', str(script_dir), '-c', 'credential.helper=',
        'merge', '--ff-only', 'origin/main',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=git_env
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        bot.logger.log("AUTO-UPDATE", "Fast-forwarded — restart required")
        return True
    bot.logger.log("AUTO-UPDATE", f"Merge failed: {stderr.decode()[:200]}", "WARNING")
    return False

async def _auto_update_loop(bot):
    interval = bot.config.get("network", {}).get("auto_update_interval_minutes", 5) * 60
    if not bot.config.get("network", {}).get("auto_update", True):
        bot.logger.log("AUTO-UPDATE", "Disabled by config")
        return
    await bot.wait_until_ready()
    if not _ensure_git_for_update(bot, bot.logger):
        return
    await asyncio.sleep(interval)
    while not bot.is_closed():
        try:
            if await _check_for_update(bot):
                bot.logger.log("AUTO-UPDATE", "Update pulled — restarting")
                await bot.close()
                os._exit(42)
        except Exception as e:
            bot.logger.log("AUTO-UPDATE", f"Check error: {e}", "WARNING")
        await asyncio.sleep(interval)

@bot.event
async def on_ready():
    # Only run initialization on first ready, not on reconnects
    if not hasattr(bot, 'initialized'):
        bot.initialized = True
        
        # Load version from file
        bot.version = load_version()
        
        mode = "DEVELOPMENT MODE"if args.development else "PRODUCTION MODE"
        bot.logger.log("MAIN", f"Embot online as {bot.user} - {mode} - v{bot.version}")
        
        # Start console IMMEDIATELY - don't wait for anything else
        start_console_thread()
        
        # Start a background task to monitor heartbeat
        bot.heartbeat_monitor = bot.loop.create_task(monitor_heartbeat())
        
        # Start auto-update loop (git-based, works without --dev)
        bot.auto_update_task = bot.loop.create_task(_auto_update_loop(bot))
        
        # Load modules
        load_modules()
        
        # Wait a moment for modules to initialize
        await asyncio.sleep(2)
        
        # ONLY sync commands ONCE on initial startup
        try:
            synced = await bot.tree.sync()
            bot.logger.log("MAIN", f"Commands synced successfully: {len(synced)} commands")
            
            if getattr(args, 'test', False):
                bot.logger.log("MAIN", f"TEST PASSED — {len(synced)} commands synced. Shutting down.")
                await bot.close()
                os._exit(0)
        except HTTPException as e:
            if e.status == 429 and e.code == 30034:
                bot.logger.log("MAIN", "Daily command sync limit reached (200/200). Commands will sync tomorrow.", "WARNING")
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
            threshold = bot.config.get("latency_warning_threshold", 1.0)
            if latency > threshold:
                bot.logger.log("MAIN", f"High latency detected: {latency:.2f}s", "WARNING")
            
            interval = bot.config.get("heartbeat_interval_seconds", 60)
            await asyncio.sleep(interval)
        except Exception as e:
            bot.logger.error("MAIN", "Heartbeat monitor error", e)
            interval = bot.config.get("heartbeat_interval_seconds", 60)
            await asyncio.sleep(interval)

def run_console():
    """Run interactive console for commands - available in all modes"""
    try:
        asyncio.run_coroutine_threadsafe(bot.wait_until_ready(), bot.loop).result(timeout=30)
    except Exception:
        pass
    bot.logger.log("CONSOLE", "Console ready. Type 'help' for commands.")
    
    # Activate prompt
    with bot.logger.prompt_lock:
        bot.logger.prompt_active = True
    
    while True:
        try:
            # Use input with prompt
            cmd = input("> ").strip()
            
            # Clear prompt temporarily during command execution
            with bot.logger.prompt_lock:
                bot.logger.prompt_active = False
            
            if not cmd:
                with bot.logger.prompt_lock:
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
            elif command == "exit"or command == "quit":
                print("Shutting down bot...")
                asyncio.run_coroutine_threadsafe(bot.close(), bot.loop)
                break
            else:
                print(f"Unknown command: {command}. Type 'help' for available commands.")
            
            # Reactivate prompt
            with bot.logger.prompt_lock:
                bot.logger.prompt_active = True
                
        except KeyboardInterrupt:
            print("\nUse 'exit' to shutdown gracefully.")
            with bot.logger.prompt_lock:
                bot.logger.prompt_active = True
        except Exception as e:
            print(f"Error: {e}")
            with bot.logger.prompt_lock:
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
            print(f"│   ... and {len(bot.guilds) - 3} more{'' * (40 - len(str(len(bot.guilds) - 3)))}│")
    
    # Latency
    latency = getattr(bot, 'latency', 0) * 1000
    print(f"│ Latency: {latency:.0f}ms{' ' * (48 - len(f'{latency:.0f}ms'))} │")
    
    # Module info - dynamic discovery (snapshot to avoid race with load_modules())
    bot_modules = []
    for name, module in list(sys.modules.items()):
        if (hasattr(module, '__file__') and 
            module.__file__ and 
            'site-packages' not in module.__file__ and
            os.path.dirname(os.path.abspath(__file__)) in module.__file__ and
            name not in ['main', '__main__']):
            bot_modules.append(name)
    
    print(f"│ Loaded modules: {len(bot_modules)}{'' * (45 - len(str(len(bot_modules))))} │")
    
    # Development mode
    mode = "DEVELOPMENT"if args.development else "PRODUCTION"
    print(f"│ Mode: {mode:<47} │")
    
    # Console commands info
    print(f"│ Console commands: {len(bot.console_commands)}{'' * (42 - len(str(len(bot.console_commands))))} │")
    
    # Log file info
    if hasattr(bot.logger, 'log_file'):
        log_file_name = bot.logger.log_file.name
        print(f"│ Log file: {log_file_name:<46} │")
    
    print("└─────────────────────────────────────────────────────────────────────┘\n")

def show_version():
    """Show version information"""
    version = bot.version if hasattr(bot, 'version') else "0.0.0.0"
    print(f"\n Embot v{version}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Discord.py: {discord.__version__}")
    print()

# Register core console commands
def setup_console_commands():
    """Setup core console commands available in all modes"""
    async def handle_reload(args):
        """Reload modules command"""
        if not args.strip():
            print("Usage: reload <module_name>")
            return
        
        module_name = args.strip()
        file_path = Path(__file__).parent / "modules"/ f"{module_name}.py"
        if not file_path.exists():
            print(f"Module '{module_name}.py' not found in modules/")
            return
        
        print(f"Reloading {module_name}...")
        try:
            # First, get list of existing commands from this module
            existing_commands = {}
            if hasattr(bot, '_module_commands'):
                existing_commands = bot._module_commands.get(module_name, {})
            else:
                bot._module_commands = {}
            
            # Remove existing commands from tree to prevent "already registered"errors
            removed_count = 0
            if existing_commands:
                for cmd_name in list(existing_commands.keys()):
                    try:
                        bot.tree.remove_command(cmd_name)
                        removed_count += 1
                    except:
                        pass
                print(f" Removed {removed_count} existing command(s)")
            
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
                    print(f" Registered {len(new_commands)} command(s): {', '.join(new_commands.keys())}")
                
                print(f"Module '{module_name}' reloaded successfully (commands updated, no sync needed)")
            else:
                print(f"Module '{module_name}' reloaded (no setup function)")
                
        except Exception as e:
            print(f"Failed to reload {module_name}: {e}")
            import traceback
            traceback.print_exc()
    
    async def handle_modules(args):
        """List loaded modules command"""
        bot_modules = []
        _modules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules")
        for name, module in list(sys.modules.items()):
            if (hasattr(module, '__file__') and 
                module.__file__ and 
                'site-packages' not in module.__file__ and
                _modules_path in module.__file__ and
                name not in ['main', '__main__', 'Embot']):
                bot_modules.append(name)
        
        print(f"\n Loaded Modules ({len(bot_modules)}):")
        for module in sorted(bot_modules):
            print(f" • {module}")
        print()
    
    async def handle_logs(args):
        """Show log file information"""
        if hasattr(bot.logger, 'log_file') and bot.logger.log_file.exists():
            file_size = bot.logger.log_file.stat().st_size
            print(f"\n Current log file: {bot.logger.log_file.name}")
            print(f"  Size: {file_size:,} bytes")
            print(f"  Location: {bot.logger.log_file}")
            
            # List other log files in data directory
            log_files = list(data_dir.glob("session_*.log"))
            if len(log_files) > 1:
                print(f"\n Other log files in {data_dir}:")
                for log_file in sorted(log_files, key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                    if log_file != bot.logger.log_file:
                        size = log_file.stat().st_size
                        mtime = datetime.fromtimestamp(log_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                        print(f"  • {log_file.name} ({size:,} bytes, {mtime})")
        else:
            print("No log file found")
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
    
    # Handle rate limits — do NOT reinvoke; that causes an infinite retry loop
    if isinstance(error, HTTPException) and error.status == 429:
        retry_after = getattr(error, 'retry_after', 0)
        bot.logger.log("MAIN", f"Rate limited on command {ctx.command}. Retry after: {retry_after}s", "WARNING")
        try:
            await ctx.send(f"⏳ I'm being rate limited. Please try again in {retry_after:.1f}s.")
        except Exception:
            pass
        return

    if isinstance(error, commands.MissingPermissions):
        perms = ", ".join(error.missing_permissions)
        try:
            await ctx.send(f"You need the following permissions: {perms}", delete_after=10)
        except Exception:
            pass
        return

    if isinstance(error, commands.MissingRequiredArgument):
        try:
            await ctx.send(f"Missing required argument: `{error.param.name}`", delete_after=10)
        except Exception:
            pass
        return

    if isinstance(error, commands.BadArgument):
        try:
            await ctx.send(f"Bad argument: {error}", delete_after=10)
        except Exception:
            pass
        return

    if isinstance(error, commands.CommandOnCooldown):
        try:
            await ctx.send(f"Command on cooldown. Try again in {error.retry_after:.1f}s.", delete_after=10)
        except Exception:
            pass
        return

    bot.logger.error("MAIN", f"Command error in {ctx.command}", error)
    await ctx.send(f"An error occurred: {str(error)}")

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
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.create_task(shutdown_bot(signame))

def _register_events():
    @bot.event
    async def on_guild_join(guild: discord.Guild):
        if guild.id != bot.home_guild_id:
            bot.logger.log("MAIN", f"Joined unauthorized guild '{guild.name}' ({guild.id}) — leaving immediately.", "WARNING")
            try:
                await guild.leave()
                bot.logger.log("MAIN", f"Successfully left unauthorized guild {guild.id}.")
            except Exception as e:
                bot.logger.error("MAIN", f"Failed to leave unauthorized guild {guild.id}", e)

def run_bot(token):
    """Start the bot with the given token. Callable from external launchers."""
    global bot, _cfg, args, data_dir, script_dir

    bot.config = _cfg

    home_guild_id = int(_cfg.get("home_guild_id") or 0)
    if not home_guild_id:
        bot.logger.error("MAIN", "home_guild_id is not set in config/embot.json!")
        bot.logger.log("MAIN", f"Edit {_CONFIG_PATH} and set home_guild_id to your server's ID.")
        sys.exit(1)

    bot.home_guild_id = home_guild_id
    _register_events()

    try:
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        setup_console_commands()

        # Auto-update check runs before Discord connection (git-only, no rate limits)
        if bot.config.get("network", {}).get("auto_update", True):
            if _ensure_git_for_update(bot, bot.logger):
                    if asyncio.run(_check_for_update(bot)):
                        bot.logger.log("MAIN", "Update pulled during pre-flight — restarting")
                        os._exit(42)

        if args.development:
            bot.logger.log("MAIN", "Pre-flight: running dev version check...")
            from modules.dev import DevManager
            dm = DevManager(bot)
            bot.dev_manager = dm
            result = asyncio.run(dm.check_and_update_version(auto_commit=True))
            bot.version = dm._get_version_from_file()
            if result:
                bot.logger.log("MAIN", f"Pre-flight bumped to v{result['version']}")
            else:
                bot.logger.log("MAIN", f"Pre-flight complete — v{bot.version} (no change)")

        if args.development:
            mode_str = "DEVELOPMENT MODE"
        elif args.test:
            mode_str = "DRY-RUN TEST MODE"
        else:
            mode_str = "PRODUCTION MODE"
        bot.logger.log("MAIN", f"Starting Embot v{load_version()} — {mode_str}")
        bot.logger.log("MAIN", f"Log files: {data_dir}")

        bot.run(token)
    except Exception as e:
        bot.logger.error("MAIN", "Failed to start bot", e)
        sys.exit(1)

if __name__ == "__main__":
    TOKEN_FILE = script_dir / "config" / "auth.json"
    TOKEN = ""
    if TOKEN_FILE.exists():
        try:
            import json
            with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            TOKEN = data.get("bot_token", "")
        except Exception:
            TOKEN = TOKEN_FILE.read_text().strip()

    if not TOKEN:
        bot.logger.error("MAIN", f"Auth file not found or empty: {TOKEN_FILE}")
        bot.logger.log("MAIN", "Create config/auth.json with your Discord bot token.")
        sys.exit(1)

    run_bot(TOKEN)