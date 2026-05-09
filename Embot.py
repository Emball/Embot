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

parser = argparse.ArgumentParser(description='Embot Discord Bot')
parser.add_argument('-dev', '--development', action='store_true',
                    help='Enable development mode (versioning, git integration)')
parser.add_argument('-t', '--test', action='store_true',
                    help='Dry-run: validate startup, sync commands, then exit')
args = parser.parse_args()

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
    cfg = dict(_CONFIG_DEFAULTS)
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            for key, val in user_cfg.items():
                if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                    cfg[key] = {**cfg[key], **val}
                else:
                    cfg[key] = val
        except Exception as e:
            print(f"[MAIN] [WARNING] Failed to read {_CONFIG_PATH}, using defaults: {e}")
    else:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_CONFIG_DEFAULTS, f, indent=4)
        print(f"[MAIN] [INFO] Created default config at {_CONFIG_PATH}")
    return cfg

_cfg = load_config()

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix=_cfg["command_prefixes"], intents=intents)

class ConsoleLogger:

    def __init__(self):
        self.prompt_active = False
        self.prompt_lock = threading.Lock()
        self.lock = threading.Lock()
        self.session_id = self._generate_session_id()
        self.log_file = None
        self.session_number = 0
        self._init_log_file()
        self._cleanup_old_logs(retention_days=30)

    def _generate_session_id(self) -> str:
        return f"session_{datetime.now().strftime('%Y%m%d')}"

    def _count_sessions(self, filepath):
        if not filepath.exists():
            return 0
        count = 0
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('--- Session'):
                        count += 1
        except Exception:
            pass
        return count

    def _init_log_file(self):
        log_filename = f"{self.session_id}.log"
        self.log_file = data_dir / log_filename
        file_exists = self.log_file.exists()
        self.session_number = self._count_sessions(self.log_file) + 1
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with open(self.log_file, 'a', encoding='utf-8') as f:
            if not file_exists:
                f.write(f"=== Embot Log — {datetime.now().strftime('%Y-%m-%d')} ===\n\n")
            f.write(f"--- Session {self.session_number} — {now_str} UTC ---\n\n")

        print(f"[LOGGER] Day log: {self.log_file.name} (session #{self.session_number})")

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
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()

    def _restore_prompt(self):
        with self.prompt_lock:
            if self.prompt_active:
                sys.stdout.write('> ')
                sys.stdout.flush()

    def _write_to_file(self, message: str):
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(message + '\n')
        except Exception as e:
            sys.stderr.write(f"[LOGGER ERROR] Failed to write to log file: {e}\n")

    def log(self, module_name: str, message: str, level: str = "INFO"):
        with self.lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_message = f"[{timestamp}] [{module_name}] [{level}] {message}"

            self._clear_line()
            print(log_message)
            self._restore_prompt()

            self._write_to_file(log_message)

    def error(self, module_name: str, message: str, exception: Exception = None):
        with self.lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_message = f"[{timestamp}] [{module_name}] [ERROR] {message}"

            self._clear_line()
            print(log_message)

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

            self._write_to_file(file_message)

bot.logger = ConsoleLogger()

bot.console_commands = {}

def register_console_command(name, description, handler):
    bot.console_commands[name] = {
        'description': description,
        'handler': handler
    }
    bot.logger.log("CONSOLE", f"Registered console command: {name}")

def load_version():
    try:
        version_file = Path(__file__).parent / "_version.py"
        if version_file.exists():
            with open(version_file, 'r', encoding='utf-8') as f:
                content = f.read()

            match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                return match.group(1)

        return "0.0.0.0"

    except Exception as e:
        bot.logger.error("MAIN", "Failed to load version from _version.py", e)
        return "0.0.0.0"

def load_modules():
    modules_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules")
    os.makedirs(modules_dir, exist_ok=True)

    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)

    loaded_count = 0
    failed_count = 0

    if not hasattr(bot, '_module_commands'):
        bot._module_commands = {}

    for file in os.listdir(modules_dir):
        if not file.endswith('.py') or file.startswith('_'):
            continue

        if file == 'dev.py' and not args.development:
            bot.logger.log("MAIN", "Skipping dev.py (not in development mode)")
            continue

        if file == 'version.py' and args.development:
            bot.logger.log("MAIN", "Skipping version.py (using dev.py instead)")
            continue

        name = file[:-3]

        try:
            commands_before = {cmd.name: cmd for cmd in bot.tree.get_commands()}

            if name in sys.modules:
                module = importlib.reload(sys.modules[name])
            else:
                module = importlib.import_module(name)

            if hasattr(module, 'setup'):
                if name == 'dev':
                    module.setup(bot, register_console_command)
                else:
                    module.setup(bot)

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
    console_thread = threading.Thread(target=run_console, daemon=True)
    console_thread.start()
    bot.logger.log("MAIN", "Console thread started")

