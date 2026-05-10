import asyncio
import discord
from discord.ext import commands
from discord import HTTPException, app_commands
import os
import sys
import traceback
from datetime import datetime, timezone
import importlib
import argparse
import threading
import subprocess
from pathlib import Path
import json
import re
import signal

parser = argparse.ArgumentParser(description='Embot Discord Bot')
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
        return f"session_{datetime.now(timezone.utc).strftime('%Y%m%d')}"

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
        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        with open(self.log_file, 'a', encoding='utf-8') as f:
            if not file_exists:
                f.write(f"=== Embot Log — {datetime.now(timezone.utc).strftime('%Y-%m-%d')} ===\n\n")
            f.write(f"--- Session {self.session_number} — {now_str} UTC ---\n\n")

        print(f"[LOGGER] Day log: {self.log_file.name} (session #{self.session_number})")

    def _cleanup_old_logs(self, retention_days=30):
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - (retention_days * 86400)
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
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            log_message = f"[{timestamp}] [{module_name}] [{level}] {message}"

            self._clear_line()
            print(log_message)
            self._restore_prompt()

            self._write_to_file(log_message)

    def error(self, module_name: str, message: str, exception: Exception = None):
        with self.lock:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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

    # Load order is explicit — mod_core must precede mod_logger so that
    # bot._pending_rehosted_media is populated before mod_logger's on_message_delete fires.
    _MODULE_ORDER = [
        "messages",
        "mod_core",
        "mod_suspicion",
        "mod_actions",
        "mod_appeals",
        "mod_oversight",
        "mod_rules",
        "mod_logger",
        "vms_core",
        "vms_transcribe",
        "vms_storage",
        "vms_playback",
        "remote_debug",
        "music_archive",
        "music_player",
        "community",
        "starboard",
        "youtube",
        "links",
        "icons",
        "artwork",
        "magic_emball",
    ]
    # Any modules present on disk but not in _MODULE_ORDER are appended at the end.
    _known = set(_MODULE_ORDER)
    _extras = sorted(
        f[:-3] for f in os.listdir(modules_dir)
        if f.endswith('.py') and not f.startswith('_') and f[:-3] not in _known
    )
    _load_sequence = _MODULE_ORDER + _extras

    for name in _load_sequence:
        file = name + ".py"
        if not os.path.exists(os.path.join(modules_dir, file)):
            continue

        try:
            commands_before = {cmd.name: cmd for cmd in bot.tree.get_commands()}

            if name in sys.modules:
                module = importlib.reload(sys.modules[name])
            else:
                module = importlib.import_module(name)

            if hasattr(module, 'setup'):
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
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
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

def _is_guild_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == interaction.guild.owner_id

async def _deny_owner(interaction: discord.Interaction):
    await interaction.response.send_message("Server owner only.", ephemeral=True)

async def _send_inline_or_file(interaction, text: str, filename: str, label: str = None):
    # chunk into <=1900-char pieces, send as sequential ephemeral followups
    chunk_size = 1900
    lines = text.splitlines(keepends=True)
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) > chunk_size:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        header = f"`[{i+1}/{len(chunks)}]` " if len(chunks) > 1 else ""
        await interaction.followup.send(f"{header}```\n{chunk}\n```", ephemeral=True)

@bot.tree.command(name="status", description="[Server owner only] Bot status and vitals")
async def slash_status(interaction: discord.Interaction):
    if not _is_guild_owner(interaction):
        return await _deny_owner(interaction)
    uptime = 0
    server = getattr(bot, "remote_debug_server", None)
    if server and server._start_time:
        import time as _time
        uptime = int(_time.time() - server._start_time)
    hours, rem = divmod(uptime, 3600)
    mins, secs = divmod(rem, 60)
    lines = [
        f"**Embot v{getattr(bot, 'version', 'unknown')}**",
        f"User: `{bot.user}`",
        f"Latency: `{round(bot.latency * 1000, 1)}ms`",
        f"Uptime: `{hours}h {mins}m {secs}s`",
        f"Guilds: `{len(bot.guilds)}`",
    ]
    if hasattr(bot.logger, "log_file"):
        lines.append(f"Log: `{bot.logger.log_file.name}`")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
    bot.logger.log("MAIN", f"/status used by {interaction.user}")

@bot.tree.command(name="modules", description="[Server owner only] List loaded and failed modules")
async def slash_modules(interaction: discord.Interaction):
    if not _is_guild_owner(interaction):
        return await _deny_owner(interaction)
    data = get_modules_data()
    if data is None:
        return await interaction.response.send_message("No log file available.", ephemeral=True)
    loaded, failed = data.get("loaded", []), data.get("failed", [])
    lines = [f"**Loaded ({len(loaded)}):**"] + [f"`{m}`" for m in loaded]
    if failed:
        lines += [f"**Failed ({len(failed)}):**"] + [f"`{m}`" for m in failed]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
    bot.logger.log("MAIN", f"/modules used by {interaction.user}")

