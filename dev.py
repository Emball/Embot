import os
import hashlib
import json
import subprocess
import importlib
import sys
from pathlib import Path
from datetime import datetime
import discord
from discord import app_commands
import re
import asyncio
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import traceback
import difflib

MODULE_NAME = "DEV"

# Configuration
VERSION_DATA_FILE = "version_data.json"
TRACKED_EXTENSIONS = ['.py']
# Exclude files that the bot writes to (prevents self-triggering)
EXCLUDE_FILES = [
    '_version.py',           # Version file
    'version_data.json',     # Version history
    'song_index.json',       # Archive index
    'cache_index.json',      # Archive cache
    'moderation_strikes.json',  # Moderation data
    'member_roles.json'      # Role persistence
]
EXCLUDE_DIRS = ['Winpython64', 'python-3', 'venv', 'env', '.git', '__pycache__', 'data', 'icons']

# Enhanced semantic versioning thresholds (actual lines changed, not estimated)
BREAKING_THRESHOLD = 500    # Breaking changes (major version bump)
MAJOR_THRESHOLD = 100       # Major features (minor version bump)
MINOR_THRESHOLD = 20        # Minor features (patch version bump)
PATCH_THRESHOLD = 1         # Bug fixes (micro version bump)


class FileChangeHandler(FileSystemEventHandler):
    """Handles file system events for hot-reloading"""
    
    def __init__(self, dev_manager, loop):
        self.dev_manager = dev_manager
        self.loop = loop
        self.last_reload_time = {}
        self.reload_cooldown = 2  # seconds
    
    def should_ignore_file(self, file_path):
        """Check if file should be ignored for hot-reload"""
        file_name = file_path.name
        
        # Ignore all excluded files
        for exclude in EXCLUDE_FILES:
            if file_name == exclude:
                return True
        
        # Ignore directories
        for exclude_dir in EXCLUDE_DIRS:
            if exclude_dir in str(file_path):
                return True
        
        return False
    
    def on_modified(self, event):
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        
        # Only track Python files in the bot directory
        if file_path.suffix != '.py':
            return
        
        # Use the improved ignore check
        if self.should_ignore_file(file_path):
            return
        
        # Cooldown to prevent multiple rapid reloads
        now = datetime.now().timestamp()
        last_reload = self.last_reload_time.get(str(file_path), 0)
        
        if now - last_reload < self.reload_cooldown:
            return
        
        self.last_reload_time[str(file_path)] = now
        
        # Schedule reload using the event loop from the main thread
        asyncio.run_coroutine_threadsafe(
            self.dev_manager.reload_module(file_path),
            self.loop
        )


