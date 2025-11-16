import os
import hashlib
import json
from pathlib import Path
from datetime import datetime
import discord
from discord import app_commands
import re

MODULE_NAME = "VERSION"

# Configuration
VERSION_DATA_FILE = "version_data.json"
TRACKED_EXTENSIONS = ['.py']
EXCLUDE_FILES = ['version.py', '__pycache__']
EXCLUDE_DIRS = ['Winpython64', 'python-3', 'venv', 'env', '.git', '__pycache__', 'data', 'icons']

# Semantic versioning thresholds (lines changed)
MAJOR_THRESHOLD = 500   # Major changes (breaking changes, complete rewrites)
MINOR_THRESHOLD = 100   # Minor changes (new features, significant updates)
PATCH_THRESHOLD = 1     # Patch changes (bug fixes, small tweaks)


class VersionManager:
    """Manages automatic semantic versioning based on code changes"""
    
    def __init__(self, bot):
        self.bot = bot
        self.current_version = "3.0.0"
        self.version_history = []
        self.file_hashes = {}
        self.last_check_time = None
        
        # Load existing version data
        self._load_version_data()
        
        # Don't check version in __init__ - do it in background
        # This prevents blocking the event loop during startup
    
    def _load_version_data(self):
        """Load version data from file"""
        try:
            if Path(VERSION_DATA_FILE).exists():
                with open(VERSION_DATA_FILE, 'r') as f:
                    data = json.load(f)
                    self.current_version = data.get('current_version', '3.0.0')
                    self.version_history = data.get('history', [])
                    self.file_hashes = data.get('file_hashes', {})
                    self.last_check_time = data.get('last_check_time')
                    self.bot.logger.log(MODULE_NAME, f"Loaded version data: v{self.current_version}")
            else:
                self.bot.logger.log(MODULE_NAME, "No version data found, starting fresh at v3.0.0")
                self._save_version_data()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load version data", e)
    
    def _save_version_data(self):
        """Save version data to file"""
        try:
            data = {
                'current_version': self.current_version,
                'history': self.version_history,
                'file_hashes': self.file_hashes,
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
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to hash {file_path}", e)
            return None
    
    def _count_lines_in_file(self, file_path):
        """Count non-empty lines in a file"""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return sum(1 for line in f if line.strip())
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to count lines in {file_path}", e)
            return 0
    
    def _should_exclude_file(self, file_path):
        """Check if a file should be excluded from tracking"""
        file_name = file_path.name
        path_str = str(file_path)
        
        # Exclude specific files
        for exclude in EXCLUDE_FILES:
            if exclude in path_str:
                return True
        
        # Exclude specific directories
        for exclude_dir in EXCLUDE_DIRS:
            if exclude_dir in path_str:
                return True
        
        return False
    
    def _scan_codebase(self):
        """Scan all Python files and return their hashes and line counts"""
        current_hashes = {}
        file_lines = {}
        
        bot_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        
        # Only scan the immediate bot directory, not subdirectories
        # This prevents scanning Python installations and other large directories
        for file_path in bot_dir.glob('*.py'):
            if file_path.is_file():
                if self._should_exclude_file(file_path):
                    continue
                
                relative_path = file_path.name
                file_hash = self._get_file_hash(file_path)
                
                if file_hash:
                    current_hashes[relative_path] = file_hash
                    file_lines[relative_path] = self._count_lines_in_file(file_path)
        
        return current_hashes, file_lines
    
    def _calculate_changes(self, old_hashes, new_hashes, file_lines):
        """Calculate the extent of changes between two versions"""
        changes = {
            'added': [],
            'modified': [],
            'deleted': [],
            'total_lines_changed': 0,
            'files_changed': 0
        }
        
        # Find added and modified files
        for file_path, new_hash in new_hashes.items():
            if file_path not in old_hashes:
                changes['added'].append(file_path)
                changes['total_lines_changed'] += file_lines.get(file_path, 0)
                changes['files_changed'] += 1
            elif old_hashes[file_path] != new_hash:
                changes['modified'].append(file_path)
                # Estimate: assume 30% of file changed on average for modified files
                changes['total_lines_changed'] += int(file_lines.get(file_path, 0) * 0.3)
                changes['files_changed'] += 1
        
        # Find deleted files
        for file_path in old_hashes:
            if file_path not in new_hashes:
                changes['deleted'].append(file_path)
                changes['files_changed'] += 1
        
        return changes
    
    def _increment_version(self, lines_changed):
        """Increment version based on lines changed"""
        major, minor, patch = map(int, self.current_version.split('.'))
        
        if lines_changed >= MAJOR_THRESHOLD:
            major += 1
            minor = 0
            patch = 0
            change_type = "MAJOR"
        elif lines_changed >= MINOR_THRESHOLD:
            minor += 1
            patch = 0
            change_type = "MINOR"
        elif lines_changed >= PATCH_THRESHOLD:
            patch += 1
            change_type = "PATCH"
        else:
            return self.current_version, "NONE"
        
        new_version = f"{major}.{minor}.{patch}"
        return new_version, change_type
    
    def _update_main_py_version(self, new_version):
        """Update the VERSION constant in main.py"""
        try:
            main_py_path = Path(os.path.dirname(os.path.abspath(__file__))) / "main.py"
            
            if not main_py_path.exists():
                self.bot.logger.log(MODULE_NAME, "main.py not found", "WARNING")
                return False
            
            with open(main_py_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check if VERSION already exists
            version_pattern = r'^VERSION\s*=\s*["\'][\d.]+["\']\s*$'
            
            if re.search(version_pattern, content, re.MULTILINE):
                # Update existing VERSION
                new_content = re.sub(
                    version_pattern,
                    f'VERSION = "{new_version}"',
                    content,
                    flags=re.MULTILINE
                )
            else:
                # Add VERSION after imports, before intents
                import_end = content.find('# Initialize bot with intents')
                if import_end == -1:
                    import_end = content.find('intents = discord.Intents')
                
                if import_end != -1:
                    new_content = (
                        content[:import_end] +
                        f'# Bot version (auto-managed by version.py)\n'
                        f'VERSION = "{new_version}"\n\n' +
                        content[import_end:]
                    )
                else:
                    # Fallback: add at top after imports
                    lines = content.split('\n')
                    insert_pos = 0
                    for i, line in enumerate(lines):
                        if line.startswith('import ') or line.startswith('from '):
                            insert_pos = i + 1
                    
                    lines.insert(insert_pos, '')
                    lines.insert(insert_pos + 1, '# Bot version (auto-managed by version.py)')
                    lines.insert(insert_pos + 2, f'VERSION = "{new_version}"')
                    new_content = '\n'.join(lines)
            
            with open(main_py_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            self.bot.logger.log(MODULE_NAME, f"Updated VERSION in main.py to {new_version}")
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to update main.py", e)
            return False
    
    def _check_and_update_version(self):
        """Check for changes and update version if necessary"""
        try:
            self.bot.logger.log(MODULE_NAME, "Scanning codebase for changes...")
            
            current_hashes, file_lines = self._scan_codebase()
            
            self.bot.logger.log(MODULE_NAME, f"Scanned {len(current_hashes)} files")
            
            # If no previous hashes, this is first run
            if not self.file_hashes:
                self.bot.logger.log(MODULE_NAME, f"First run detected, baseline set at v{self.current_version}")
                self.file_hashes = current_hashes
                self._save_version_data()
                self._update_main_py_version(self.current_version)
                return
            
            # Calculate changes
            changes = self._calculate_changes(self.file_hashes, current_hashes, file_lines)
            
            if changes['files_changed'] == 0:
                self.bot.logger.log(MODULE_NAME, "No changes detected")
                return
            
            # Log changes
            self.bot.logger.log(MODULE_NAME, 
                f"Changes detected: {changes['files_changed']} files, ~{changes['total_lines_changed']} lines")
            
            if changes['added']:
                self.bot.logger.log(MODULE_NAME, f"Added: {', '.join(changes['added'])}")
            if changes['modified']:
                self.bot.logger.log(MODULE_NAME, f"Modified: {', '.join(changes['modified'])}")
            if changes['deleted']:
                self.bot.logger.log(MODULE_NAME, f"Deleted: {', '.join(changes['deleted'])}")
            
            # Increment version
            old_version = self.current_version
            new_version, change_type = self._increment_version(changes['total_lines_changed'])
            
            if change_type == "NONE":
                self.bot.logger.log(MODULE_NAME, "Changes too minor to increment version")
                # Still update hashes
                self.file_hashes = current_hashes
                self._save_version_data()
                return
            
            # Update version
            self.current_version = new_version
            
            # Add to history
            history_entry = {
                'version': new_version,
                'previous_version': old_version,
                'change_type': change_type,
                'timestamp': datetime.utcnow().isoformat(),
                'files_changed': changes['files_changed'],
                'lines_changed': changes['total_lines_changed'],
                'added': changes['added'],
                'modified': changes['modified'],
                'deleted': changes['deleted']
            }
            
            self.version_history.append(history_entry)
            
            # Keep only last 50 entries
            if len(self.version_history) > 50:
                self.version_history = self.version_history[-50:]
            
            # Update file hashes
            self.file_hashes = current_hashes
            
            # Save data
            self._save_version_data()
            
            # Update main.py
            self._update_main_py_version(new_version)
            
            self.bot.logger.log(MODULE_NAME, 
                f"Version updated: v{old_version} → v{new_version} ({change_type})")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error checking version", e)
    
    async def send_startup_banner(self):
        """Send version info to bot-logs channel on startup"""
        try:
            channel = None
            for guild in self.bot.guilds:
                channel = discord.utils.get(guild.text_channels, name="bot-logs")
                if channel:
                    break
            
            if not channel:
                return
            
            embed = discord.Embed(
                title="🤖 Bot Started",
                description=f"**Embot v{self.current_version}**",
                color=0x57f287,
                timestamp=datetime.utcnow()
            )
            
            if self.version_history:
                last_update = self.version_history[-1]
                
                embed.add_field(
                    name="Latest Update",
                    value=f"v{last_update['previous_version']} → v{last_update['version']}",
                    inline=True
                )
                
                embed.add_field(
                    name="Change Type",
                    value=last_update['change_type'],
                    inline=True
                )
                
                embed.add_field(
                    name="Changes",
                    value=f"{last_update['files_changed']} files, ~{last_update['lines_changed']} lines",
                    inline=True
                )
                
                timestamp = datetime.fromisoformat(last_update['timestamp'])
                embed.add_field(
                    name="Updated",
                    value=timestamp.strftime("%Y-%m-%d %H:%M UTC"),
                    inline=False
                )
            
            embed.set_footer(text="Automatic Semantic Versioning System")
            
            await channel.send(embed=embed)
            self.bot.logger.log(MODULE_NAME, "Sent startup banner")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send startup banner", e)
    
    def get_version_info(self):
        """Get formatted version information"""
        info = {
            'current_version': self.current_version,
            'total_versions': len(self.version_history) + 1,  # +1 for initial 3.0.0
            'tracked_files': len(self.file_hashes),
            'last_update': None
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


def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up version manager module")
    
    version_manager = VersionManager(bot)
    bot.version_manager = version_manager
    
    # Make version accessible globally
    bot.version = version_manager.current_version
    
    # Run version check in background to avoid blocking
    async def check_version_background():
        await bot.wait_until_ready()
        await asyncio.sleep(1)  # Give bot time to fully initialize
        
        bot.logger.log(MODULE_NAME, "Starting background version check...")
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, version_manager._check_and_update_version)
        
        bot.logger.log(MODULE_NAME, f"Version check complete - v{version_manager.current_version}")
        
        # Update bot.version after check
        bot.version = version_manager.current_version
        
        # Send startup banner after version check
        await asyncio.sleep(1)
        await version_manager.send_startup_banner()
    
    # Schedule background version check
    import asyncio
    asyncio.create_task(check_version_background())
    
    @bot.tree.command(name="version", description="Show bot version and changelog")
    async def version_cmd(interaction: discord.Interaction):
        """Display version information"""
        try:
            info = version_manager.get_version_info()
            
            embed = discord.Embed(
                title=f"🤖 Embot v{info['current_version']}",
                color=0x5865f2,
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="Version Info",
                value=f"Current: **v{info['current_version']}**\n"
                      f"Total Versions: {info['total_versions']}\n"
                      f"Tracked Files: {info['tracked_files']}",
                inline=False
            )
            
            if info['last_update']:
                last = info['last_update']
                timestamp = datetime.fromisoformat(last['timestamp'])
                
                embed.add_field(
                    name="Latest Update",
                    value=f"Type: **{last['type']}**\n"
                          f"Changes: {last['files_changed']} files, ~{last['lines_changed']} lines\n"
                          f"Date: {timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
                    inline=False
                )
            
            # Show recent changelog
            if version_manager.version_history:
                recent = version_manager.version_history[-5:]
                changelog = []
                
                for entry in reversed(recent):
                    timestamp = datetime.fromisoformat(entry['timestamp'])
                    changelog.append(
                        f"**v{entry['version']}** ({entry['change_type']}) - "
                        f"{timestamp.strftime('%Y-%m-%d')}"
                    )
                
                embed.add_field(
                    name="Recent Changes",
                    value="\n".join(changelog),
                    inline=False
                )
            
            embed.add_field(
                name="Versioning Thresholds",
                value=f"🔴 Major: {MAJOR_THRESHOLD}+ lines\n"
                      f"🟡 Minor: {MINOR_THRESHOLD}+ lines\n"
                      f"🟢 Patch: {PATCH_THRESHOLD}+ lines",
                inline=False
            )
            
            embed.set_footer(text="Automatic Semantic Versioning")
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Version command failed", e)
            await interaction.response.send_message(
                "❌ Failed to retrieve version information",
                ephemeral=True
            )
    
    @bot.tree.command(name="changelog", description="View detailed version history")
    @app_commands.describe(count="Number of versions to show (default: 10)")
    async def changelog(interaction: discord.Interaction, count: int = 10):
        """Display detailed changelog"""
        try:
            count = max(1, min(25, count))
            
            if not version_manager.version_history:
                await interaction.response.send_message(
                    "No version history available yet.",
                    ephemeral=True
                )
                return
            
            recent = version_manager.version_history[-count:]
            
            embed = discord.Embed(
                title="📋 Version Changelog",
                description=f"Showing last {len(recent)} version(s)",
                color=0x5865f2,
                timestamp=datetime.utcnow()
            )
            
            for entry in reversed(recent):
                timestamp = datetime.fromisoformat(entry['timestamp'])
                
                change_details = []
                if entry['added']:
                    change_details.append(f"➕ Added: {len(entry['added'])} files")
                if entry['modified']:
                    change_details.append(f"📝 Modified: {len(entry['modified'])} files")
                if entry['deleted']:
                    change_details.append(f"➖ Deleted: {len(entry['deleted'])} files")
                
                value = (
                    f"**{entry['change_type']}** update from v{entry['previous_version']}\n"
                    f"{', '.join(change_details)}\n"
                    f"~{entry['lines_changed']} lines changed\n"
                    f"*{timestamp.strftime('%Y-%m-%d %H:%M UTC')}*"
                )
                
                embed.add_field(
                    name=f"v{entry['version']}",
                    value=value,
                    inline=False
                )
            
            embed.set_footer(text=f"Current version: v{version_manager.current_version}")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Changelog command failed", e)
            await interaction.response.send_message(
                "❌ Failed to retrieve changelog",
                ephemeral=True
            )
    
    bot.logger.log(MODULE_NAME, f"Version manager setup complete - v{version_manager.current_version}")