@bot.tree.command(name="logs", description="[Server owner only] View recent log lines")
@app_commands.describe(tail="Number of lines (default 200)", search="Regex search pattern")
async def slash_logs(interaction: discord.Interaction, tail: int = 200, search: str = None):
    if not _is_guild_owner(interaction):
        return await _deny_owner(interaction)
    await interaction.response.defer(ephemeral=True)
    try:
        data = get_logs_data(tail=tail, search=search)
    except re.error as e:
        return await interaction.followup.send(f"Invalid regex: {e}", ephemeral=True)
    if data.get("error"):
        return await interaction.followup.send(data["error"], ephemeral=True)
    if "matches" in data:
        matches = data["matches"]
        if not matches:
            return await interaction.followup.send("No matches.", ephemeral=True)
        text = "\n".join(f"{m['file']}:{m['line']}: {m['content']}" for m in matches)
        label = f"{len(matches)} match(es)" + (" (truncated)" if data["truncated"] else "")
    else:
        text = data.get("lines", "")
        label = f"{len(text.splitlines())} lines"
    await _send_inline_or_file(interaction, text, "logs.txt", label)
    bot.logger.log("MAIN", f"/logs (tail={tail} search={search}) used by {interaction.user}")

@bot.tree.command(name="config", description="[Server owner only] View a config file")
@app_commands.describe(name="Config name without .json (e.g. embot, mod, vms)")
async def slash_config(interaction: discord.Interaction, name: str):
    if not _is_guild_owner(interaction):
        return await _deny_owner(interaction)
    await interaction.response.defer(ephemeral=True)
    data, err = get_config_data(name)
    if err:
        return await interaction.followup.send(err, ephemeral=True)
    text = json.dumps(data, indent=2)
    await _send_inline_or_file(interaction, text, f"{name}.json", f"config/{name}.json")
    bot.logger.log("MAIN", f"/config {name} used by {interaction.user}")

@bot.tree.command(name="dbquery", description="[Server owner only] Run a read-only SQL query on a database")
@app_commands.describe(name="DB name without .db (e.g. mod, vms)", query="SELECT query")
async def slash_dbquery(interaction: discord.Interaction, name: str, query: str):
    if not _is_guild_owner(interaction):
        return await _deny_owner(interaction)
    await interaction.response.defer(ephemeral=True)
    rows, err = run_db_query(name, query)
    if err:
        return await interaction.followup.send(err, ephemeral=True)
    if not rows:
        return await interaction.followup.send("(empty result)", ephemeral=True)
    text = "\n".join(json.dumps(r, default=str) for r in rows) + f"\n({len(rows)} rows)"
    await _send_inline_or_file(interaction, text, "query.txt", f"{len(rows)} row(s)")
    bot.logger.log("MAIN", f"/dbquery {name} used by {interaction.user}")

@bot.tree.command(name="restart", description="[Owner role only] Restart the bot")
async def slash_restart(interaction: discord.Interaction):
    from modules.mod_core import is_owner
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner role only.", ephemeral=True)
    await interaction.response.send_message("Restarting...", ephemeral=True)
    bot.logger.log("MAIN", f"/restart used by {interaction.user}")
    await _restart_async(bot)

@bot.event
async def on_ready():
    if not hasattr(bot, 'initialized'):
        bot.initialized = True

        bot.version = load_version()

        mode = "PRODUCTION MODE"
        bot.logger.log("MAIN", f"Embot online as {bot.user} - {mode} - v{bot.version}")

        start_console_thread()

        bot.heartbeat_monitor = bot.loop.create_task(monitor_heartbeat())

        bot.auto_update_task = bot.loop.create_task(_auto_update_loop(bot))

        load_modules()

        await asyncio.sleep(2)

        try:
            synced = await bot.tree.sync()
            bot.logger.log("MAIN", f"Commands synced successfully: {len(synced)} commands")

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
                timeout = 90 if command == "exec" else 30
                asyncio.run_coroutine_threadsafe(
                    cmd_info['handler'](args_str),
                    bot.loop
                ).result(timeout=timeout)
            elif command == "exit" or command == "quit":
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
    print("\nCommands:")
    print("  help                   show this help")
    print("  status                 bot status")
    print("  version                current version")
    for name, info in sorted(bot.console_commands.items()):
        print(f"  {name:<22} {info['description']}")
    print("  exit / quit            shutdown gracefully\n")

