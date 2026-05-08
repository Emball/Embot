import os
import json
import subprocess
from pathlib import Path
from datetime import datetime
import discord
import re
import asyncio
import traceback

MODULE_NAME = "DEV"

DEV_CONFIG_PATH = Path(__file__).parent.parent / "config" / "dev.json"
DEV_CONFIG_DEFAULTS = {
    "breaking_threshold": 500,
    "major_threshold": 100,
    "minor_threshold": 20,
    "patch_threshold": 1,
    "auto_commit": True,
    "auto_versioning": True,
}

def _load_dev_config() -> dict:
    cfg = dict(DEV_CONFIG_DEFAULTS)
    if DEV_CONFIG_PATH.exists():
        try:
            with open(DEV_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    else:
        DEV_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEV_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEV_CONFIG_DEFAULTS, f, indent=4)
    return cfg

def _save_dev_config(data: dict) -> None:
    try:
        existing = {}
        if DEV_CONFIG_PATH.exists():
            with open(DEV_CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing.update(data)
        tmp = str(DEV_CONFIG_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=4, ensure_ascii=False)
        os.replace(tmp, str(DEV_CONFIG_PATH))
    except Exception as e:
        print(f"[DEV] Failed to save dev.json: {e}")

class DevManager:
    def __init__(self, bot):
        self.bot = bot
        self._config = _load_dev_config()
        self.version_history = []
        self.last_check_time = None
        self.git_enabled = False
        self.auto_commit_enabled = self._config.get("auto_commit", True)
        self.auto_versioning_enabled = self._config.get("auto_versioning", True)
        
        self._check_git()
        
        if self.git_enabled:
            self._auto_setup_git()
        
        self._ensure_gitignore()
        self._log_dev_features()
    
    def _auto_setup_git(self):
        root = Path(__file__).parent.parent
        token_candidates = [root / "config" / "token", root / "token.json"]
        token_path = next((p for p in token_candidates if p.exists()), None)
        if not token_path:
            return
        try:
            with open(token_path, 'r', encoding='utf-8') as f:
                creds = json.load(f)
            gh_token = creds.get("github_token", "")
            gh_email = creds.get("github_email", "")
            gh_name = creds.get("github_name", "")
            if not gh_token:
                return
            self.bot.logger.log(MODULE_NAME, "Auto-configuring git from config/token...")
            self._auto_configure_git(gh_token, gh_email, gh_name)
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Git auto-setup skipped: {e}", "WARNING")
    
    def _auto_configure_git(self, token, email, name):
        git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}

        if name:
            subprocess.run(['git', '-c', 'credential.helper=', 'config', 'user.name', name],
                           capture_output=True, text=True, timeout=5, env=git_env)
        if email:
            subprocess.run(['git', '-c', 'credential.helper=', 'config', 'user.email', email],
                           capture_output=True, text=True, timeout=5, env=git_env)

        subprocess.run(['git', '-c', 'credential.helper=', 'config', 'core.askPass', ''],
                       capture_output=True, text=True, timeout=5, env=git_env)

        # Embed token in remote URL — no credential helper interaction at all
        try:
            r = subprocess.run(['git', '-c', 'credential.helper=', 'remote', 'get-url', 'origin'],
                               capture_output=True, text=True, timeout=5, env=git_env)
            current_url = r.stdout.strip()
            if r.returncode == 0 and 'github.com' in current_url:
                parts = current_url.split('@')[-1] if '@' in current_url else current_url
                parts = parts.replace('https://', '').replace('http://', '')
                parts = parts.removesuffix('.git').split('/')
                if len(parts) >= 2:
                    owner, repo = parts[-2], parts[-1]
                else:
                    owner, repo = (name or 'Emball'), Path.cwd().name
            else:
                owner, repo = (name or 'Emball'), Path.cwd().name
        except Exception:
            owner, repo = (name or 'Emball'), Path.cwd().name

        auth_url = f"https://git:{token}@github.com/{owner}/{repo}.git"
        subprocess.run(['git', '-c', 'credential.helper=', 'remote', 'set-url', 'origin', auth_url],
                       capture_output=True, text=True, timeout=5, env=git_env)

        self.bot.logger.log(MODULE_NAME, "Git auto-configured: user, email, remote with embedded token")
    
    def _log_dev_features(self):
        self.bot.logger.log(MODULE_NAME, "Development features enabled:")
        self.bot.logger.log(MODULE_NAME, " • Automatic versioning (MAJOR.MINOR.PATCH.MICRO)")
        self.bot.logger.log(MODULE_NAME, " • Git integration (commit & push)")
        self.bot.logger.log(MODULE_NAME, " • Development console commands")
        self.bot.logger.log(MODULE_NAME, f" • Auto-commit: {'ENABLED' if self.auto_commit_enabled else 'DISABLED'}")
        self.bot.logger.log(MODULE_NAME, f" • Auto-versioning: {'ENABLED' if self.auto_versioning_enabled else 'DISABLED'}")
    
    def _check_git(self):
        """Check if Git is available and repository is initialized"""
        try:
            result = subprocess.run(
                ['git', '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                result = subprocess.run(
                    ['git', 'rev-parse', '--git-dir'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    self.git_enabled = True
                    self.bot.logger.log(MODULE_NAME, "Git repository detected")
                else:
                    self.bot.logger.log(MODULE_NAME, 
                        "Git available but not in a repository. Run 'git init' to enable Git features.", 
                        "WARNING")
            else:
                self.bot.logger.log(MODULE_NAME, "Git not available", "WARNING")
                
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            self.bot.logger.log(MODULE_NAME, "Git not available", "WARNING")
    
    def setup_github_with_token(self, token):
        """Setup GitHub using token from config/auth.json."""
        try:
            root = Path(__file__).parent.parent
            token_path = (root / "config" / "auth.json")
            if token_path:
                with open(token_path, 'r', encoding='utf-8') as f:
                    creds = json.load(f)
                gh_token = creds.get("github_token", token)
                gh_email = creds.get("github_email", "")
                gh_name = creds.get("github_name", "")
            else:
                gh_token = token
                gh_email = ""
                gh_name = ""
            self._auto_configure_git(gh_token, gh_email, gh_name)
            self.git_enabled = True
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "GitHub token setup failed", e)
            return False
    
    def get_repo_owner_and_name(self):
        """Extract repo owner/name from current remote"""
        try:
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                # Handle both SSH and HTTPS URLs
                url = result.stdout.strip()
                if 'github.com' in url:
                    if url.startswith('https://'):
                        # https://github.com/owner/repo.git
                        parts = url.split('/')
                        if len(parts) >= 5:
                            return f"{parts[3]}/{parts[4].replace('.git', '')}"
                    else:
                        # git@github.com:owner/repo.git
                        if ':' in url:
                            return url.split(':')[1].replace('.git', '')
            return "owner/repo" # Fallback
        except:
            return "owner/repo" # Fallback
    
    def get_github_username(self):
        """Try to get GitHub username from git config"""
        try:
            result = subprocess.run(
                ['git', 'config', 'user.name'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        return "your_username" # Fallback
    
    def _ensure_gitignore(self):
        """Create a comprehensive .gitignore if it doesn't exist"""
        gitignore_path = Path('.gitignore')
        
        if gitignore_path.exists():
            self.bot.logger.log(MODULE_NAME, ".gitignore already exists")
            return
        
        gitignore_content = r"""# Python
__pycache__/
*.pyc
*.pyo
*.pyd

# Virtual environments
.venv/
venv/
env/
Winpython64*/

# Project tooling
.git/
pyproject.toml
uv.lock

# Runtime data
logs/
cache/
db/
temp/
*.log
*.db
*.sqlite
*.tmp

# Secrets
.env
config/auth.json
.python-version

# Instance config (auto-generated)
config/*.json

# Launcher scripts (per-machine)
start.bat
start.sh

# OS
.DS_Store
Thumbs.db
desktop.ini

# Temp scripts
clean_comments.py
check_broken.py
fix_spaces.py
"""
        
        try:
            with open(gitignore_path, 'w') as f:
                f.write(gitignore_content)
            self.bot.logger.log(MODULE_NAME, "Created .gitignore template")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to create .gitignore", e)
    
    def _get_version_from_file(self):
        """Read version from _version.py file directly"""
        try:
            version_file = Path(__file__).parent.parent / "_version.py"
            if version_file.exists():
                with open(version_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Extract version using regex to avoid importing
                match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
                if match:
                    return match.group(1)
            
            return "0.0.0.0"
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to read version from _version.py", e)
            return "0.0.0.0"

    def _save_version_to_file(self, version):
        """Save version to _version.py file"""
        try:
            version_content = f'__version__ = "{version}"\n'
            
            with open(str(Path(__file__).parent.parent / "_version.py"), 'w', encoding='utf-8') as f:
                f.write(version_content)

            self.bot.logger.log(MODULE_NAME, f"Saved version to _version.py: v{version}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save version to _version.py", e)
    
    def _increment_version(self, current_version, lines_changed):
        parts = current_version.split('.')
        major, minor, patch, micro = map(int, parts)
        cfg = self._config
        
        if lines_changed >= cfg.get("breaking_threshold", 500):
            major += 1; minor = 0; patch = 0; micro = 0
            change_type = "BREAKING"
        elif lines_changed >= cfg.get("major_threshold", 100):
            minor += 1; patch = 0; micro = 0
            change_type = "MAJOR"
        elif lines_changed >= cfg.get("minor_threshold", 20):
            patch += 1; micro = 0
            change_type = "MINOR"
        elif lines_changed >= cfg.get("patch_threshold", 1):
            micro += 1
            change_type = "MICRO"
        else:
            return current_version, "NONE"
        
        return f"{major}.{minor}.{patch}.{micro}", change_type

    async def _get_git_diff_lines(self, working_tree: bool = False) -> tuple[int, list[dict]]:
        """Returns (total_lines, file_details).
        If working_tree=True, diffs working tree vs HEAD.
        Otherwise diffs from the last version-bump commit to HEAD."""
        git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
        try:
            if working_tree:
                numstat_args = ['git', '-c', 'credential.helper=', 'diff', '--numstat', 'HEAD']
            else:
                base = await self._get_last_version_bump_commit()
                if base:
                    numstat_args = ['git', '-c', 'credential.helper=', 'diff', '--numstat', base, 'HEAD']
                else:
                    numstat_args = ['git', '-c', 'credential.helper=', 'diff', '--numstat', '--root', 'HEAD']

            proc = await asyncio.create_subprocess_exec(
                *numstat_args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return 0, []

            total = 0
            files = []
            for line in stdout.decode().strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) >= 3:
                    try:
                        added = int(parts[0])
                        removed = int(parts[1])
                        total += added + removed
                        files.append({'path': parts[2], 'added': added, 'removed': removed})
                    except ValueError:
                        pass
            return total, files
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Git diff failed: {e}", "WARNING")
            return 0, []

    async def _get_last_version_bump_commit(self) -> str:
        """Get the hash of the most recent commit matching vX.X.X.X. Returns empty string if none."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'log', '--oneline', '-20',
                '--grep=^v[0-9]',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
            )
            stdout, _ = await proc.communicate()
            for line in stdout.decode().strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(' ', 1)
                msg = parts[1] if len(parts) > 1 else ''
                if re.match(r'^v\d+\.\d+\.\d+\.\d+$', msg.strip()):
                    return parts[0]
            return ''
        except Exception:
            return ''

    async def _is_head_version_bump(self) -> bool:
        """Check if HEAD commit message looks like a version bump (e.g. 'v4.2.1.1')."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'log', '-1', '--format=%s', 'HEAD',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
            )
            stdout, _ = await proc.communicate()
            return bool(re.match(r'^v\d+\.\d+\.\d+\.\d+$', stdout.decode().strip()))
        except Exception:
            return False

    async def check_and_update_version(self, auto_commit=False, commit_message=None):
        try:
            current_version = self._get_version_from_file()
            self.bot.logger.log(MODULE_NAME, f"Checking version (current: v{current_version})...")

            # First, check for uncommitted changes
            lines_changed, files = await self._get_git_diff_lines(working_tree=True)
            is_working_tree = True

            if lines_changed == 0 and self.git_enabled:
                if await self._is_head_version_bump():
                    self.bot.logger.log(MODULE_NAME, "HEAD is already a version bump — skipping")
                    return None
                lines_changed, files = await self._get_git_diff_lines(working_tree=False)
                is_working_tree = False

            if lines_changed == 0:
                self.bot.logger.log(MODULE_NAME, "No changes detected — skipping")
                return None

            self.bot.logger.log(MODULE_NAME,
                f"{'Working-tree' if is_working_tree else 'Commit'} diff: {len(files)} files, {lines_changed} lines")
            for f in files[:20]:
                self.bot.logger.log(MODULE_NAME,
                    f"  {f['path']}: +{f['added']} -{f['removed']}")

            if not self.auto_versioning_enabled:
                self.bot.logger.log(MODULE_NAME, "Auto-versioning disabled, skipping")
                return None

            old_version = current_version
            new_version, change_type = self._increment_version(current_version, lines_changed)

            if change_type == "NONE":
                self.bot.logger.log(MODULE_NAME, "Changes too minor to increment version")
                return None

            history_entry = {
                'version': new_version,
                'previous_version': old_version,
                'change_type': change_type,
                'timestamp': datetime.utcnow().isoformat(),
                'lines_changed': lines_changed,
                'files': files,
            }
            self.version_history.append(history_entry)
            if len(self.version_history) > 100:
                self.version_history = self.version_history[-100:]

            self._save_version_to_file(new_version)
            self.bot.version = new_version
            self.last_check_time = datetime.utcnow().isoformat()

            self.bot.logger.log(MODULE_NAME, f"Version bumped: v{old_version} -> v{new_version} ({change_type})")

            should_auto_commit = auto_commit and self.auto_commit_enabled and self.git_enabled
            if should_auto_commit:
                self.bot.logger.log(MODULE_NAME,
                    f"Auto-committing {'all changes' if is_working_tree else 'version bump'}...")
                success = await self._git_commit_version_bump(history_entry, stage_all=is_working_tree)
                if success:
                    self.bot.logger.log(MODULE_NAME, "Committed and pushed")
                else:
                    self.bot.logger.log(MODULE_NAME, "Commit completed with warnings", "WARNING")

            return history_entry

        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error checking version", e)
            return None

    async def _git_commit_version_bump(self, version_entry, stage_all: bool = False):
        if not self.git_enabled:
            return False
        message = f"v{version_entry['version']}"
        git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
        try:
            if stage_all:
                proc = await asyncio.create_subprocess_exec(
                    'git', '-c', 'credential.helper=', 'add', '-A',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=git_env
                )
                await proc.wait()
            else:
                version_file = Path(__file__).parent.parent / "_version.py"
                proc = await asyncio.create_subprocess_exec(
                    'git', '-c', 'credential.helper=', 'add', str(version_file),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=git_env
                )
                await proc.wait()
            proc = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'commit', '-m', message,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0 and "nothing to commit" not in stderr.decode():
                self.bot.logger.log(MODULE_NAME, f"Commit failed: {stderr.decode()[:200]}", "WARNING")
                return False
            self.bot.logger.log(MODULE_NAME, f"Committed: {message}")
            proc = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'pull', '--rebase', 'origin', 'main',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            await proc.communicate()
            proc = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'push', 'origin', 'main',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                self.bot.logger.log(MODULE_NAME, f"Push failed: {stderr.decode()[:200]}", "WARNING")
                return True
            self.bot.logger.log(MODULE_NAME, "Pushed to remote")
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Version bump commit error", e)
            return False
    
    async def git_commit_and_push(self, message=None, version_entry=None):
        """Commit and push changes to Git"""
        if not self.git_enabled:
            self.bot.logger.log(MODULE_NAME, "Git not enabled", "WARNING")
            return False
        
        git_env = {**__import__('os').environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
        
        try:
            current_version = self._get_version_from_file()
            
            # Generate clean commit message if not provided
            if not message and version_entry:
                # Simple, clean format: "v4.2.1.1"
                message = f"v{version_entry['version']}"
            elif not message:
                message = f"v{current_version}"
            
            # Stage all changes
            result = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'add', '-A',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            await result.wait()
            
            # Commit
            result = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'commit', '-m', message,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            stdout, stderr = await result.communicate()
            
            if result.returncode != 0:
                error = stderr.decode()
                if "nothing to commit"in error:
                    self.bot.logger.log(MODULE_NAME, "Nothing to commit")
                    return False
                else:
                    self.bot.logger.error(MODULE_NAME, f"Git commit failed: {error}")
                    return False
            
            self.bot.logger.log(MODULE_NAME, f"Committed: {message}")
            
            # Pull with rebase to sync remote changes, then push
            pull = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'pull', '--rebase', 'origin', 'main',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            pull_stdout, pull_stderr = await pull.communicate()
            if pull.returncode != 0:
                err = pull_stderr.decode().strip()
                self.bot.logger.log(MODULE_NAME, f"Git pull skipped (no upstream or rebase failed): {err.split(chr(10))[0]}", "WARNING")
            
            push = await asyncio.create_subprocess_exec(
                'git', '-c', 'credential.helper=', 'push', 'origin', 'main',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env
            )
            stdout, stderr = await push.communicate()
            
            if push.returncode != 0:
                error = stderr.decode()
                self.bot.logger.log(MODULE_NAME, f"Git push failed: {error.strip().split(chr(10))[0]}", "WARNING")
                return True
            
            self.bot.logger.log(MODULE_NAME, "Pushed to remote repository")
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Git operation failed", e)
            return False
    
    
    def get_version_info(self):
        """Get formatted version information"""
        current_version = self._get_version_from_file()
        info = {
            'current_version': current_version,
            'total_versions': len(self.version_history) + 1,
            'last_update': None,
            'git_enabled': self.git_enabled,
            'auto_commit_enabled': self.auto_commit_enabled,
            'auto_versioning_enabled': self.auto_versioning_enabled,
            'last_check_time': self.last_check_time,
        }
        if self.version_history:
            last = self.version_history[-1]
            info['last_update'] = {
                'version': last['version'],
                'type': last['change_type'],
                'timestamp': last['timestamp'],
                'lines_changed': last['lines_changed'],
            }
        return info

def setup(bot, register_console_command):
    bot.logger.log(MODULE_NAME, "Setting up DEV module (development mode)")

    if hasattr(bot, 'dev_manager') and bot.dev_manager is not None:
        dev_manager = bot.dev_manager
        bot.logger.log(MODULE_NAME, "Reusing pre-flight DevManager")
    else:
        dev_manager = DevManager(bot)
        bot.dev_manager = dev_manager

    bot.version = dev_manager._get_version_from_file()

    async def initial_version_check():
        await bot.wait_until_ready()
        await asyncio.sleep(2)
        bot.logger.log(MODULE_NAME, "Running post-login version check...")
        version_entry = await dev_manager.check_and_update_version(auto_commit=True)
        bot.version = dev_manager._get_version_from_file()
        if version_entry:
            bot.logger.log(MODULE_NAME, f"Version bumped to v{version_entry['version']}")
        else:
            bot.logger.log(MODULE_NAME, f"Version check complete — v{bot.version} (no change)")
    
    asyncio.create_task(initial_version_check())
    
    # Register development console commands
    def setup_dev_console_commands():
        """Register development-specific console commands"""
        async def handle_commit(args):
            message = args.strip() if args.strip() else None
            print("Checking for changes...")
            version_entry = None
            if dev_manager.auto_versioning_enabled:
                version_entry = await dev_manager.check_and_update_version(auto_commit=False)
                if version_entry:
                    print(f"Version updated to v{version_entry['version']}")
            print("Committing and pushing...")
            success = await dev_manager.git_commit_and_push(message, version_entry)
            if success:
                print(f"Changes committed and pushed! (v{dev_manager._get_version_from_file()})")
            else:
                print("Git operation completed with warnings (check logs)")
        
        async def handle_changelog(args):
            count = int(args) if args.strip().isdigit() else 10
            print(f"\n Changelog (last {count} version bumps):\n")
            try:
                proc = await asyncio.create_subprocess_exec(
                    'git', '-c', 'credential.helper=', 'log', '--oneline',
                    f'-{count * 2}',  # grab extra in case non-version commits are mixed in
                    '--grep=^v[0-9]',  # only commits matching v4.2.1.1 etc
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
                )
                stdout, _ = await proc.communicate()
                lines = stdout.decode().strip().split('\n')
                shown = 0
                for line in lines:
                    if not line.strip():
                        continue
                    # git log --oneline format: "abc1234 v4.2.1.1"
                    parts = line.split(' ', 1)
                    msg = parts[1] if len(parts) > 1 else line
                    if not re.match(r'^v\d+\.\d+\.\d+\.\d+$', msg.strip()):
                        continue
                    print(f"  {msg.strip()}")
                    shown += 1
                    if shown >= count:
                        break
                if shown == 0:
                    print("  (no version bumps found in git log)")
                print()
            except Exception as e:
                print(f"  Error reading git log: {e}\n")
        
        async def handle_git(args):
            """Show git repository status"""
            import subprocess
            try:
                # Current branch
                result = subprocess.run(
                    ['git', 'branch', '--show-current'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                branch = result.stdout.strip() or "unknown"
                
                # Check for uncommitted changes
                result = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                changes = result.stdout.strip()
                
                # Get remote
                result = subprocess.run(
                    ['git', 'remote', '-v'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                remotes = result.stdout.strip()
                
                print("\n┌─────────────────────────────────────────────────────────────────────┐")
                print("│                     GIT REPOSITORY STATUS                           │")
                print("├─────────────────────────────────────────────────────────────────────┤")
                print(f"│ Branch: {branch:<53} │")
                print(f"│ Status: {'🟢 Clean' if not changes else '🟡 Uncommitted changes':<53} │")
                
                if changes:
                    print("├─────────────────────────────────────────────────────────────────────┤")
                    print("│ Uncommitted changes:                                                │")
                    for line in changes.split('\n')[:10]:  # Show first 10 changes
                        print(f"│   {line:<59} │")
                
                if remotes:
                    print("├─────────────────────────────────────────────────────────────────────┤")
                    print("│ Remotes:                                                            │")
                    for line in remotes.split('\n'):
                        print(f"│   {line[:59]:<59} │")
                
                print("└─────────────────────────────────────────────────────────────────────┘\n")
                
            except Exception as e:
                print(f"Error getting git status: {e}")
        
        async def handle_setup_github(args):
            """Setup GitHub with token - usage: setup_github YOUR_TOKEN"""
            token = args.strip()
            if not token:
                print("Usage: setup_github YOUR_GITHUB_TOKEN")
                print("  Get token from: GitHub → Settings → Developer settings → Personal access tokens")
                return
            
            print("Setting up GitHub with token...")
            success = dev_manager.setup_github_with_token(token)
            
            if success:
                print("GitHub configured! Try 'commit test' now.")
            else:
                print("Failed to setup GitHub. Check logs.")
        
        async def handle_dev_status(args):
            """Show development module status"""
            info = dev_manager.get_version_info()
            
            print("\n┌─────────────────────────────────────────────────────────────────────┐")
            print("│                      DEVELOPMENT MODULE STATUS                       │")
            print("├─────────────────────────────────────────────────────────────────────┤")
            print(f"│ Current Version: v{info['current_version']:<44} │")
            print(f"│ Total Versions: {info['total_versions']:<46} │")
            print(f"│ Git Enabled: {' Yes' if info['git_enabled'] else ' No':<49} │")
            print(f"│ Auto-Commit: {' Enabled' if info['auto_commit_enabled'] else ' Disabled':<45} │")
            print(f"│ Auto-Versioning: {' Enabled' if info['auto_versioning_enabled'] else ' Disabled':<41} │")
            
            if info['last_update']:
                last = info['last_update']
                timestamp = datetime.fromisoformat(last['timestamp']).strftime('%Y-%m-%d %H:%M')
                print("├─────────────────────────────────────────────────────────────────────┤")
                print("│ Last Update:                                                        │")
                
                version_text = f"v{last['version']} ({last['type']})"
                spacing = 36 - len(version_text)
                print(f"│   Version: {version_text}{'' * spacing} │")
                
                print(f"│   Time: {timestamp}{'' * (50 - len(timestamp))} │")
                
                lines_text = f"{last['lines_changed']} lines"
                spacing = 35 - len(lines_text)
                print(f"│   Lines: {lines_text}{'' * spacing} │")
            
            print("└─────────────────────────────────────────────────────────────────────┘\n")
        
        async def handle_auto_commit(args):
            a = args.strip().lower()
            if a in ('on', 'enable', 'true', '1'):
                dev_manager.auto_commit_enabled = True
            elif a in ('off', 'disable', 'false', '0'):
                dev_manager.auto_commit_enabled = False
            elif a == '':
                dev_manager.auto_commit_enabled = not dev_manager.auto_commit_enabled
            dev_manager._config["auto_commit"] = dev_manager.auto_commit_enabled
            _save_dev_config({"auto_commit": dev_manager.auto_commit_enabled})
            status = "ENABLED" if dev_manager.auto_commit_enabled else "DISABLED"
            print(f"Auto-commit {status}")
            dev_manager.bot.logger.log(MODULE_NAME, f"Auto-commit {status}")
        
        async def handle_auto_version(args):
            a = args.strip().lower()
            if a in ('on', 'enable', 'true', '1'):
                dev_manager.auto_versioning_enabled = True
            elif a in ('off', 'disable', 'false', '0'):
                dev_manager.auto_versioning_enabled = False
            elif a == '':
                dev_manager.auto_versioning_enabled = not dev_manager.auto_versioning_enabled
            dev_manager._config["auto_versioning"] = dev_manager.auto_versioning_enabled
            _save_dev_config({"auto_versioning": dev_manager.auto_versioning_enabled})
            status = "ENABLED" if dev_manager.auto_versioning_enabled else "DISABLED"
            print(f"Auto-versioning {status}")
            dev_manager.bot.logger.log(MODULE_NAME, f"Auto-versioning {status}")
        
        # Register development console commands
        register_console_command("commit", "Commit and push changes", handle_commit)
        register_console_command("changelog", "Show version changelog", handle_changelog)
        register_console_command("git", "Show git repository status", handle_git)
        register_console_command("setup_github", "Setup GitHub with personal access token", handle_setup_github)
        register_console_command("dev_status", "Show development module status", handle_dev_status)
        register_console_command("auto_commit", "Toggle auto-commit on/off", handle_auto_commit)
        register_console_command("auto_version", "Toggle auto-versioning on/off", handle_auto_version)
    
    setup_dev_console_commands()
    
    bot.logger.log(MODULE_NAME, f"DEV module setup complete - v{bot.version}")