import os
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime
import discord
from discord import app_commands
import re
import asyncio
import time
import difflib
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

MODULE_NAME = "DEV"

# Configuration - all data files now stored in /data
VERSION_DATA_FILE = "version_data.json"
TRACKED_EXTENSIONS = ['.py']
# Exclude files that the bot writes to (prevents self-triggering)
EXCLUDE_FILES = [
    '_version.py',           # Version file
    'version_data.json',     # Version history
    'song_index.json',       # Archive index
    'cache_index.json',      # Archive cache
    'moderation_strikes.json',  # Moderation data
    'member_roles.json',     # Role persistence
    '.gitignore',            # Git ignore file
    '.gitattributes',        # Git attributes
    '*.pyc',                 # Python bytecode
    '*.pyo',
    '*.pyd'
]
EXCLUDE_DIRS = ['Winpython64', 'python-3', 'venv', 'env', '.git', '__pycache__', 'data', 'icons']

# Enhanced semantic versioning thresholds (actual lines changed, not estimated)
BREAKING_THRESHOLD = 500    # Breaking changes (major version bump)
MAJOR_THRESHOLD = 100       # Major features (minor version bump)
MINOR_THRESHOLD = 20        # Minor features (patch version bump)
PATCH_THRESHOLD = 1         # Bug fixes (micro version bump)


class FileChangeHandler(FileSystemEventHandler):
    """Handles file system events for version detection (no hot-reload)"""
    
    def __init__(self, dev_manager, loop):
        self.dev_manager = dev_manager
        self.loop = loop
        self.last_check_time = {}
        self.check_cooldown = 5  # 5 seconds between version checks
        self.file_states = {}  # Track file size and mtime to detect real changes
        self.last_event_time = 0
        self.event_cooldown = 1  # Minimum 1 second between events for same file
    
    def should_ignore_file(self, file_path):
        """Check if file should be ignored for version tracking"""
        file_name = file_path.name
        
        # Ignore all excluded files
        for exclude in EXCLUDE_FILES:
            if exclude.startswith('*'):
                if file_name.endswith(exclude[1:]):
                    return True
            elif file_name == exclude:
                return True
        
        # Ignore directories
        path_str = str(file_path)
        for exclude_dir in EXCLUDE_DIRS:
            if exclude_dir in path_str:
                return True
        
        return False
    
    def on_modified(self, event):
        """Handle file modification events - trigger version check only"""
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        
        # Only track Python files in the bot directory
        if file_path.suffix != '.py':
            return
        
        # Use the improved ignore check
        if self.should_ignore_file(file_path):
            return
        
        # Check if file actually changed (size or mtime)
        try:
            current_stat = file_path.stat()
            current_size = current_stat.st_size
            current_mtime = current_stat.st_mtime
            
            # Get previous state
            prev_state = self.file_states.get(str(file_path))
            
            # If we have a previous state and nothing changed, ignore
            if prev_state and prev_state[0] == current_size and prev_state[1] == current_mtime:
                return
            
            # Update state
            self.file_states[str(file_path)] = (current_size, current_mtime)
            
        except Exception as e:
            self.dev_manager.bot.logger.log(MODULE_NAME, 
                f"Could not check file state for {file_path.name}", "WARNING")
        
        # Global cooldown to prevent rapid-fire events
        now = time.time()
        if now - self.last_event_time < self.event_cooldown:
            return
        self.last_event_time = now
        
        # Per-file cooldown
        file_key = str(file_path)
        last_check = self.last_check_time.get(file_key, 0)
        
        if now - last_check < self.check_cooldown:
            return
        
        self.last_check_time[file_key] = now
        
        # Schedule version check (not reload)
        asyncio.run_coroutine_threadsafe(
            self.dev_manager.check_for_changes_async(),
            self.loop
        )