def show_status():
    ver = bot.version if hasattr(bot, 'version') else "0.0.0.0"
    latency = getattr(bot, 'latency', 0) * 1000
    print(f"\nEmbot v{ver}  |  {latency:.0f}ms latency")
    if bot.user:
        print(f"User:     {bot.user} ({bot.user.id})")
    for g in bot.guilds:
        print(f"Guild:    {g.name} ({g.member_count} members)")
    print(f"Modules:  {len(getattr(bot, '_module_commands', {}))}")
    if hasattr(bot.logger, 'log_file'):
        print(f"Log:      {bot.logger.log_file.name}")
    print()

def show_version():
    version = bot.version if hasattr(bot, 'version') else "0.0.0.0"
    print(f"\n Embot v{version}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Discord.py: {discord.__version__}")
    print()

# --- Shared logic called by both console and remote_debug HTTP handlers ---

def get_modules_data():
    """Returns {'loaded': [...], 'failed': [...]} from the current log file."""
    if not hasattr(bot.logger, 'log_file') or not bot.logger.log_file.exists():
        return None
    loaded, failed = [], []
    with open(bot.logger.log_file, 'r', encoding='utf-8') as f:
        for line in f:
            m = re.search(r'Loaded module: (\S+)', line)
            if m and m.group(1) not in loaded:
                loaded.append(m.group(1))
            m = re.search(r'Failed to load module: (\S+)', line)
            if m and m.group(1) not in loaded and m.group(1) not in failed:
                failed.append(m.group(1))
    return {'loaded': loaded, 'failed': failed}

_SENSITIVE_KEYS = {'token', 'secret', 'password', 'api_key', 'webhook', 'client_secret'}

