import discord
from discord import app_commands
import re
import json
import hashlib
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple
import aiohttp
from dataclasses import dataclass, asdict
from enum import Enum

MODULE_NAME = "COMMUNITY"

# Channel configuration
PROJECTS_CHANNEL_NAME = "projects"
ARTWORK_CHANNEL_NAME = "artwork"

# Reaction emojis
REACTION_FIRE = "🔥"
REACTION_NEUTRAL = "😐"
REACTION_TRASH = "🗑️"
REACTION_STAR = "⭐"

# XP values
XP_VALUES = {
    REACTION_FIRE: 5,
    REACTION_NEUTRAL: 0,
    REACTION_TRASH: -5,
    REACTION_STAR: 10
}

# Database file
DB_FILE = Path("data/community_submissions.json")


class SubmissionType(Enum):
    """Submission type enumeration"""
    PROJECT = "project"
    ARTWORK = "artwork"


@dataclass
class Submission:
    """Unified submission object for projects and artwork"""
    project_id: str
    message_id: str
    user_id: str
    submission_type: SubmissionType
    version: str
    title: Optional[str]
    description: Optional[str]
    media_links: List[str]
    thumbnail: Optional[str]
    file_hashes: List[str]
    votes: Dict[str, int]
    user_votes: Dict[str, str]  # user_id -> emoji (tracks first vote only)
    thread_message_count: int
    created_at: str
    updated_at: str
    last_voted_at: Optional[str]
    last_thread_message_at: Optional[str]
    is_deleted: bool
    channel_id: Optional[str] = None  # For lazy deletion checks
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        data['submission_type'] = self.submission_type.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Submission':
        """Create from dictionary, filtering out unexpected fields"""
        # Get valid field names from the dataclass
        import inspect
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        
        # Filter data to only include valid fields
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        
        # Convert submission_type back to enum
        if 'submission_type' in filtered_data:
            filtered_data['submission_type'] = SubmissionType(filtered_data['submission_type'])
        
        return cls(**filtered_data)
    
    def calculate_xp(self) -> int:
        """Calculate total XP for this submission"""
        return sum(count * XP_VALUES.get(emoji, 0) for emoji, count in self.votes.items())
    
    def update_media(self, media_links: List[str], thumbnail: Optional[str], file_hashes: List[str]):
        """Update media content"""
        self.media_links = media_links
        self.thumbnail = thumbnail
        self.file_hashes = file_hashes
        self.updated_at = datetime.now().isoformat()
    
    def update_content(self, title: Optional[str], description: Optional[str]):
        """Update text content"""
        self.title = title
        self.description = description
        self.updated_at = datetime.now().isoformat()
    
    def increment_version(self) -> str:
        """Increment version number and return new version"""
        major, minor = map(int, self.version.split('.'))
        self.version = f"{major + 1}.0"
        self.updated_at = datetime.now().isoformat()
        return self.version
    
    def mark_deleted(self):
        """Mark submission as deleted"""
        self.is_deleted = True
        self.updated_at = datetime.now().isoformat()
    
    def record_vote(self, user_id: str, emoji: str):
        """Record a user's vote"""
        # Only record first vote
        if user_id not in self.user_votes:
            self.user_votes[user_id] = emoji
        self.last_voted_at = datetime.now().isoformat()
    
    def has_voted(self, user_id: str) -> bool:
        """Check if user has already voted"""
        return user_id in self.user_votes