class DevManager:
    """Manages development mode: versioning, hot-reloading, and GitHub integration"""
    
    def __init__(self, bot):
        self.bot = bot
        self.version_history = []
        self.file_hashes = {}
        self.file_contents = {}  # Store actual file contents for diff
        self.last_check_time = None
        self.git_enabled = False
        self.file_observer = None
        self.watched_modules = {}
        self.auto_commit_enabled = False  # Default to disabled
        self.auto_versioning_enabled = True  # Default to enabled
        
        # Check Git availability
        self._check_git()
        
        # Load existing version data
        self._load_version_data()
        
        # Create .gitignore if it doesn't exist
        self._ensure_gitignore()
        
        # Log development features
        self._log_dev_features()
    
    def _log_dev_features(self):
        """Log development-specific features that are enabled"""
        self.bot.logger.log(MODULE_NAME, "🔧 Development features enabled:")
        self.bot.logger.log(MODULE_NAME, "  • Hot-reload on file changes")
        self.bot.logger.log(MODULE_NAME, "  • Automatic versioning (MAJOR.MINOR.PATCH.MICRO)")
        self.bot.logger.log(MODULE_NAME, "  • Git integration (commit & push)")
        self.bot.logger.log(MODULE_NAME, "  • File watcher monitoring")
        self.bot.logger.log(MODULE_NAME, "  • Development console commands")
        self.bot.logger.log(MODULE_NAME, f"  • Auto-commit: {'ENABLED' if self.auto_commit_enabled else 'DISABLED'}")
        self.bot.logger.log(MODULE_NAME, f"  • Auto-versioning: {'ENABLED' if self.auto_versioning_enabled else 'DISABLED'}")
        self.bot.logger.log(MODULE_NAME, f"  • Excluded files: {', '.join(EXCLUDE_FILES)}")
        self.bot.logger.log(MODULE_NAME, f"  • Excluded dirs: {', '.join(EXCLUDE_DIRS)}")
    
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
        """Simple GitHub token setup - no SSH bullshit"""
        try:
            # Get repo owner/name
            repo_path = self.get_repo_owner_and_name()
            if repo_path == "owner/repo":
                self.bot.logger.log(MODULE_NAME, "Could not detect repository, using current directory", "WARNING")
                # Try to get from current directory
                current_dir = Path.cwd().name
                repo_path = f"{self.get_github_username()}/{current_dir}"
            
            # Set the remote URL with token
            repo_url = f"https://{token}@github.com/{repo_path}.git"
            
            self.bot.logger.log(MODULE_NAME, f"Setting remote URL: https://token@github.com/{repo_path}.git")
            
            result = subprocess.run(
                ['git', 'remote', 'set-url', 'origin', repo_url],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode == 0:
                self.bot.logger.log(MODULE_NAME, "✅ GitHub token configured successfully!")
                self.git_enabled = True
                return True
            else:
                self.bot.logger.error(MODULE_NAME, f"Failed to set remote: {result.stderr}")
                # Try adding remote if it doesn't exist
                result = subprocess.run(
                    ['git', 'remote', 'add', 'origin', repo_url],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    self.bot.logger.log(MODULE_NAME, "✅ Added origin remote with token!")
                    self.git_enabled = True
                    return True
                return False
                
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
            return "owner/repo"  # Fallback
        except:
            return "owner/repo"  # Fallback
    
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
        return "your_username"  # Fallback
    
    def _ensure_gitignore(self):
        """Create a comprehensive .gitignore if it doesn't exist"""
        gitignore_path = Path('.gitignore')
        
        if gitignore_path.exists():
            self.bot.logger.log(MODULE_NAME, ".gitignore already exists")
            return
        
        gitignore_content = """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Bot-specific
*.log
version_data.json
song_index.json
cache_index.json
moderation_strikes.json
member_roles.json
.env
*.db
*.sqlite
data/
cache/
temp/
*.tmp

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
desktop.ini

# Voice messages and audio
*.ogg
*.mp3
*.wav
*.m4a

# Bot token and secrets
DISCORD_BOT_TOKEN
config.ini
secrets.json

# Music library (if you don't want to track it)
# D:/Media/Music/

# Windows Python distributions
Winpython64/
python-3/

# Large data directories
icons/
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
            version_file = Path("_version.py")
            if version_file.exists():
                with open(version_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Extract version using regex to avoid importing
                import re
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
            
            with open("_version.py", 'w', encoding='utf-8') as f:
                f.write(version_content)

            self.bot.logger.log(MODULE_NAME, f"Saved version to _version.py: v{version}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save version to _version.py", e)
    
    def _load_version_data(self):
        """Load version data from file"""
        try:
            # Always get version from _version.py
            current_version = self._get_version_from_file()
            
            if Path(VERSION_DATA_FILE).exists():
                with open(VERSION_DATA_FILE, 'r') as f:
                    data = json.load(f)
                    self.version_history = data.get('history', [])
                    self.file_hashes = data.get('file_hashes', {})
                    self.file_contents = data.get('file_contents', {})
                    self.last_check_time = data.get('last_check_time')
                    self.bot.logger.log(MODULE_NAME, f"Loaded version data: v{current_version}")
            else:
                self.bot.logger.log(MODULE_NAME, f"No version data found, starting fresh at v{current_version}")
                self._save_version_data()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load version data", e)
    
    def _save_version_data(self):
        """Save version data to file (without version number)"""
        try:
            data = {
                'history': self.version_history,
                'file_hashes': self.file_hashes,
                'file_contents': self.file_contents,
                'last_check_time': datetime.utcnow().isoformat()
            }
            
            with open(VERSION_DATA_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            
            self.bot.logger.log(MODULE_NAME, "Saved version data")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save version data", e)
    
    def _get_file_hash(self, file_path):
        """Calculate SHA256 hash of a file"""
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            return hashlib.sha256(content).hexdigest()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to hash {file_path}", e)
            return None
    
    def _get_file_content(self, file_path):
        """Get file content as string"""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return content
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to read {file_path}", e)
            return ""
    
    def _should_exclude_file(self, file_path):
        """Check if a file should be excluded from tracking"""
        path_str = str(file_path)
        file_name = file_path.name
        
        # Exclude specific files by exact name match
        for exclude in EXCLUDE_FILES:
            if file_name == exclude:
                return True
        
        # Exclude by directory
        for exclude_dir in EXCLUDE_DIRS:
            if exclude_dir in path_str:
                return True
        
        return False
    
    def _scan_codebase(self):
        """Scan all Python files and return their hashes and contents"""
        current_hashes = {}
        current_contents = {}
        
        bot_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        
        tracked_count = 0
        excluded_count = 0
        
        for file_path in bot_dir.glob('*.py'):
            if file_path.is_file():
                if self._should_exclude_file(file_path):
                    excluded_count += 1
                    continue
                
                relative_path = file_path.name
                file_hash = self._get_file_hash(file_path)
                file_content = self._get_file_content(file_path)
                
                if file_hash:
                    current_hashes[relative_path] = file_hash
                    current_contents[relative_path] = file_content
                    tracked_count += 1
        
        if tracked_count > 0 or excluded_count > 0:
            self.bot.logger.log(MODULE_NAME, 
                f"Scanned codebase: {tracked_count} tracked, {excluded_count} excluded")
        
        return current_hashes, current_contents
    
    def _calculate_actual_changes(self, old_contents, new_contents):
        """Calculate actual line-by-line changes using difflib"""
        changes = {
            'added': [],
            'modified': [],
            'deleted': [],
            'total_lines_changed': 0,
            'files_changed': 0,
            'details': {}
        }
        
        # Find added and modified files
        for file_path, new_content in new_contents.items():
            if file_path not in old_contents:
                # New file
                changes['added'].append(file_path)
                lines = new_content.split('\n')
                non_empty = sum(1 for line in lines if line.strip())
                changes['total_lines_changed'] += non_empty
                changes['files_changed'] += 1
                changes['details'][file_path] = {
                    'type': 'added',
                    'lines_changed': non_empty
                }
            elif old_contents[file_path] != new_content:
                # Modified file - use actual diff
                old_lines = old_contents[file_path].split('\n')
                new_lines = new_content.split('\n')
                
                # Use difflib to get actual changes
                diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=''))
                
                # Count actual changed lines (lines starting with + or -)
                added_lines = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
                removed_lines = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
                
                lines_changed = added_lines + removed_lines
                
                changes['modified'].append(file_path)
                changes['total_lines_changed'] += lines_changed
                changes['files_changed'] += 1
                changes['details'][file_path] = {
                    'type': 'modified',
                    'lines_changed': lines_changed,
                    'added': added_lines,
                    'removed': removed_lines
                }
        
        # Find deleted files
        for file_path in old_contents:
            if file_path not in new_contents:
                changes['deleted'].append(file_path)
                changes['files_changed'] += 1
                changes['details'][file_path] = {
                    'type': 'deleted'
                }
        
        return changes
    
    def _increment_version(self, current_version, lines_changed):
        """Increment version based on lines changed (MAJOR.MINOR.PATCH.MICRO)"""
        parts = current_version.split('.')
        major, minor, patch, micro = map(int, parts)
        
        if lines_changed >= BREAKING_THRESHOLD:
            major += 1
            minor = 0
            patch = 0
            micro = 0
            change_type = "BREAKING"
        elif lines_changed >= MAJOR_THRESHOLD:
            minor += 1
            patch = 0
            micro = 0
            change_type = "MAJOR"
        elif lines_changed >= MINOR_THRESHOLD:
            patch += 1
            micro = 0
            change_type = "MINOR"
        elif lines_changed >= PATCH_THRESHOLD:
            micro += 1
            change_type = "MICRO"
        else:
            return current_version, "NONE"
        
        new_version = f"{major}.{minor}.{patch}.{micro}"
        return new_version, change_type
    
    async def check_and_update_version(self, auto_commit=False, commit_message=None):
        """Check for changes and update version if necessary"""
        try:
            current_version = self._get_version_from_file()
            self.bot.logger.log(MODULE_NAME, f"Scanning codebase for changes (current: v{current_version})...")
            
            current_hashes, current_contents = self._scan_codebase()
            
            if not self.file_hashes:
                self.bot.logger.log(MODULE_NAME, f"First run detected, baseline set at v{current_version}")
                self.file_hashes = current_hashes
                self.file_contents = current_contents
                self._save_version_data()
                return None
            
            changes = self._calculate_actual_changes(self.file_contents, current_contents)
            
            if changes['files_changed'] == 0:
                self.bot.logger.log(MODULE_NAME, "No changes detected")
                return None
            
            # Log detailed changes
            self.bot.logger.log(MODULE_NAME, 
                f"Changes detected: {changes['files_changed']} files, {changes['total_lines_changed']} actual lines changed")
            
            for file, details in changes['details'].items():
                if details['type'] == 'modified':
                    self.bot.logger.log(MODULE_NAME, 
                        f"  {file}: +{details['added']} -{details['removed']} = {details['lines_changed']} lines")
                else:
                    self.bot.logger.log(MODULE_NAME, f"  {file}: {details['type']}")
            
            # Only update version if auto-versioning is enabled
            if not self.auto_versioning_enabled:
                self.bot.logger.log(MODULE_NAME, "Auto-versioning disabled, skipping version update")
                self.file_hashes = current_hashes
                self.file_contents = current_contents
                self._save_version_data()
                return None
            
            old_version = current_version
            new_version, change_type = self._increment_version(current_version, changes['total_lines_changed'])
            
            if change_type == "NONE":
                self.bot.logger.log(MODULE_NAME, "Changes too minor to increment version")
                self.file_hashes = current_hashes
                self.file_contents = current_contents
                self._save_version_data()
                return None
            
            history_entry = {
                'version': new_version,
                'previous_version': old_version,
                'change_type': change_type,
                'timestamp': datetime.utcnow().isoformat(),
                'files_changed': changes['files_changed'],
                'lines_changed': changes['total_lines_changed'],
                'added': changes['added'],
                'modified': changes['modified'],
                'deleted': changes['deleted'],
                'details': changes['details']
            }
            
            self.version_history.append(history_entry)
            
            if len(self.version_history) > 100:
                self.version_history = self.version_history[-100:]
            
            self.file_hashes = current_hashes
            self.file_contents = current_contents
            self._save_version_data()
            
            # Save new version to _version.py
            self._save_version_to_file(new_version)
            
            # Update bot.version
            self.bot.version = new_version
            
            self.bot.logger.log(MODULE_NAME, 
                f"Version updated: v{old_version} → v{new_version} ({change_type})")
            
            # Auto-commit if enabled
            if auto_commit and self.auto_commit_enabled and self.git_enabled:
                await self.git_commit_and_push(commit_message, history_entry)
            
            return history_entry
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error checking version", e)
            return None
    
    async def git_commit_and_push(self, message=None, version_entry=None):
        """Commit and push changes to Git"""
        if not self.git_enabled:
            self.bot.logger.log(MODULE_NAME, "Git not enabled", "WARNING")
            return False
        
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
                'git', 'add', '-A',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await result.wait()
            
            # Commit
            result = await asyncio.create_subprocess_exec(
                'git', 'commit', '-m', message,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()
            
            if result.returncode != 0:
                error = stderr.decode()
                if "nothing to commit" in error:
                    self.bot.logger.log(MODULE_NAME, "Nothing to commit")
                    return False
                else:
                    self.bot.logger.error(MODULE_NAME, f"Git commit failed: {error}")
                    return False
            
            self.bot.logger.log(MODULE_NAME, f"Committed: {message}")
            
            # Push
            result = await asyncio.create_subprocess_exec(
                'git', 'push',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()
            
            if result.returncode != 0:
                error = stderr.decode()
                self.bot.logger.error(MODULE_NAME, f"Git push failed: {error}")
                return False
            
            self.bot.logger.log(MODULE_NAME, "Pushed to remote repository")
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Git operation failed", e)
            return False
    
    async def reload_module(self, file_path):
        """Hot-reload a module when its file changes"""
        try:
            module_name = file_path.stem
            
            # Don't reload main.py or excluded files
            if module_name == 'main' or self._should_exclude_file(file_path):
                return
            
            self.bot.logger.log(MODULE_NAME, f"🔄 Reloading module: {module_name}")
            
            # Check if module exists in sys.modules
            if module_name not in sys.modules:
                self.bot.logger.log(MODULE_NAME, f"Module {module_name} not loaded yet", "WARNING")
                return
            
            try:
                # Reload the module
                module = importlib.reload(sys.modules[module_name])
                
                # Re-run setup if it exists
                if hasattr(module, 'setup'):
                    # Clear existing commands for this module
                    self.bot.tree.clear_commands(guild=None)
                    
                    # Re-setup the module - only pass register_console_command to dev module
                    if module_name == 'dev':
                        # Create a dummy register function for hot-reload
                        def dummy_register(*args, **kwargs):
                            pass
                        module.setup(self.bot, dummy_register)
                    else:
                        module.setup(self.bot)
                    
                    # Sync commands
                    await self.bot.tree.sync()
                    
                    self.bot.logger.log(MODULE_NAME, f"✅ Reloaded and synced: {module_name}")
                else:
                    self.bot.logger.log(MODULE_NAME, f"✅ Reloaded: {module_name} (no setup)")
                
                # Don't trigger version check on hot-reload - it causes cycles
                # Version will be checked on manual saves/commits instead
                
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, f"Failed to reload {module_name}", e)
                tb = traceback.format_exc()
                self.bot.logger.log(MODULE_NAME, f"Traceback:\n{tb}", "ERROR")
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error in reload_module for {file_path}", e)
    
    def start_file_watcher(self):
        """Start watching files for changes (only in dev mode)"""
        try:
            bot_dir = Path(os.path.dirname(os.path.abspath(__file__)))
            
            # Pass the bot's event loop to the file handler
            event_handler = FileChangeHandler(self, self.bot.loop)
            self.file_observer = Observer()
            self.file_observer.schedule(event_handler, str(bot_dir), recursive=False)
            self.file_observer.start()
            
            self.bot.logger.log(MODULE_NAME, "📁 File watcher started - hot-reloading enabled")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to start file watcher", e)
    
    def stop_file_watcher(self):
        """Stop watching files"""
        if self.file_observer:
            self.file_observer.stop()
            self.file_observer.join()
            self.bot.logger.log(MODULE_NAME, "File watcher stopped")
    
    def get_version_info(self):
        """Get formatted version information"""
        current_version = self._get_version_from_file()
        
        info = {
            'current_version': current_version,
            'total_versions': len(self.version_history) + 1,
            'tracked_files': len(self.file_hashes),
            'last_update': None,
            'git_enabled': self.git_enabled,
            'hot_reload_enabled': self.file_observer is not None,
            'auto_commit_enabled': self.auto_commit_enabled,
            'auto_versioning_enabled': self.auto_versioning_enabled
        }
        
        if self.version_history:
            last = self.version_history[-1]
            info['last_update'] = {
                'version': last['version'],
                'type': last['change_type'],
                'timestamp': last['timestamp'],
                'files_changed': last['files_changed'],
                'lines_changed': last['lines_changed']
            }
        
        return info


def setup(bot, register_console_command):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "🔧 Setting up DEV module (development mode)")
    
    dev_manager = DevManager(bot)
    bot.dev_manager = dev_manager
    
    # Get version from _version.py and set on bot
    bot.version = dev_manager._get_version_from_file()
    
    # Start file watcher for hot-reloading (only in dev mode)
    dev_manager.start_file_watcher()
    
    # Initial version check and auto-commit
    async def initial_version_check():
        await bot.wait_until_ready()
        await asyncio.sleep(1)
        
        bot.logger.log(MODULE_NAME, "Starting initial version check...")
        version_entry = await dev_manager.check_and_update_version(auto_commit=False)
        bot.version = dev_manager._get_version_from_file()
        
        if version_entry:
            bot.logger.log(MODULE_NAME, f"✅ Version updated to v{version_entry['version']}")
            
            # Auto-commit on startup if git is enabled
            if dev_manager.git_enabled:
                bot.logger.log(MODULE_NAME, "Auto-committing changes on startup...")
                success = await dev_manager.git_commit_and_push(None, version_entry)
                if success:
                    bot.logger.log(MODULE_NAME, f"✅ Auto-committed v{bot.version} on startup")
                else:
                    bot.logger.log(MODULE_NAME, "Auto-commit completed with warnings", "WARNING")
        else:
            bot.logger.log(MODULE_NAME, f"✅ Version check complete - v{bot.version} (no changes)")
    
    asyncio.create_task(initial_version_check())
    
    # Register development console commands
    def setup_dev_console_commands():
        """Register development-specific console commands"""
        async def handle_commit(args):
            """Commit and push changes"""
            message = args.strip() if args.strip() else None
            print("📦 Checking for changes...")
            
            # Only check version on manual commit if auto-versioning is enabled
            version_entry = None
            if dev_manager.auto_versioning_enabled:
                version_entry = await dev_manager.check_and_update_version(auto_commit=False)
                
                if version_entry:
                    print(f"✅ Version updated to v{version_entry['version']}")
            else:
                print("ℹ️ Auto-versioning disabled, using current version")
            
            print("📤 Committing and pushing...")
            
            success = await dev_manager.git_commit_and_push(message, version_entry)
            
            if success:
                current_version = dev_manager._get_version_from_file()
                print(f"✅ Changes committed and pushed! (v{current_version})")
            else:
                print("⚠️ Git operation completed with warnings (check logs)")
        
        async def handle_changelog(args):
            """Show version changelog"""
            count = int(args) if args.strip().isdigit() else 10
            count = min(count, len(dev_manager.version_history))
            
            if not dev_manager.version_history:
                print("No version history available yet.")
                return
            
            recent = dev_manager.version_history[-count:]
            
            print(f"\n📋 Changelog (last {count} versions):\n")
            
            for entry in reversed(recent):
                timestamp = datetime.fromisoformat(entry['timestamp'])
                
                type_emoji = {
                    'BREAKING': '🔴',
                    'MAJOR': '🟠',
                    'MINOR': '🟡',
                    'MICRO': '🟢'
                }.get(entry['change_type'], '⚪')
                
                print(f"{type_emoji} v{entry['version']} ({entry['change_type']}) - {timestamp.strftime('%Y-%m-%d %H:%M')}")
                print(f"   From v{entry['previous_version']}")
                print(f"   {entry['files_changed']} files, {entry['lines_changed']} lines changed")
                
                if entry.get('details'):
                    for file, details in entry['details'].items():
                        if details['type'] == 'modified':
                            print(f"   • {file}: +{details.get('added', 0)} -{details.get('removed', 0)}")
                        else:
                            print(f"   • {file}: {details['type']}")
                print()
        
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
                print(f"❌ Error getting git status: {e}")
        
        async def handle_files(args):
            """Show tracked files"""
            files = sorted(dev_manager.file_hashes.keys())
            
            print(f"\n📁 Tracked Files ({len(files)}):\n")
            for file in files:
                print(f"  • {file}")
            print()
        
        async def handle_setup_github(args):
            """Setup GitHub with token - usage: setup_github YOUR_TOKEN"""
            token = args.strip()
            if not token:
                print("❌ Usage: setup_github YOUR_GITHUB_TOKEN")
                print("   Get token from: GitHub → Settings → Developer settings → Personal access tokens")
                return
            
            print("🔄 Setting up GitHub with token...")
            success = dev_manager.setup_github_with_token(token)
            
            if success:
                print("✅ GitHub configured! Try 'commit test' now.")
            else:
                print("❌ Failed to setup GitHub. Check logs.")
        
        async def handle_dev_status(args):
            """Show development module status"""
            info = dev_manager.get_version_info()
            
            print("\n┌─────────────────────────────────────────────────────────────────────┐")
            print("│                      DEVELOPMENT MODULE STATUS                       │")
            print("├─────────────────────────────────────────────────────────────────────┤")
            print(f"│ Current Version: v{info['current_version']:<44} │")
            print(f"│ Tracked Files: {info['tracked_files']:<47} │")
            print(f"│ Total Versions: {info['total_versions']:<46} │")
            print(f"│ Git Enabled: {'✅ Yes' if info['git_enabled'] else '❌ No':<49} │")
            print(f"│ Hot Reload: {'✅ Enabled' if info['hot_reload_enabled'] else '❌ Disabled':<46} │")
            print(f"│ Auto-Commit: {'✅ Enabled' if info['auto_commit_enabled'] else '❌ Disabled':<45} │")
            print(f"│ Auto-Versioning: {'✅ Enabled' if info['auto_versioning_enabled'] else '❌ Disabled':<41} │")
            
            if info['last_update']:
                last = info['last_update']
                timestamp = datetime.fromisoformat(last['timestamp']).strftime('%Y-%m-%d %H:%M')
                print("├─────────────────────────────────────────────────────────────────────┤")
                print("│ Last Update:                                                        │")
                
                version_text = f"v{last['version']} ({last['type']})"
                spacing = 36 - len(version_text)
                print(f"│   Version: {version_text}{' ' * spacing} │")
                
                print(f"│   Time: {timestamp}{' ' * (50 - len(timestamp))} │")
                
                files_text = f"{last['files_changed']}, Lines: {last['lines_changed']}"
                spacing = 35 - len(files_text)
                print(f"│   Files: {files_text}{' ' * spacing} │")
            
            print("└─────────────────────────────────────────────────────────────────────┘\n")
        
        async def handle_auto_commit(args):
            """Toggle auto-commit functionality"""
            if args.strip().lower() in ['on', 'enable', 'true', '1']:
                dev_manager.auto_commit_enabled = True
                print("✅ Auto-commit ENABLED")
            elif args.strip().lower() in ['off', 'disable', 'false', '0']:
                dev_manager.auto_commit_enabled = False
                print("✅ Auto-commit DISABLED")
            else:
                # Toggle if no args
                dev_manager.auto_commit_enabled = not dev_manager.auto_commit_enabled
                status = "ENABLED" if dev_manager.auto_commit_enabled else "DISABLED"
                print(f"✅ Auto-commit {status}")
            
            dev_manager.bot.logger.log(MODULE_NAME, f"Auto-commit {status}")
        
        async def handle_auto_version(args):
            """Toggle auto-versioning functionality"""
            if args.strip().lower() in ['on', 'enable', 'true', '1']:
                dev_manager.auto_versioning_enabled = True
                print("✅ Auto-versioning ENABLED")
            elif args.strip().lower() in ['off', 'disable', 'false', '0']:
                dev_manager.auto_versioning_enabled = False
                print("✅ Auto-versioning DISABLED")
            else:
                # Toggle if no args
                dev_manager.auto_versioning_enabled = not dev_manager.auto_versioning_enabled
                status = "ENABLED" if dev_manager.auto_versioning_enabled else "DISABLED"
                print(f"✅ Auto-versioning {status}")
            
            dev_manager.bot.logger.log(MODULE_NAME, f"Auto-versioning {status}")
        
        # Register development console commands
        register_console_command("commit", "Commit and push changes", handle_commit)
        register_console_command("changelog", "Show version changelog", handle_changelog)
        register_console_command("git", "Show git repository status", handle_git)
        register_console_command("files", "Show tracked files", handle_files)
        register_console_command("setup_github", "Setup GitHub with personal access token", handle_setup_github)
        register_console_command("dev_status", "Show development module status", handle_dev_status)
        register_console_command("auto_commit", "Toggle auto-commit on/off", handle_auto_commit)
        register_console_command("auto_version", "Toggle auto-versioning on/off", handle_auto_version)
    
    setup_dev_console_commands()
    
    bot.logger.log(MODULE_NAME, f"✅ DEV module setup complete - v{bot.version}")