def _redact(obj):
    """Recursively redact sensitive keys from a config dict."""
    if isinstance(obj, dict):
        return {k: ('[REDACTED]' if k.lower() in _SENSITIVE_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(i) for i in obj]
    return obj

def get_config_data(name):
    """Returns (data, error) for a config file. Sensitive keys are redacted."""
    cfg_path = script_dir / 'config' / f'{name}.json'
    if not cfg_path.exists():
        return None, f"config '{name}.json' not found"
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            return _redact(json.load(f)), None
    except Exception as e:
        return None, str(e)

def get_logs_data(*, tail=None, file=None, session=None, list_files=False, search=None, search_max=200):
    """Unified log access. Returns a dict with one of: 'files', 'matches', 'lines', or 'error'."""
    if list_files:
        if not data_dir.exists():
            return {'files': []}
        files = []
        for fp in sorted(data_dir.glob('session_*.log'), key=lambda x: x.stat().st_mtime, reverse=True):
            sc = 0
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    sc = sum(1 for l in f if l.startswith('--- Session'))
            except Exception:
                pass
            files.append({'name': fp.name, 'size': fp.stat().st_size, 'sessions': sc})
        return {'files': files}

    if search is not None:
        pattern = re.compile(search, re.IGNORECASE)
        limit = tail if tail else search_max
        matches = []
        for fp in sorted(data_dir.glob('session_*.log'), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            matches.append({'file': fp.name, 'line': i, 'content': line.rstrip()})
                            if len(matches) >= limit:
                                return {'matches': matches, 'truncated': True, 'query': search}
            except Exception:
                continue
        return {'matches': matches, 'truncated': False, 'query': search}

    if file:
        if not re.match(r'^session_\d{8}(?:_\d{6})?\.log$', file):
            return {'error': 'invalid log name'}
        log_path = data_dir / file
    else:
        if not hasattr(bot.logger, 'log_file') or not bot.logger.log_file.exists():
            return {'error': 'no log file'}
        log_path = bot.logger.log_file

    if not log_path.exists():
        return {'error': 'log file not found'}

    with open(log_path, 'r', encoding='utf-8') as f:
        all_lines = f.readlines()

    n = tail or 200
    if session and session != 'all':
        try:
            snum = int(session)
        except ValueError:
            return {'error': "session must be a number or 'all'"}
        start, end = None, None
        hre = re.compile(r'^--- Session (\d+) ')
        for i, line in enumerate(all_lines):
            m = hre.match(line)
            if m:
                if int(m.group(1)) == snum:
                    start = i
                elif start is not None:
                    end = i
                    break
        if start is None:
            return {'error': f'session {snum} not found'}
        out = all_lines[start:end]
        if tail and tail < len(out):
            out = out[-tail:]
    else:
        out = all_lines if session == 'all' else all_lines[-n:]

    return {'lines': ''.join(out)}


def run_db_query(db_name, sql):
    """Returns (rows, error). rows is list of dicts. Raises nothing."""
    import sqlite3 as _sqlite3
    if not re.match(r'^[a-zA-Z0-9_\-]+$', db_name):
        return None, 'invalid db name'
    db_path = script_dir / 'db' / f'{db_name}.db'
    if not db_path.exists():
        return None, f"db '{db_name}' not found"
    q = sql.strip().upper()
    if not (q.startswith('SELECT') or q.startswith('PRAGMA') or q.startswith('EXPLAIN')):
        return None, 'only SELECT / PRAGMA / EXPLAIN allowed'
    try:
        conn = _sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.row_factory = _sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows, None
    except Exception as e:
        return None, str(e)

async def run_exec(cmd, timeout=60):
    """Returns (stdout, stderr, exit_code) or raises TimeoutError."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(script_dir),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode(errors='replace'), stderr.decode(errors='replace'), proc.returncode

# --- Console command setup ---

def setup_console_commands():
    async def handle_modules(args):
        data = get_modules_data()
        if data is None:
            print("No log file available.")
            return
        print(f"\nLoaded ({len(data['loaded'])}):")
        for mod in data['loaded']:
            print(f"  + {mod}")
        if data['failed']:
            print(f"Failed ({len(data['failed'])}):")
            for mod in data['failed']:
                print(f"  - {mod}")
        print()

    async def handle_update(args):
        print("Checking for updates...")
        if not _ensure_git_for_update(bot, bot.logger):
            print("Git not available.")
            return
        updated = await _check_for_update(bot)
        if updated:
            print("Update pulled. Restarting...")
            await _restart_async(bot)
        else:
            print("Already up to date.")

    async def handle_restart(args):
        print("Restarting...")
        await asyncio.sleep(0.5)
        await bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    async def handle_exec(args):
        if not args.strip():
            print("Usage: exec <command>")
            return
        try:
            out, err, code = await run_exec(args)
            if out.strip(): print(out.strip())
            if err.strip(): print(err.strip())
            if not out.strip() and not err.strip(): print(f"(exit {code})")
        except asyncio.TimeoutError:
            print("Timed out after 60s.")
        except Exception as e:
            print(f"Error: {e}")

    async def handle_config(args):
        name = args.strip()
        if not name:
            print("Usage: config <name>")
            return
        data, err = get_config_data(name)
        if err:
            print(err)
        else:
            print(json.dumps(data, indent=2))

    async def handle_logs(args):
        import shlex
        kwargs = {'tail': None, 'file': None, 'session': None, 'list_files': False, 'search': None}
        try:
            parts = shlex.split(args)
        except ValueError:
            parts = args.split()
        i = 0
        while i < len(parts):
            p = parts[i]
            if p == '--list':
                kwargs['list_files'] = True
            elif p == '--tail' and i + 1 < len(parts):
                try:
                    kwargs['tail'] = int(parts[i + 1])
                except ValueError:
                    print(f"Invalid --tail value: {parts[i+1]}")
                    return
                i += 1
            elif p == '--file' and i + 1 < len(parts):
                kwargs['file'] = parts[i + 1]; i += 1
            elif p == '--session' and i + 1 < len(parts):
                kwargs['session'] = parts[i + 1]; i += 1
            elif p == '--search' and i + 1 < len(parts):
                kwargs['search'] = ' '.join(parts[i + 1:]); break
            else:
                print(f"Usage: logs [--tail N] [--file NAME] [--session ID] [--list] [--search PATTERN]")
                return
            i += 1
        try:
            data = get_logs_data(**kwargs)
        except re.error as e:
            print(f"Invalid regex: {e}"); return
        if data.get('error'):
            print(data['error']); return
        if 'files' in data:
            for f in data['files']:
                print(f"  {f['name']}  {f['size']:,}b  {f['sessions']} sessions")
            if not data['files']:
                print("No log files.")
        elif 'matches' in data:
            for m in data['matches']:
                print(f"{m['file']}:{m['line']}: {m['content']}")
            if data['truncated']:
                print(f"(limit {len(data['matches'])} reached)")
            if not data['matches']:
                print("No matches.")
        elif 'lines' in data:
            print(data['lines'].rstrip())

    async def handle_db_query(args):
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            print("Usage: db-query <name> <sql>"); return
        rows, err = run_db_query(parts[0], parts[1])
        if err:
            print(err); return
        if not rows:
            print("(empty)")
        else:
            for row in rows:
                print(json.dumps(row, default=str))
            print(f"({len(rows)} rows)")

    register_console_command("modules",  "List loaded/failed bot modules",                    handle_modules)
    register_console_command("update",   "Git pull + restart if updated",                     handle_update)
    register_console_command("restart",  "Restart the bot",                                   handle_restart)
    register_console_command("exec",     "Run a shell command",                               handle_exec)
    register_console_command("config",   "View a config file",                                handle_config)
    register_console_command("logs",     "View/search logs. See: logs --help",               handle_logs)
    register_console_command("db-query", "db-query <name> <sql>  Read-only DB query",        handle_db_query)

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

        bot.logger.log("MAIN", f"Starting Embot v{load_version()} — PRODUCTION MODE")
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