class DevManager:
    """Manages development mode: versioning and GitHub integration"""
    
    def __init__(self, bot):
        self.bot = bot
        self.version_history = []
        self.file_hashes = {}
        self.file_contents = {}  # Store actual file contents for diff
        self.last_check_time = None
        self.git_enabled = False
        self.file_observer = None  # File system observer for version detection
        self.auto_commit_enabled = True  # DEFAULT TO ENABLED
        self.auto_versioning_enabled = True  # Default to enabled
        
        # Get data directory - create it in bot root
        bot_root = Path.cwd()
        self.data_dir = bot_root / "data"
        self.data_dir.mkdir(exist_ok=True)
        
        # Check Git availability
        self._check_git()
        
        # Load existing version data
        self._load_version_data()
        
        # Create .gitignore if it doesn't exist
        self._ensure_gitignore()
        
        # Log development features
        self._log_dev_features()
        
        # Start file monitoring for version detection
        self._start_file_monitoring()
    
    def _log_dev_features(self):
        """Log development-specific features that are enabled"""
        self.bot.logger.log(MODULE_NAME, "🔧 Development features enabled:")
        self.bot.logger.log(MODULE_NAME, "  • Automatic versioning (MAJOR.MINOR.PATCH.MICRO)")
        self.bot.logger.log(MODULE_NAME, "  • Git integration (commit & push)")
        self.bot.logger.log(MODULE_NAME, "  • File monitoring for version detection")
        self.bot.logger.log(MODULE_NAME, "  • Development console commands")
        self.bot.logger.log(MODULE_NAME, f"  • Auto-commit: {'ENABLED' if self.auto_commit_enabled else 'DISABLED'}")
        self.bot.logger.log(MODULE_NAME, f"  • Auto-versioning: {'ENABLED' if self.auto_versioning_enabled else 'DISABLED'}")
        self.bot.logger.log(MODULE_NAME, f"  • Data directory: {self.data_dir}")
        self.bot.logger.log(MODULE_NAME, f"  • Excluded files: {', '.join(EXCLUDE_FILES)}")
        self.bot.logger.log(MODULE_NAME, f"  • Excluded dirs: {', '.join(EXCLUDE_DIRS)}")
    
    def _start_file_monitoring(self):
        """Start the file system observer for version detection"""
        try:
            # Get the event loop
            loop = asyncio.get_event_loop()
            
            # Create and start the observer
            event_handler = FileChangeHandler(self, loop)
            self.file_observer = Observer()
            
            # Watch the current directory
            watch_path = Path.cwd()
            self.file_observer.schedule(event_handler, str(watch_path), recursive=True)
            self.file_observer.start()
            
            self.bot.logger.log(MODULE_NAME, f"✅ File monitoring started for: {watch_path}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to start file monitoring", e)
            self.file_observer = None
    
    async def check_for_changes_async(self):
        """Async wrapper for check_for_changes"""
        # Small delay to allow file writes to complete
        await asyncio.sleep(0.5)
        self.check_for_changes()
    
    def _check_git(self):
        """Check if Git is available and repository is initialized"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                self.git_enabled = True
                self.bot.logger.log(MODULE_NAME, "✅ Git repository detected")
                
                # Check if we have a remote configured
                remote_result = subprocess.run(
                    ['git', 'remote', '-v'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if remote_result.stdout.strip():
                    self.bot.logger.log(MODULE_NAME, "✅ Git remote configured")
                else:
                    self.bot.logger.log(MODULE_NAME, "⚠️ No Git remote configured - commits will be local only", "WARNING")
            else:
                self.git_enabled = False
                self.bot.logger.log(MODULE_NAME, "⚠️ Git not initialized - versioning will be local only", "WARNING")
                
        except FileNotFoundError:
            self.git_enabled = False
            self.bot.logger.log(MODULE_NAME, "⚠️ Git not found - versioning will be local only", "WARNING")
        except Exception as e:
            self.git_enabled = False
            self.bot.logger.error(MODULE_NAME, "Error checking Git status", e)
    
    def _load_version_data(self):
        """Load version history from data directory"""
        version_file = self.data_dir / VERSION_DATA_FILE
        
        if version_file.exists():
            try:
                with open(version_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.version_history = data.get('history', [])
                    self.file_hashes = data.get('hashes', {})
                    self.file_contents = data.get('contents', {})
                    self.last_check_time = data.get('last_check')
                    
                    self.bot.logger.log(MODULE_NAME, f"📚 Loaded {len(self.version_history)} version entries")
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, f"Failed to load version data from {version_file}", e)
        else:
            self.bot.logger.log(MODULE_NAME, "📝 No existing version data found, starting fresh")
            # Perform initial scan
            self._initial_scan()
    
    def _save_version_data(self):
        """Save version history to data directory"""
        version_file = self.data_dir / VERSION_DATA_FILE
        
        try:
            data = {
                'history': self.version_history,
                'hashes': self.file_hashes,
                'contents': self.file_contents,
                'last_check': datetime.now().isoformat()
            }
            
            with open(version_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to save version data to {version_file}", e)
    
    def _ensure_gitignore(self):
        """Create or update .gitignore to exclude data files and logs"""
        gitignore_path = Path('.gitignore')
        
        ignore_patterns = [
            '# Development data files',
            'data/',
            'version_data.json',
            '_version.py',
            '*.log',
            '',
            '# Python',
            '__pycache__/',
            '*.pyc',
            '*.pyo',
            '*.pyd',
            '',
            '# Environment',
            '.env',
            'venv/',
            'env/',
            '',
            '# Bot data',
            '*.json',
            '!package.json',
            ''
        ]
        
        try:
            if gitignore_path.exists():
                with open(gitignore_path, 'r', encoding='utf-8') as f:
                    existing = f.read()
                
                # Add missing patterns
                with open(gitignore_path, 'a', encoding='utf-8') as f:
                    for pattern in ignore_patterns:
                        if pattern and pattern not in existing:
                            f.write(f"{pattern}\n")
            else:
                with open(gitignore_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(ignore_patterns))
                
                self.bot.logger.log(MODULE_NAME, "✅ Created .gitignore")
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to create/update .gitignore", e)
    
    def _initial_scan(self):
        """Scan all tracked files initially"""
        bot_dir = Path.cwd()
        
        for file_path in bot_dir.rglob('*'):
            if not file_path.is_file():
                continue
            
            # Skip excluded files and directories
            if self._should_exclude(file_path):
                continue
            
            # Only track specified extensions
            if file_path.suffix not in TRACKED_EXTENSIONS:
                continue
            
            # Calculate hash
            file_hash = self._hash_file(file_path)
            if file_hash:
                relative_path = str(file_path.relative_to(bot_dir))
                self.file_hashes[relative_path] = file_hash
                
                # Store content for future diffs
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        self.file_contents[relative_path] = f.read()
                except:
                    pass
        
        self.bot.logger.log(MODULE_NAME, f"📂 Initial scan complete: tracking {len(self.file_hashes)} files")
        self._save_version_data()
    
    def _should_exclude(self, file_path: Path) -> bool:
        """Check if a file should be excluded from tracking"""
        file_name = file_path.name
        path_str = str(file_path)
        
        # Check excluded files
        for exclude in EXCLUDE_FILES:
            if exclude.startswith('*'):
                if file_name.endswith(exclude[1:]):
                    return True
            elif file_name == exclude:
                return True
        
        # Check excluded directories
        for exclude_dir in EXCLUDE_DIRS:
            if exclude_dir in path_str:
                return True
        
        return False
    
    def _hash_file(self, file_path: Path) -> str:
        """Calculate SHA256 hash of a file"""
        try:
            hasher = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to hash {file_path}", e)
            return None
    
    def check_for_changes(self):
        """Check for file changes and update version if needed"""
        if not self.auto_versioning_enabled:
            self.bot.logger.log(MODULE_NAME, "Auto-versioning is disabled, skipping check")
            return False
        
        bot_dir = Path.cwd()
        changes = {
            'added': [],
            'modified': [],
            'deleted': []
        }
        
        current_files = {}
        
        # Scan all files
        for file_path in bot_dir.rglob('*'):
            if not file_path.is_file():
                continue
            
            if self._should_exclude(file_path):
                continue
            
            if file_path.suffix not in TRACKED_EXTENSIONS:
                continue
            
            relative_path = str(file_path.relative_to(bot_dir))
            file_hash = self._hash_file(file_path)
            
            if not file_hash:
                continue
            
            current_files[relative_path] = file_hash
            
            # Check if file is new or modified
            if relative_path not in self.file_hashes:
                changes['added'].append(relative_path)
            elif self.file_hashes[relative_path] != file_hash:
                changes['modified'].append(relative_path)
        
        # Check for deleted files
        for old_file in self.file_hashes:
            if old_file not in current_files:
                changes['deleted'].append(old_file)
        
        # If there are changes, create a new version
        if any(changes.values()):
            self._create_version(changes, current_files)
            return True
        
        return False
    
    def _create_version(self, changes: dict, current_files: dict):
        """Create a new version based on detected changes"""
        # Calculate total lines changed
        total_lines = 0
        change_details = {}
        
        for file in changes['modified']:
            file_path = Path(file)
            if file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        new_content = f.read()
                    
                    old_content = self.file_contents.get(file, '')
                    
                    # Calculate actual line diff
                    old_lines = old_content.splitlines()
                    new_lines = new_content.splitlines()
                    
                    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=''))
                    
                    added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
                    removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
                    
                    total_lines += added + removed
                    
                    change_details[file] = {
                        'type': 'modified',
                        'added': added,
                        'removed': removed
                    }
                    
                    # Update stored content
                    self.file_contents[file] = new_content
                    
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, f"Failed to diff {file}", e)
        
        # Add new files
        for file in changes['added']:
            file_path = Path(file)
            if file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        lines = len(content.splitlines())
                        total_lines += lines
                        self.file_contents[file] = content
                        
                        change_details[file] = {
                            'type': 'added',
                            'lines': lines
                        }
                except:
                    pass
        
        # Handle deleted files
        for file in changes['deleted']:
            if file in self.file_contents:
                lines = len(self.file_contents[file].splitlines())
                total_lines += lines
                del self.file_contents[file]
                
                change_details[file] = {
                    'type': 'deleted',
                    'lines': lines
                }
        
        # Determine version bump type
        if total_lines >= BREAKING_THRESHOLD:
            change_type = 'BREAKING'
        elif total_lines >= MAJOR_THRESHOLD:
            change_type = 'MAJOR'
        elif total_lines >= MINOR_THRESHOLD:
            change_type = 'MINOR'
        else:
            change_type = 'MICRO'
        
        # Get current version
        current_version = self._get_current_version()
        major, minor, patch, micro = map(int, current_version.split('.'))
        
        # Bump version based on change type
        if change_type == 'BREAKING':
            major += 1
            minor = 0
            patch = 0
            micro = 0
        elif change_type == 'MAJOR':
            minor += 1
            patch = 0
            micro = 0
        elif change_type == 'MINOR':
            patch += 1
            micro = 0
        else:
            micro += 1
        
        new_version = f"{major}.{minor}.{patch}.{micro}"
        
        # Create version entry
        version_entry = {
            'version': new_version,
            'previous_version': current_version,
            'timestamp': datetime.now().isoformat(),
            'change_type': change_type,
            'lines_changed': total_lines,
            'files_changed': len(changes['added']) + len(changes['modified']) + len(changes['deleted']),
            'details': change_details
        }
        
        self.version_history.append(version_entry)
        self.file_hashes = current_files
        
        # Update _version.py
        self._update_version_file(new_version)
        
        # Update bot.version
        self.bot.version = new_version
        
        # Save version data
        self._save_version_data()
        
        # Log the version change
        self.bot.logger.log(MODULE_NAME, 
            f"🎯 Version bumped: v{current_version} → v{new_version} ({change_type})")
        self.bot.logger.log(MODULE_NAME, 
            f"   Files: {len(changes['added'])} added, {len(changes['modified'])} modified, {len(changes['deleted'])} deleted")
        self.bot.logger.log(MODULE_NAME, 
            f"   Lines changed: {total_lines}")
        
        # Commit to Git if enabled
        if self.auto_commit_enabled and self.git_enabled:
            self._commit_changes(new_version, change_type, changes)
    
    def _get_current_version(self) -> str:
        """Get the current version from version history or default"""
        if self.version_history:
            return self.version_history[-1]['version']
        return "0.0.0.0"
    
    def _update_version_file(self, version: str):
        """Update the _version.py file"""
        version_file = Path('_version.py')
        
        try:
            with open(version_file, 'w', encoding='utf-8') as f:
                f.write(f'__version__ = "{version}"\n')
            
            self.bot.logger.log(MODULE_NAME, f"✅ Updated _version.py to {version}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to update _version.py", e)
    
    def _commit_changes(self, version: str, change_type: str, changes: dict):
        """Commit changes to Git and push to remote"""
        try:
            # Add all changed files
            all_changed = changes['added'] + changes['modified']
            
            if all_changed:
                subprocess.run(['git', 'add'] + all_changed, timeout=10, check=True)
            
            # Also add the version file
            subprocess.run(['git', 'add', '_version.py'], timeout=10, check=True)
            
            # Create commit message
            commit_msg = f"v{version} - {change_type} update\n\n"
            if changes['added']:
                commit_msg += f"Added: {', '.join(changes['added'])}\n"
            if changes['modified']:
                commit_msg += f"Modified: {', '.join(changes['modified'])}\n"
            if changes['deleted']:
                commit_msg += f"Deleted: {', '.join(changes['deleted'])}\n"
            
            # Commit
            result = subprocess.run(
                ['git', 'commit', '-m', commit_msg],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                self.bot.logger.log(MODULE_NAME, f"✅ Committed v{version} to Git")
                
                # Try to push
                push_result = subprocess.run(
                    ['git', 'push'],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if push_result.returncode == 0:
                    self.bot.logger.log(MODULE_NAME, "✅ Pushed to remote repository")
                else:
                    self.bot.logger.log(MODULE_NAME, 
                        f"⚠️ Failed to push: {push_result.stderr}", "WARNING")
            else:
                self.bot.logger.log(MODULE_NAME, 
                    f"⚠️ Failed to commit: {result.stderr}", "WARNING")
                
        except subprocess.TimeoutExpired:
            self.bot.logger.log(MODULE_NAME, "⚠️ Git operation timed out", "WARNING")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to commit changes", e)
    
    def manual_commit(self, message: str = None):
        """Manually commit current state to Git"""
        if not self.git_enabled:
            self.bot.logger.log(MODULE_NAME, "❌ Git is not enabled", "ERROR")
            return False
        
        try:
            # Check for changes
            status_result = subprocess.run(
                ['git', 'status', '--porcelain'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if not status_result.stdout.strip():
                self.bot.logger.log(MODULE_NAME, "✅ No changes to commit")
                return True
            
            # Add all changes
            subprocess.run(['git', 'add', '-A'], timeout=10, check=True)
            
            # Create commit message
            if not message:
                current_version = self._get_current_version()
                message = f"v{current_version} - Manual commit"
            
            # Commit
            result = subprocess.run(
                ['git', 'commit', '-m', message],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                self.bot.logger.log(MODULE_NAME, f"✅ Committed: {message}")
                
                # Try to push
                push_result = subprocess.run(
                    ['git', 'push'],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if push_result.returncode == 0:
                    self.bot.logger.log(MODULE_NAME, "✅ Pushed to remote repository")
                    return True
                else:
                    self.bot.logger.log(MODULE_NAME, 
                        f"⚠️ Failed to push: {push_result.stderr}", "WARNING")
                    return False
            else:
                self.bot.logger.log(MODULE_NAME, 
                    f"❌ Failed to commit: {result.stderr}", "ERROR")
                return False
                
        except subprocess.TimeoutExpired:
            self.bot.logger.log(MODULE_NAME, "❌ Git operation timed out", "ERROR")
            return False
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to commit", e)
            return False
    
    def setup_github_with_token(self, token: str) -> bool:
        """Setup GitHub authentication with a personal access token"""
        try:
            # Get current remote URL
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                self.bot.logger.log(MODULE_NAME, "❌ No Git remote 'origin' found", "ERROR")
                return False
            
            current_url = result.stdout.strip()
            
            # Parse the URL to get repo info
            if 'github.com' in current_url:
                # Extract username and repo name
                match = re.search(r'github\.com[:/]([^/]+)/(.+?)(?:\.git)?$', current_url)
                if match:
                    username, repo = match.groups()
                    
                    # Create new URL with token
                    new_url = f"https://{token}@github.com/{username}/{repo}.git"
                    
                    # Update remote URL
                    subprocess.run(
                        ['git', 'remote', 'set-url', 'origin', new_url],
                        timeout=5,
                        check=True
                    )
                    
                    self.bot.logger.log(MODULE_NAME, "✅ GitHub authentication configured")
                    return True
                else:
                    self.bot.logger.log(MODULE_NAME, "❌ Could not parse GitHub URL", "ERROR")
                    return False
            else:
                self.bot.logger.log(MODULE_NAME, "❌ Remote is not a GitHub repository", "ERROR")
                return False
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to setup GitHub", e)
            return False
    
    def cleanup(self):
        """Cleanup resources (stop file observer)"""
        if self.file_observer:
            try:
                self.file_observer.stop()
                self.file_observer.join(timeout=5)
                self.bot.logger.log(MODULE_NAME, "✅ File monitoring stopped")
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Error stopping file observer", e)
    
    def get_version_info(self) -> dict:
        """Get comprehensive version information"""
        current_version = self._get_current_version()
        
        info = {
            'current_version': current_version,
            'total_versions': len(self.version_history),
            'tracked_files': len(self.file_hashes),
            'git_enabled': self.git_enabled,
            'auto_commit_enabled': self.auto_commit_enabled,
            'auto_versioning_enabled': self.auto_versioning_enabled,
            'file_monitoring_active': self.file_observer is not None and self.file_observer.is_alive(),
            'data_directory': str(self.data_dir),
            'last_update': None
        }
        
        if self.version_history:
            last_entry = self.version_history[-1]
            info['last_update'] = {
                'version': last_entry['version'],
                'timestamp': last_entry['timestamp'],
                'type': last_entry['change_type'],
                'files_changed': last_entry['files_changed'],
                'lines_changed': last_entry['lines_changed']
            }
        
        return info


def setup(bot, register_console_command):
    """Setup the development module"""
    # Set bot version from version file
    version_file = Path('_version.py')
    if version_file.exists():
        with open(version_file, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            bot.version = match.group(1)
    else:
        bot.version = "0.0.0.0"
    
    # Create DevManager
    dev_manager = DevManager(bot)
    
    # Make dev_manager accessible to other modules
    bot.dev_manager = dev_manager
    
    # Slash command for version info
    @bot.tree.command(name="version", description="Show bot version information")
    async def version_command(interaction: discord.Interaction):
        """Display current version and recent changes"""
        info = dev_manager.get_version_info()
        
        embed = discord.Embed(
            title="🤖 Embot Version Information",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="Current Version",
            value=f"v{info['current_version']}",
            inline=True
        )
        
        embed.add_field(
            name="Total Versions",
            value=str(info['total_versions']),
            inline=True
        )
        
        embed.add_field(
            name="Tracked Files",
            value=str(info['tracked_files']),
            inline=True
        )
        
        if info['last_update']:
            last = info['last_update']
            timestamp = datetime.fromisoformat(last['timestamp']).strftime('%Y-%m-%d %H:%M')
            
            embed.add_field(
                name="Last Update",
                value=f"v{last['version']} ({last['type']})\n{timestamp}",
                inline=False
            )
            
            embed.add_field(
                name="Changes",
                value=f"{last['files_changed']} files, {last['lines_changed']} lines",
                inline=False
            )
        
        embed.set_footer(text="Development Mode Active")
        
        await interaction.response.send_message(embed=embed)
    
    # Setup console commands
    def setup_dev_console_commands():
        """Setup development-specific console commands"""
        
        async def handle_commit(args):
            """Manual commit command"""
            message = args.strip() if args.strip() else None
            
            print("🔄 Committing changes...")
            success = dev_manager.manual_commit(message)
            
            if success:
                print("✅ Changes committed and pushed successfully")
            else:
                print("❌ Failed to commit changes")
        
        async def handle_changelog(args):
            """Show version changelog"""
            count = 10  # Default to last 10 versions
            
            if args.strip():
                try:
                    count = int(args.strip())
                except ValueError:
                    print("⚠️ Invalid number, showing last 10 versions")
            
            recent = dev_manager.version_history[-count:]
            
            if not recent:
                print("📝 No version history available")
                return
            
            print(f"\n📚 Version History (Last {len(recent)} versions):\n")
            
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
            print(f"│ File Monitoring: {'✅ Active' if info['file_monitoring_active'] else '❌ Inactive':<43} │")
            print(f"│ Auto-Commit: {'✅ Enabled' if info['auto_commit_enabled'] else '❌ Disabled':<45} │")
            print(f"│ Auto-Versioning: {'✅ Enabled' if info['auto_versioning_enabled'] else '❌ Disabled':<41} │")
            print(f"│ Data Directory: {info['data_directory']:<44} │")
            
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
                status = "ENABLED"
                print("✅ Auto-commit ENABLED")
            elif args.strip().lower() in ['off', 'disable', 'false', '0']:
                dev_manager.auto_commit_enabled = False
                status = "DISABLED"
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
                status = "ENABLED"
                print("✅ Auto-versioning ENABLED")
            elif args.strip().lower() in ['off', 'disable', 'false', '0']:
                dev_manager.auto_versioning_enabled = False
                status = "DISABLED"
                print("✅ Auto-versioning DISABLED")
            else:
                # Toggle if no args
                dev_manager.auto_versioning_enabled = not dev_manager.auto_versioning_enabled
                status = "ENABLED" if dev_manager.auto_versioning_enabled else "DISABLED"
                print(f"✅ Auto-versioning {status}")
            
            dev_manager.bot.logger.log(MODULE_NAME, f"Auto-versioning {status}")
        
        async def handle_scan(args):
            """Manually trigger a file scan and version check"""
            print("🔍 Scanning for changes...")
            changes_found = dev_manager.check_for_changes()
            
            if changes_found:
                print("✅ Changes detected and version updated")
            else:
                print("✅ No changes detected")
        
        # Register development console commands
        register_console_command("commit", "Commit and push changes", handle_commit)
        register_console_command("changelog", "Show version changelog", handle_changelog)
        register_console_command("git", "Show git repository status", handle_git)
        register_console_command("files", "Show tracked files", handle_files)
        register_console_command("setup_github", "Setup GitHub with personal access token", handle_setup_github)
        register_console_command("dev_status", "Show development module status", handle_dev_status)
        register_console_command("auto_commit", "Toggle auto-commit on/off", handle_auto_commit)
        register_console_command("auto_version", "Toggle auto-versioning on/off", handle_auto_version)
        register_console_command("scan", "Manually scan for file changes", handle_scan)
    
    setup_dev_console_commands()
    
    bot.logger.log(MODULE_NAME, f"✅ DEV module setup complete - v{bot.version}")