class SubmissionDatabase:
    """Manages submission data persistence"""
    
    def __init__(self, bot):
        self.bot = bot
        self.data = {
            "submissions": {},  # message_id -> submission dict
            "user_projects": {},  # user_id -> list of project_ids
            "file_hashes": {},  # file_hash -> (user_id, message_id, project_id)
            "link_registry": {},  # link -> (user_id, message_id, project_id)
            "sticky_messages": {},  # channel_id -> message_id
            "project_versions": {}  # (user_id, title_hash) -> list of message_ids
        }
        self._load()
    
    def _load(self):
        """Load database from file"""
        try:
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            if DB_FILE.exists():
                with open(DB_FILE, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                self.bot.logger.log(MODULE_NAME, f"Loaded {len(self.data['submissions'])} submissions from database")
            else:
                self._save()
                self.bot.logger.log(MODULE_NAME, "Created new submission database")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load database", e)
    
    def _save(self):
        """Save database to file"""
        try:
            with open(DB_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save database", e)
    
    def _get_title_hash(self, title: str) -> str:
        """Generate short hash for title (for version detection and IDs)"""
        # Use first 8 characters of MD5 for compact IDs
        return hashlib.md5(title.lower().strip().encode()).hexdigest()[:8]
    
    def add_submission(self, message_id: str, user_id: str, submission_type: SubmissionType, 
                      title: Optional[str], description: Optional[str], 
                      media_links: List[str], thumbnail: Optional[str],
                      file_hashes: List[str], channel_id: Optional[str] = None) -> Tuple[Submission, bool]:
        """
        Add a new submission to the database
        For versioned projects, updates the existing entry instead of creating duplicate
        Returns: (Submission object, is_new_version)
        """
        is_new_version = False
        version = "1.0"
        existing_submission = None
        
        # Generate compact project ID
        if submission_type == SubmissionType.PROJECT and title:
            title_hash = self._get_title_hash(title)
            project_id = f"p-{user_id[-6:]}-{title_hash}"  # Compact: p-123456-abc12345
            version_key = f"{user_id}:{title_hash}"
            
            # Check for existing versions
            if version_key in self.data["project_versions"]:
                is_new_version = True
                existing_message_ids = self.data["project_versions"][version_key]
                
                # Get the latest version and its submission
                latest_version = "1.0"
                for msg_id in existing_message_ids:
                    if msg_id in self.data["submissions"]:
                        sub = Submission.from_dict(self.data["submissions"][msg_id])
                        if sub.version > latest_version:
                            latest_version = sub.version
                            existing_submission = sub
                
                # Increment version
                major, minor = map(int, latest_version.split('.'))
                version = f"{major + 1}.0"
                
                # Add to version list
                self.data["project_versions"][version_key].append(message_id)
            else:
                # First version
                self.data["project_versions"][version_key] = [message_id]
        else:
            # Artwork gets simple ID
            project_id = f"a-{user_id[-6:]}-{message_id[-8:]}"  # Compact: a-123456-abc12345
        
        submission = Submission(
            project_id=project_id,
            message_id=message_id,
            user_id=user_id,
            submission_type=submission_type,
            version=version,
            title=title,
            description=description,
            media_links=media_links,
            thumbnail=thumbnail,
            file_hashes=file_hashes,
            votes=existing_submission.votes if existing_submission else {REACTION_FIRE: 0, REACTION_NEUTRAL: 0, REACTION_TRASH: 0, REACTION_STAR: 0},
            user_votes=existing_submission.user_votes if existing_submission else {},
            thread_message_count=0,
            created_at=existing_submission.created_at if existing_submission else datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            last_voted_at=existing_submission.last_voted_at if existing_submission else None,
            last_thread_message_at=None,
            is_deleted=False,
            channel_id=channel_id
        )
        
        self.data["submissions"][message_id] = submission.to_dict()
        
        if user_id not in self.data["user_projects"]:
            self.data["user_projects"][user_id] = []
        
        # Only add project_id if not already present (avoid duplicates)
        if project_id not in self.data["user_projects"][user_id]:
            self.data["user_projects"][user_id].append(project_id)
        
        # Register file hashes
        for file_hash in file_hashes:
            self.data["file_hashes"][file_hash] = (user_id, message_id, project_id)
        
        # Register links
        for link in media_links:
            self.data["link_registry"][link] = (user_id, message_id, project_id)
        
        self._save()
        return submission, is_new_version
    
    def get_submission(self, message_id: str) -> Optional[Submission]:
        """Get submission by message ID"""
        data = self.data["submissions"].get(message_id)
        return Submission.from_dict(data) if data else None
    
    def update_submission(self, submission: Submission):
        """Update an existing submission"""
        self.data["submissions"][submission.message_id] = submission.to_dict()
        
        # Update file hash registry
        for file_hash in submission.file_hashes:
            self.data["file_hashes"][file_hash] = (submission.user_id, submission.message_id, submission.project_id)
        
        # Update link registry
        for link in submission.media_links:
            self.data["link_registry"][link] = (submission.user_id, submission.message_id, submission.project_id)
        
        self._save()
    
    def handle_vote(self, message_id: str, user_id: str, emoji: str, count: int) -> bool:
        """
        Centralized vote handling
        Returns: True if vote was recorded, False if spam detected
        """
        submission = self.get_submission(message_id)
        if not submission:
            return False
        
        # Check if user has already voted (spam protection)
        if submission.has_voted(user_id):
            self.bot.logger.log(MODULE_NAME, 
                              f"Vote spam detected: {user_id} tried to vote again on {message_id}", 
                              "WARNING")
            return False
        
        # Record vote
        submission.record_vote(user_id, emoji)
        submission.votes[emoji] = count
        
        self.update_submission(submission)
        return True
    
    def update_vote_count(self, message_id: str, emoji: str, count: int):
        """Update vote count for a submission"""
        submission = self.get_submission(message_id)
        if submission:
            submission.votes[emoji] = count
            submission.last_voted_at = datetime.now().isoformat()
            self.update_submission(submission)
    
    def get_user_xp(self, user_id: str) -> int:
        """Calculate total XP for a user (only non-deleted submissions, grouped by project_id)"""
        total_xp = 0
        counted_projects = set()
        
        for project_id in self.data["user_projects"].get(user_id, []):
            if project_id in counted_projects:
                continue
                
            # Find latest version of this project
            latest_submission = None
            for submission_data in self.data["submissions"].values():
                submission = Submission.from_dict(submission_data)
                if submission.project_id == project_id and not submission.is_deleted:
                    if latest_submission is None or submission.version > latest_submission.version:
                        latest_submission = submission
            
            if latest_submission:
                total_xp += latest_submission.calculate_xp()
                counted_projects.add(project_id)
        
        return total_xp
    
    async def get_leaderboard(self, bot, limit: int = 10) -> List[tuple]:
        """
        Get top users by XP
        Checks if submissions still exist (lazy deletion detection)
        Groups versions together (only counts latest version of each project)
        """
        user_xp = {}
        
        for user_id in self.data["user_projects"].keys():
            counted_projects = set()
            
            # Check if user's submissions still exist
            for project_id in self.data["user_projects"][user_id]:
                if project_id in counted_projects:
                    continue
                
                # Find latest version of this project
                latest_submission = None
                for msg_id, submission_data in list(self.data["submissions"].items()):
                    submission = Submission.from_dict(submission_data)
                    
                    if submission.project_id == project_id and not submission.is_deleted:
                        # Lazy deletion check - only if we have channel_id
                        if submission.channel_id:
                            try:
                                channel = bot.get_channel(int(submission.channel_id))
                                if channel:
                                    await channel.fetch_message(int(msg_id))
                            except (discord.NotFound, discord.HTTPException, AttributeError):
                                # Message was deleted
                                self.bot.logger.log(MODULE_NAME, 
                                                  f"Detected deleted submission: {msg_id}", "WARNING")
                                submission.mark_deleted()
                                self.update_submission(submission)
                                continue
                        
                        # Track latest version
                        if latest_submission is None or submission.version > latest_submission.version:
                            latest_submission = submission
                
                # Add XP from latest version only
                if latest_submission:
                    if user_id not in user_xp:
                        user_xp[user_id] = 0
                    user_xp[user_id] += latest_submission.calculate_xp()
                    counted_projects.add(project_id)
        
        sorted_users = sorted(user_xp.items(), key=lambda x: x[1], reverse=True)
        return sorted_users[:limit]
    
    def check_file_duplicate(self, file_hash: str) -> Optional[tuple]:
        """Check if file hash exists (returns (user_id, message_id, project_id) or None)"""
        return self.data["file_hashes"].get(file_hash)
    
    def check_link_duplicate(self, link: str) -> Optional[tuple]:
        """Check if link exists (returns (user_id, message_id, project_id) or None)"""
        return self.data["link_registry"].get(link)
    
    def get_stats(self) -> Dict:
        """Get database statistics"""
        active_submissions = [s for s in self.data["submissions"].values() 
                            if not Submission.from_dict(s).is_deleted]
        
        return {
            "total_submissions": len(active_submissions),
            "total_projects": sum(1 for s in active_submissions 
                                if Submission.from_dict(s).submission_type == SubmissionType.PROJECT),
            "total_artwork": sum(1 for s in active_submissions 
                               if Submission.from_dict(s).submission_type == SubmissionType.ARTWORK),
            "total_users": len(self.data["user_projects"]),
            "total_votes": sum(sum(Submission.from_dict(s).votes.values()) 
                             for s in active_submissions)
        }
    
    def set_sticky_message(self, channel_id: str, message_id: str):
        """Set the sticky message ID for a channel"""
        self.data["sticky_messages"][channel_id] = message_id
        self._save()
    
    def get_sticky_message(self, channel_id: str) -> Optional[str]:
        """Get the sticky message ID for a channel"""
        return self.data["sticky_messages"].get(channel_id)


class SubmissionValidator:
    """Validates submission format and content"""
    
    @staticmethod
    def extract_links(content: str) -> List[str]:
        """Extract URLs from message content"""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        return re.findall(url_pattern, content)
    
    @staticmethod
    def validate_project(message: discord.Message) -> Tuple[bool, Optional[str], Optional[str], List[str], Optional[str]]:
        """
        Validate project submission
        Returns: (is_valid, title, description, media_links, error_message)
        """
        content = message.content
        
        # Check for title (markdown headers)
        title_match = re.search(r'^#{1,3}\s+(.+)$', content, re.MULTILINE)
        if not title_match:
            return (False, None, None, [], 
                   "Your project submission must include a title using `# Title`, `## Title`, or `### Title`")
        
        title = title_match.group(1).strip()
        
        # Check for description (bullet points with -)
        description_lines = re.findall(r'^\s*-\s+(.+)$', content, re.MULTILINE)
        if not description_lines:
            return (False, None, None, [], 
                   "Your project submission must include a description with bullet points using `-`")
        
        description = "\n".join(description_lines)
        
        # Check for media (link, image, or file)
        links = SubmissionValidator.extract_links(content)
        has_attachment = len(message.attachments) > 0
        
        # Special case: image-only submission
        if has_attachment and not links:
            all_images = all(
                att.content_type and att.content_type.startswith('image/')
                for att in message.attachments
            )
            if all_images:
                return (False, None, None, [], 
                       "Image-only submissions should be posted in #artwork instead!")
        
        if not links and not has_attachment:
            return (False, None, None, [], 
                   "Your project submission must include at least one link, image, or file attachment")
        
        # Collect media links
        media_links = links.copy()
        for att in message.attachments:
            media_links.append(att.url)
        
        return (True, title, description, media_links, None)
    
    @staticmethod
    def validate_artwork(message: discord.Message) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validate artwork submission
        Returns: (is_valid, thumbnail, error_message)
        """
        has_image = any(
            att.content_type and att.content_type.startswith('image/')
            for att in message.attachments
        )
        
        if not has_image:
            return (False, None, "Artwork submissions must include at least one attached image")
        
        thumbnail = next(
            (att.url for att in message.attachments 
             if att.content_type and att.content_type.startswith('image/')),
            None
        )
        
        return (True, thumbnail, None)


class CommunityManager:
    """Manages community submissions and validation"""
    
    def __init__(self, bot, db: SubmissionDatabase):
        self.bot = bot
        self.db = db
    
    async def compute_file_hash(self, attachment: discord.Attachment) -> str:
        """Compute SHA256 hash of file content"""
        try:
            file_data = await attachment.read()
            return hashlib.sha256(file_data).hexdigest()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to hash file {attachment.filename}", e)
            return ""
    
    async def check_duplicates(self, message: discord.Message, file_hashes: List[str], 
                              media_links: List[str]) -> Optional[str]:
        """
        Check for duplicate submissions using file hashing
        Returns: error message if duplicate found, None otherwise
        """
        user_id = str(message.author.id)
        
        # Check file hashes (detects same content regardless of filename)
        for file_hash in file_hashes:
            existing = self.db.check_file_duplicate(file_hash)
            if existing:
                existing_user_id, existing_msg_id, existing_proj_id = existing
                
                if existing_user_id != user_id:
                    user = await self.bot.fetch_user(int(existing_user_id))
                    return f"This file appears to be copied from <@{existing_user_id}> ({user.display_name})'s submission. Please only submit your own original work."
                else:
                    return "You've already submitted this file. Please only repost if something has changed."
        
        # Check links
        for link in media_links:
            existing = self.db.check_link_duplicate(link)
            if existing:
                existing_user_id, existing_msg_id, existing_proj_id = existing
                
                if existing_user_id != user_id:
                    user = await self.bot.fetch_user(int(existing_user_id))
                    return f"This link was already shared by <@{existing_user_id}> ({user.display_name}). Please only submit your own original work."
        
        return None
    
    async def send_error_dm(self, user: discord.User, error_message: str, submission_type: str):
        """Send a friendly error DM to the user"""
        try:
            embed = discord.Embed(
                title=f"❌ {submission_type.title()} Submission Issue",
                description=error_message,
                color=discord.Color.red()
            )
            
            if submission_type == "project":
                embed.add_field(
                    name="✅ Correct Format",
                    value=(
                        "```\n"
                        "# My Project Title\n\n"
                        "- First feature description\n"
                        "- Second feature description\n\n"
                        "[Link to project](https://example.com)\n"
                        "```\n"
                        "**Or attach an image/file with your title and description**"
                    ),
                    inline=False
                )
            else:
                embed.add_field(
                    name="✅ Correct Format",
                    value="Simply attach one or more images to your message!",
                    inline=False
                )
            
            embed.set_footer(text="Please try posting again with the correct format")
            
            await user.send(embed=embed)
            self.bot.logger.log(MODULE_NAME, f"Sent error DM to {user.display_name}")
            
        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME, 
                              f"Could not DM {user.display_name} (DMs disabled)", "WARNING")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send error DM to {user.display_name}", e)
    
    async def send_version_notification(self, user: discord.User, submission: Submission, 
                                       message: discord.Message) -> bool:
        """
        Send DM about project version update with undo button
        Returns: True if user wants to keep as version, False if undo
        """
        try:
            embed = discord.Embed(
                title="🔄 Project Version Updated",
                description=f"Your project **{submission.title}** has been updated to version **{submission.version}**",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Shared Features",
                value=(
                    "• All versions share the same votes and XP\n"
                    "• This prevents leaderboard clutter\n"
                    "• Your old version is still tracked"
                ),
                inline=False
            )
            embed.add_field(
                name="Was this a mistake?",
                value="Click **Undo** below to register this as a separate project instead.",
                inline=False
            )
            
            view = VersionUndoView(self.bot, self.db, message.id, user.id)
            dm_message = await user.send(embed=embed, view=view)
            
            self.bot.logger.log(MODULE_NAME, 
                              f"Sent version notification to {user.display_name} for v{submission.version}")
            
            return True
            
        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME, 
                              f"Could not DM {user.display_name} (DMs disabled)", "WARNING")
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, 
                                f"Failed to send version notification to {user.display_name}", e)
            return True
    
    async def process_submission(self, message: discord.Message, channel_type: str):
        """Process a submission in projects or artwork channel"""
        try:
            self.bot.logger.log(MODULE_NAME, 
                              f"Processing {channel_type} submission from {message.author.display_name}")
            
            # Validate based on channel type
            if channel_type == "project":
                is_valid, title, description, media_links, error = SubmissionValidator.validate_project(message)
                thumbnail = None
                
                for att in message.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        thumbnail = att.url
                        break
            else:  # artwork
                is_valid, thumbnail, error = SubmissionValidator.validate_artwork(message)
                title = None
                description = None
                media_links = [thumbnail] if thumbnail else []
            
            if not is_valid:
                await self.send_error_dm(message.author, error, channel_type)
                await message.delete()
                self.bot.logger.log(MODULE_NAME, 
                                  f"Deleted invalid {channel_type} submission from {message.author.display_name}")
                return
            
            # Compute file hashes (uses content, not filename)
            file_hashes = []
            for att in message.attachments:
                file_hash = await self.compute_file_hash(att)
                if file_hash:
                    file_hashes.append(file_hash)
            
            # Check for duplicates
            duplicate_error = await self.check_duplicates(message, file_hashes, media_links)
            if duplicate_error:
                await self.send_error_dm(message.author, duplicate_error, channel_type)
                await message.delete()
                self.bot.logger.log(MODULE_NAME, 
                                  f"Deleted duplicate {channel_type} submission from {message.author.display_name}")
                return
            
            # Add to database
            submission_type = SubmissionType.PROJECT if channel_type == "project" else SubmissionType.ARTWORK
            submission, is_new_version = self.db.add_submission(
                message_id=str(message.id),
                user_id=str(message.author.id),
                submission_type=submission_type,
                title=title,
                description=description,
                media_links=media_links,
                thumbnail=thumbnail,
                file_hashes=file_hashes,
                channel_id=str(message.channel.id)
            )
            
            # Send version notification if applicable
            if is_new_version:
                await self.send_version_notification(message.author, submission, message)
            
            # Create discussion thread (even for version updates)
            thread_name = title[:100] if title else f"{message.author.display_name}'s artwork"
            if is_new_version:
                thread_name = f"{thread_name} v{submission.version}"
            thread = await message.create_thread(name=thread_name, auto_archive_duration=10080)
            
            # Add reaction votes
            await message.add_reaction(REACTION_FIRE)
            await message.add_reaction(REACTION_NEUTRAL)
            await message.add_reaction(REACTION_TRASH)
            
            version_str = f" v{submission.version}" if is_new_version else ""
            self.bot.logger.log(MODULE_NAME, 
                              f"✅ Successfully registered {channel_type} submission: {submission.project_id}{version_str}")
            
            # Update sticky message
            await self.update_sticky_message(message.channel)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to process submission", e)
    
    async def process_edit(self, before: discord.Message, after: discord.Message):
        """Process message edit and update submission"""
        try:
            submission = self.db.get_submission(str(after.id))
            if not submission:
                return
            
            self.bot.logger.log(MODULE_NAME, 
                              f"Detected edit for submission {submission.project_id}")
            
            # Re-validate content
            if submission.submission_type == SubmissionType.PROJECT:
                is_valid, title, description, media_links, error = SubmissionValidator.validate_project(after)
                
                # Get new thumbnail
                thumbnail = None
                for att in after.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        thumbnail = att.url
                        break
            else:  # artwork
                is_valid, thumbnail, error = SubmissionValidator.validate_artwork(after)
                title = None
                description = None
                media_links = [thumbnail] if thumbnail else []
            
            if not is_valid:
                # Edit made it invalid - mark as deleted
                submission.mark_deleted()
                self.db.update_submission(submission)
                await after.delete()
                await self.send_error_dm(after.author, 
                    "Your edit made the submission invalid. " + error, 
                    submission.submission_type.value)
                return
            
            # Compute new file hashes
            file_hashes = []
            for att in after.attachments:
                file_hash = await self.compute_file_hash(att)
                if file_hash:
                    file_hashes.append(file_hash)
            
            # Update submission
            submission.update_content(title, description)
            submission.update_media(media_links, thumbnail, file_hashes)
            self.db.update_submission(submission)
            
            self.bot.logger.log(MODULE_NAME, 
                              f"✅ Updated submission {submission.project_id} after edit")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to process edit", e)
    
    async def update_sticky_message(self, channel: discord.TextChannel):
        """Update the sticky dashboard message"""
        try:
            stats = self.db.get_stats()
            
            if channel.name == PROJECTS_CHANNEL_NAME:
                embed = discord.Embed(
                    title="👋 Welcome to Projects!",
                    description="Share your projects and get feedback from the community.",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="📋 Submission Format",
                    value=(
                        "```\n"
                        "# Your Project Title\n"
                        "- Feature description\n"
                        "- Another feature\n"
                        "[Link](https://...)\n"
                        "```"
                    ),
                    inline=False
                )
            else:  # artwork
                embed = discord.Embed(
                    title="👋 Welcome to Artwork!",
                    description="Share your creative work and get feedback from the community.",
                    color=discord.Color.purple()
                )
                embed.add_field(
                    name="🎨 How to Submit",
                    value="Attach one or more images to your message!",
                    inline=False
                )
            
            # Compact stats in two columns
            embed.add_field(
                name="📊 Stats",
                value=(
                    f"**Submissions:** {stats['total_submissions']}\n"
                    f"**Projects:** {stats['total_projects']}\n"
                    f"**Artwork:** {stats['total_artwork']}"
                ),
                inline=True
            )
            
            embed.add_field(
                name="👥 Community",
                value=(
                    f"**Contributors:** {stats['total_users']}\n"
                    f"**Total Votes:** {stats['total_votes']}\n"
                    f"**Active:** 🟢"
                ),
                inline=True
            )
            
            embed.set_footer(text="React with 🔥 😐 🗑️ to vote • Click buttons below for more")
            
            view = CommunityDashboardView(self.bot, self.db)
            
            old_sticky_id = self.db.get_sticky_message(str(channel.id))
            if old_sticky_id:
                try:
                    old_message = await channel.fetch_message(int(old_sticky_id))
                    await old_message.delete()
                except:
                    pass
            
            new_sticky = await channel.send(embed=embed, view=view)
            self.db.set_sticky_message(str(channel.id), str(new_sticky.id))
            
            self.bot.logger.log(MODULE_NAME, f"Updated sticky message in #{channel.name}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to update sticky message", e)


class VersionUndoView(discord.ui.View):
    """View with undo button for version updates"""
    
    def __init__(self, bot, db: SubmissionDatabase, message_id: str, user_id: int):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.db = db
        self.message_id = message_id
        self.user_id = user_id
    
    @discord.ui.button(label="Undo - Register Separately", style=discord.ButtonStyle.danger, emoji="↩️")
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Undo version linking and register as separate project"""
        try:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "❌ Only the project owner can undo this.",
                    ephemeral=True
                )
                return
            
            submission = self.db.get_submission(self.message_id)
            if not submission:
                await interaction.response.send_message(
                    "❌ Submission not found.",
                    ephemeral=True
                )
                return
            
            # Reset to version 1.0 and create new project ID
            submission.version = "1.0"
            submission.project_id = f"project_{submission.user_id}_{submission.message_id}"
            self.db.update_submission(submission)
            
            await interaction.response.send_message(
                f"✅ Project **{submission.title}** is now registered separately with version 1.0",
                ephemeral=False
            )
            
            # Disable button
            button.disabled = True
            await interaction.message.edit(view=self)
            
            self.bot.logger.log(MODULE_NAME, 
                              f"User {interaction.user.id} undid version linking for {self.message_id}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to undo version", e)
            await interaction.response.send_message(
                "❌ Failed to undo version linking.",
                ephemeral=True
            )


class CommunityDashboardView(discord.ui.View):
    """View with leaderboard and my projects buttons"""
    
    def __init__(self, bot, db: SubmissionDatabase):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
    
    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.primary, emoji="🏆")
    async def leaderboard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show leaderboard when button is clicked"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            leaderboard = await self.db.get_leaderboard(self.bot, limit=10)
            
            embed = discord.Embed(
                title="🏆 Community Leaderboard",
                description="Top contributors by total XP",
                color=discord.Color.gold()
            )
            
            if not leaderboard:
                embed.add_field(
                    name="No Data",
                    value="No submissions yet! Be the first to contribute!",
                    inline=False
                )
            else:
                medals = ["🥇", "🥈", "🥉"]
                leaderboard_text = ""
                
                for i, (user_id, xp) in enumerate(leaderboard):
                    try:
                        user = await self.bot.fetch_user(int(user_id))
                        avatar_emoji = "👤"
                        username = user.display_name
                    except:
                        avatar_emoji = "❓"
                        username = f"User {user_id[-6:]}"
                    
                    medal = medals[i] if i < 3 else f"**{i+1}.**"
                    leaderboard_text += f"{medal} {avatar_emoji} **{username}** — {xp} XP\n"
                
                embed.add_field(name="Rankings", value=leaderboard_text, inline=False)
            
            user_xp = self.db.get_user_xp(str(interaction.user.id))
            user_submissions = len([p for p in self.db.data["user_projects"].get(str(interaction.user.id), [])])
            embed.set_footer(
                text=f"Your stats: {user_xp} XP • {user_submissions} submission(s)",
                icon_url=interaction.user.display_avatar.url
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to show leaderboard", e)
            try:
                await interaction.followup.send("❌ Failed to load leaderboard", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(label="My Projects", style=discord.ButtonStyle.secondary, emoji="📁")
    async def my_projects_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show user's projects"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            user_id = str(interaction.user.id)
            project_ids = self.db.data["user_projects"].get(user_id, [])
            
            if not project_ids:
                embed = discord.Embed(
                    title="📁 Your Projects",
                    description="You haven't submitted anything yet!",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="Get Started",
                    value="Submit a project or artwork to see your stats here.",
                    inline=False
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Collect user's submissions (grouped by project_id, showing latest version)
            user_submissions = {}
            for msg_id, sub_data in self.db.data["submissions"].items():
                sub = Submission.from_dict(sub_data)
                if sub.user_id == user_id and not sub.is_deleted:
                    if sub.project_id not in user_submissions or sub.version > user_submissions[sub.project_id].version:
                        user_submissions[sub.project_id] = sub
            
            embed = discord.Embed(
                title=f"📁 {interaction.user.display_name}'s Projects",
                description=f"You have **{len(user_submissions)}** active submission(s)",
                color=discord.Color.blue()
            )
            
            total_xp = self.db.get_user_xp(user_id)
            total_votes = sum(sum(sub.votes.values()) for sub in user_submissions.values())
            
            embed.add_field(
                name="📊 Your Stats",
                value=(
                    f"**Total XP:** {total_xp}\n"
                    f"**Total Votes:** {total_votes}\n"
                    f"**Submissions:** {len(user_submissions)}"
                ),
                inline=False
            )
            
            # Show up to 5 most recent projects
            sorted_subs = sorted(user_submissions.values(), 
                               key=lambda x: x.updated_at, reverse=True)[:5]
            
            projects_text = ""
            for sub in sorted_subs:
                title_display = sub.title if sub.title else "Artwork"
                xp = sub.calculate_xp()
                votes_str = " ".join(f"{emoji}{count}" for emoji, count in sub.votes.items() if count > 0)
                version_str = f" v{sub.version}" if sub.version != "1.0" else ""
                
                projects_text += f"**{title_display}**{version_str}\n"
                projects_text += f"├ ID: `{sub.project_id}`\n"
                projects_text += f"├ XP: {xp} • Votes: {votes_str or 'None'}\n"
                projects_text += f"└ Updated: <t:{int(datetime.fromisoformat(sub.updated_at).timestamp())}:R>\n\n"
            
            if projects_text:
                embed.add_field(
                    name="🎯 Recent Submissions",
                    value=projects_text,
                    inline=False
                )
            
            embed.set_footer(
                text=f"Showing {len(sorted_subs)} of {len(user_submissions)} submission(s)",
                icon_url=interaction.user.display_avatar.url
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to show user projects", e)
            try:
                await interaction.followup.send("❌ Failed to load your projects", ephemeral=True)
            except:
                pass


class LeaderboardView(discord.ui.View):
    """Legacy view for backwards compatibility"""
    
    def __init__(self, bot, db: SubmissionDatabase):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
    
    @discord.ui.button(label="View Leaderboard", style=discord.ButtonStyle.primary, emoji="🏆")
    async def leaderboard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show leaderboard when button is clicked"""
        view = CommunityDashboardView(self.bot, self.db)
        await view.leaderboard_button(interaction, button)


def setup(bot):
    """Setup function called by main bot to initialize this module"""
    
    listener_name = f"_{MODULE_NAME.lower()}_listener_registered"
    if hasattr(bot, listener_name):
        bot.logger.log(MODULE_NAME, "Module already setup, skipping duplicate registration")
        return
    setattr(bot, listener_name, True)
    
    bot.logger.log(MODULE_NAME, "Setting up community module")
    
    db = SubmissionDatabase(bot)
    bot.community_db = db
    
    manager = CommunityManager(bot, db)
    bot.community_manager = manager
    
    @bot.listen('on_message')
    async def on_community_message(message: discord.Message):
        """Listen for messages in projects and artwork channels"""
        if message.author.bot:
            return
        
        if not message.guild:
            return
        
        channel_name = message.channel.name.lower()
        
        if channel_name == PROJECTS_CHANNEL_NAME:
            await manager.process_submission(message, "project")
        elif channel_name == ARTWORK_CHANNEL_NAME:
            await manager.process_submission(message, "artwork")
    
    @bot.listen('on_message_edit')
    async def on_community_edit(before: discord.Message, after: discord.Message):
        """Detect submission edits and update database"""
        if after.author.bot:
            return
        
        if not after.guild:
            return
        
        channel_name = after.channel.name.lower()
        
        if channel_name in [PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME]:
            await manager.process_edit(before, after)
    
    @bot.listen('on_raw_reaction_add')
    async def on_community_reaction_add(payload: discord.RawReactionActionEvent):
        """Centralized vote handling with spam protection"""
        try:
            if payload.user_id == bot.user.id:
                return
            
            submission = db.get_submission(str(payload.message_id))
            if not submission:
                return
            
            emoji = str(payload.emoji)
            
            if emoji not in XP_VALUES:
                return
            
            channel = bot.get_channel(payload.channel_id)
            if not channel:
                return
            
            message = await channel.fetch_message(payload.message_id)
            
            # Get reaction count
            count = 0
            for reaction in message.reactions:
                if str(reaction.emoji) == emoji:
                    count = reaction.count
                    break
            
            # Handle vote with spam protection
            was_recorded = db.handle_vote(str(message.id), str(payload.user_id), emoji, count)
            
            if was_recorded:
                bot.logger.log(MODULE_NAME, 
                              f"Recorded {emoji} vote from user {payload.user_id} on {submission.project_id}")
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to process reaction", e)
    
    @bot.listen('on_raw_reaction_remove')
    async def on_community_reaction_remove(payload: discord.RawReactionActionEvent):
        """Update vote counts when reactions are removed"""
        try:
            submission = db.get_submission(str(payload.message_id))
            if not submission:
                return
            
            emoji = str(payload.emoji)
            
            if emoji not in XP_VALUES:
                return
            
            channel = bot.get_channel(payload.channel_id)
            if not channel:
                return
            
            message = await channel.fetch_message(payload.message_id)
            
            for reaction in message.reactions:
                if str(reaction.emoji) == emoji:
                    db.update_vote_count(str(message.id), emoji, reaction.count)
                    break
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to process reaction removal", e)
    
    @bot.tree.command(name="update_sticky", description="[Admin] Update the sticky dashboard message")
    @app_commands.checks.has_permissions(administrator=True)
    async def update_sticky(interaction: discord.Interaction):
        """Manually update sticky message"""
        try:
            channel_name = interaction.channel.name.lower()
            
            if channel_name not in [PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME]:
                await interaction.response.send_message(
                    "❌ This command can only be used in #projects or #artwork",
                    ephemeral=True
                )
                return
            
            await interaction.response.defer(ephemeral=True)
            await manager.update_sticky_message(interaction.channel)
            
            await interaction.followup.send("✅ Sticky message updated!", ephemeral=True)
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to update sticky via command", e)
            await interaction.followup.send("❌ Failed to update sticky message", ephemeral=True)
    
    @bot.tree.command(name="community_stats", description="View community submission statistics")
    async def community_stats(interaction: discord.Interaction):
        """Show community statistics"""
        try:
            stats = db.get_stats()
            leaderboard = await db.get_leaderboard(bot, limit=5)
            
            embed = discord.Embed(
                title="📊 Community Statistics",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="📈 Overall Stats",
                value=(
                    f"**Total Submissions:** {stats['total_submissions']}\n"
                    f"**Projects:** {stats['total_projects']}\n"
                    f"**Artwork:** {stats['total_artwork']}\n"
                    f"**Contributors:** {stats['total_users']}\n"
                    f"**Total Votes:** {stats['total_votes']}"
                ),
                inline=False
            )
            
            if leaderboard:
                medals = ["🥇", "🥈", "🥉", "4.", "5."]
                leaderboard_text = ""
                for i, (user_id, xp) in enumerate(leaderboard):
                    medal = medals[i]
                    leaderboard_text += f"{medal} <@{user_id}> - **{xp} XP**\n"
                
                embed.add_field(name="🏆 Top Contributors", value=leaderboard_text, inline=False)
            
            user_xp = db.get_user_xp(str(interaction.user.id))
            user_submissions = db.data["user_projects"].get(str(interaction.user.id), [])
            
            embed.add_field(
                name="Your Stats",
                value=f"**XP:** {user_xp}\n**Submissions:** {len(user_submissions)}",
                inline=False
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to show stats", e)
            await interaction.response.send_message("❌ Failed to load statistics", ephemeral=True)
    
    bot.logger.log(MODULE_NAME, "Community module setup complete")


# ============================================================================
# UNIT TESTS
# ============================================================================

def run_tests():
    """Run unit tests for submission validation"""
    
    class MockMessage:
        def __init__(self, content, attachments=None):
            self.content = content
            self.attachments = attachments or []
    
    class MockAttachment:
        def __init__(self, content_type, url="https://example.com/file.png"):
            self.content_type = content_type
            self.url = url
    
    print("✅ Test 8 Passed: All header formats recognized")
    
    print("\n" + "="*70)
    print("All Tests Passed! ✅")
    print("="*70 + "\n")


# Uncomment to run tests during module load
# run_tests()\n" + "="*70)
    print("Running Community Module Unit Tests")
    print("="*70 + "\n")
    
    # Test 1: Valid project submission
    msg = MockMessage(
        "# My Cool Project\n\n- Feature 1\n- Feature 2\n\nhttps://github.com/user/project"
    )
    is_valid, title, desc, links, error = SubmissionValidator.validate_project(msg)
    assert is_valid, "Test 1 Failed: Valid project not recognized"
    assert title == "My Cool Project", f"Test 1 Failed: Title mismatch (got '{title}')"
    assert "Feature 1" in desc, "Test 1 Failed: Description missing feature"
    print("✅ Test 1 Passed: Valid project submission")
    
    # Test 2: Missing title
    msg = MockMessage("- Feature 1\n- Feature 2\n\nhttps://example.com")
    is_valid, _, _, _, error = SubmissionValidator.validate_project(msg)
    assert not is_valid, "Test 2 Failed: Should reject missing title"
    assert "title" in error.lower(), "Test 2 Failed: Error should mention title"
    print("✅ Test 2 Passed: Missing title detected")
    
    # Test 3: Missing description
    msg = MockMessage("# Title\n\nhttps://example.com")
    is_valid, _, _, _, error = SubmissionValidator.validate_project(msg)
    assert not is_valid, "Test 3 Failed: Should reject missing description"
    assert "bullet" in error.lower(), "Test 3 Failed: Error should mention bullets"
    print("✅ Test 3 Passed: Missing description detected")
    
    # Test 4: Image-only project (should redirect to artwork)
    msg = MockMessage(
        "# Title\n\n- Feature 1",
        attachments=[MockAttachment("image/png")]
    )
    is_valid, _, _, _, error = SubmissionValidator.validate_project(msg)
    assert not is_valid, "Test 4 Failed: Should reject image-only project"
    assert "artwork" in error.lower(), "Test 4 Failed: Should mention artwork channel"
    print("✅ Test 4 Passed: Image-only project redirected to artwork")
    
    # Test 5: Valid artwork submission
    msg = MockMessage("", attachments=[MockAttachment("image/jpeg")])
    is_valid, thumbnail, error = SubmissionValidator.validate_artwork(msg)
    assert is_valid, "Test 5 Failed: Valid artwork not recognized"
    assert thumbnail is not None, "Test 5 Failed: Thumbnail not extracted"
    print("✅ Test 5 Passed: Valid artwork submission")
    
    # Test 6: Invalid artwork (no image)
    msg = MockMessage("Some text", attachments=[MockAttachment("application/pdf")])
    is_valid, _, error = SubmissionValidator.validate_artwork(msg)
    assert not is_valid, "Test 6 Failed: Should reject non-image artwork"
    print("✅ Test 6 Passed: Non-image artwork rejected")
    
    # Test 7: Link extraction
    links = SubmissionValidator.extract_links("Check out https://example.com and http://test.org")
    assert len(links) == 2, f"Test 7 Failed: Expected 2 links, got {len(links)}"
    assert "https://example.com" in links, "Test 7 Failed: First link not extracted"
    print("✅ Test 7 Passed: Link extraction works")
    
    # Test 8: Multiple header formats
    for header in ["# Title", "## Title", "### Title"]:
        msg = MockMessage(f"{header}\n\n- Feature\n\nhttps://example.com")
        is_valid, title, _, _, _ = SubmissionValidator.validate_project(msg)
        assert is_valid, f"Test 8 Failed: {header} not recognized"
        assert title == "Title", f"Test 8 Failed: Title mismatch for {header}"
    print("