def _restart():
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def _restart_async(bot):
    await bot.close()
    _restart()

def _parse_version_tuple(v: str):
    try:
        parts = v.strip().split('.')
        return tuple(int(p) for p in parts[:4])
    except Exception:
        return (0, 0, 0, 0)

def _ensure_git_for_update(bot, logger) -> bool:
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
    if remote_ver <= local_ver:
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
                bot.logger.log("AUTO-UPDATE", "Update pulled — restarting...")
                await _restart_async(bot)
        except Exception as e:
            bot.logger.log("AUTO-UPDATE", f"Check error: {e}", "WARNING")
        await asyncio.sleep(interval)

@bot.tree.command(name="update", description="[Owner only] Pull latest changes from git and restart")
async def update_cmd(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.send_message("Checking for updates...", ephemeral=True)
    if not _ensure_git_for_update(bot, bot.logger):
        await interaction.edit_original_response(content="Git not available.")
        return
    updated = await _check_for_update(bot)
    if updated:
        await interaction.edit_original_response(content="Update pulled. Restarting...")
        await _restart_async(bot)
    else:
        await interaction.edit_original_response(content="Already up to date.")

@bot.command(name="update")
async def prefix_update(ctx):
    if ctx.author.id != ctx.guild.owner_id:
        return await ctx.message.delete()
    msg = await ctx.send("Checking for updates...")
    if not _ensure_git_for_update(bot, bot.logger):
        await msg.edit(content="Git not available.")
        await msg.delete(delay=8)
        return
    updated = await _check_for_update(bot)
    if updated:
        await msg.edit(content="Update pulled. Restarting...")
        await _restart_async(bot)
    else:
        await msg.edit(content="Already up to date.")
        await msg.delete(delay=8)

@bot.event
async def on_ready():
    if not hasattr(bot, 'initialized'):
        bot.initialized = True

        bot.version = load_version()

        mode = "DEVELOPMENT MODE"if args.development else "PRODUCTION MODE"
        bot.logger.log("MAIN", f"Embot online as {bot.user} - {mode} - v{bot.version}")

        start_console_thread()

        bot.heartbeat_monitor = bot.loop.create_task(monitor_heartbeat())

        bot.auto_update_task = bot.loop.create_task(_auto_update_loop(bot))

        load_modules()

        await asyncio.sleep(2)

        try:
            synced = await bot.tree.sync()
            bot.logger.log("MAIN", f"Commands synced successfully: {len(synced)} commands")

            if getattr(args, 'test', False):
                bot.logger.log("MAIN", f"TEST PASSED — {len(synced)} commands synced. Shutting down.")
                await bot.close()
                return
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
        bot.logger.log("MAIN", "Reconnected successfully (commands not resynced to avoid rate limits)")

async def monitor_heartbeat():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
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
    try:
        asyncio.run_coroutine_threadsafe(bot.wait_until_ready(), bot.loop).result(timeout=30)
    except Exception:
        pass
    bot.logger.log("CONSOLE", "Console ready. Type 'help' for commands.")

    with bot.logger.prompt_lock:
        bot.logger.prompt_active = True

    while True:
        try:
            cmd = input("> ").strip()

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
                cmd_info = bot.console_commands[command]
                asyncio.run_coroutine_threadsafe(
                    cmd_info['handler'](args_str),
                    bot.loop
                ).result(timeout=30)
            elif command == "exit"or command == "quit":
                print("Shutting down bot...")
                asyncio.run_coroutine_threadsafe(bot.close(), bot.loop).result(timeout=15)
                break
            else:
                print(f"Unknown command: {command}. Type 'help' for available commands.")

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
    help_text = """
┌─────────────────────────────────────────────────────────────────────┐
│                         BOT CONSOLE                                 │
├─────────────────────────────────────────────────────────────────────┤
│ help              - Show this help message                          │
│ status            - Show bot status                                 │
│ version           - Show current version                            │"""

    for cmd_name, cmd_info in sorted(bot.console_commands.items()):
        desc = cmd_info['description'][:35] if len(cmd_info['description']) > 35 else cmd_info['description']
        help_text += f"\n│ {cmd_name:<17} - {desc:<35} │"

    help_text += """
│ exit / quit       - Shutdown bot gracefully                         │
└─────────────────────────────────────────────────────────────────────┘
"""
    print(help_text)

def show_status():
    print("\n┌─────────────────────────────────────────────────────────────────────┐")
    print(f"│ Bot Status - v{bot.version if hasattr(bot, 'version') else '0.0.0.0':<43} │")
    print("├─────────────────────────────────────────────────────────────────────┤")

    if bot.user:
        print(f"│ Logged in as: {bot.user.name:<45} │")
        print(f"│ User ID: {bot.user.id:<49} │")

    if bot.guilds:
        print(f"│ Servers: {len(bot.guilds):<50} │")
        for guild in list(bot.guilds)[:3]:
            guild_name = guild.name[:45] + "..." if len(guild.name) > 45 else guild.name
            print(f"│   • {guild_name:<47} │")
        if len(bot.guilds) > 3:
            print(f"│   ... and {len(bot.guilds) - 3} more{'' * (40 - len(str(len(bot.guilds) - 3)))}│")

    latency = getattr(bot, 'latency', 0) * 1000
    print(f"│ Latency: {latency:.0f}ms{' ' * (48 - len(f'{latency:.0f}ms'))} │")

    module_count = len(getattr(bot, '_module_commands', {}))
    print(f"│ Loaded modules: {module_count}{'' * (45 - len(str(module_count)))} │")

    mode = "DEVELOPMENT"if args.development else "PRODUCTION"
    print(f"│ Mode: {mode:<47} │")

    print(f"│ Console commands: {len(bot.console_commands)}{'' * (42 - len(str(len(bot.console_commands))))} │")

    if hasattr(bot.logger, 'log_file'):
        log_file_name = bot.logger.log_file.name
        print(f"│ Log file: {log_file_name:<46} │")

    print("└─────────────────────────────────────────────────────────────────────┘\n")

def show_version():
    version = bot.version if hasattr(bot, 'version') else "0.0.0.0"
    print(f"\n Embot v{version}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Discord.py: {discord.__version__}")
    print()

def setup_console_commands():
    async def handle_reload(args):
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
            existing_commands = {}
            if hasattr(bot, '_module_commands'):
                existing_commands = bot._module_commands.get(module_name, {})
            else:
                bot._module_commands = {}

            removed_count = 0
            if existing_commands:
                for cmd_name in list(existing_commands.keys()):
                    try:
                        bot.tree.remove_command(cmd_name)
                        removed_count += 1
                    except Exception:
                        pass
                print(f" Removed {removed_count} existing command(s)")

            if module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)

            if hasattr(module, 'setup'):
                commands_before = {cmd.name: cmd for cmd in bot.tree.get_commands()}

                if module_name == 'dev':
                    module.setup(bot, register_console_command)
                else:
                    module.setup(bot)

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
        if hasattr(bot.logger, 'log_file') and bot.logger.log_file.exists():
            file_size = bot.logger.log_file.stat().st_size
            session_count = bot.logger._count_sessions(bot.logger.log_file)
            print(f"\n Day log: {bot.logger.log_file.name}")
            print(f"  Sessions today: {session_count} (current: #{bot.logger.session_number})")
            print(f"  Size: {file_size:,} bytes")
            print(f"  Location: {bot.logger.log_file}")

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
    exc_type, exc_value, exc_traceback = sys.exc_info()

    if isinstance(exc_value, HTTPException) and exc_value.status == 429:
        retry_after = getattr(exc_value, 'retry_after', 0)
        bot.logger.log("MAIN", f"Rate limited on event {event}. Retry after: {retry_after}s", "WARNING")
        await asyncio.sleep(retry_after)
    else:
        bot.logger.error("MAIN", f"Error in event {event}", exc_value)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

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
    bot.logger.log("MAIN", f"Received signal {signame}, shutting down gracefully...")

    try:
        await bot.close()
        bot.logger.log("MAIN", "Bot closed successfully")
    except Exception as e:
        bot.logger.error("MAIN", "Error during shutdown", e)

def handle_signal(signum, frame):
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

        if bot.config.get("network", {}).get("auto_update", True):
            if _ensure_git_for_update(bot, bot.logger):
                    if asyncio.run(_check_for_update(bot)):
                        bot.logger.log("MAIN", "Update pulled — restarting...")
                        _restart()

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
