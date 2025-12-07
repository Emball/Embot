import discord
from discord import app_commands
from discord.ext import tasks
import re
import time
import json
import hashlib
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
import aiohttp
from dataclasses import dataclass, asdict
from enum import Enum

MODULE_NAME = "COMMUNITY"

# Channel configuration
PROJECTS_CHANNEL_NAME = "projects"
ARTWORK_CHANNEL_NAME = "artwork"
ANNOUNCEMENTS_CHANNEL_NAME = "announcements"

# Special User Configuration
EMBALL_USER_ID = "1328822521084117033"
EMBALL_ROLE_NAME = "Emball Releases"
EMBALL_KEYWORDS = ["remaster", "edit"] # logic handles "remaster/edit", "remaster & edit" via inclusion check

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

# Thread message XP
THREAD_MESSAGE_XP = 0.1

# Theme colors (sleek, modern palette)
THEME_COLORS = {
    "primary": 0x00D9FF,      # Cyan - main accent
    "secondary": 0xFF006E,    # Pink - highlights
    "success": 0x06FFA5,      # Mint green
    "warning": 0xFFBE0B,      # Amber
    "error": 0xFF006E,        # Pink-red
    "dark": 0x2B2D31,         # Discord dark gray
    "project": 0x5865F2,      # Discord blurple
    "artwork": 0xEB459E,      # Vibrant pink
    "gold": 0xFFC107,         # Gold for leaderboard
    "spotlight": 0xFFD700,    # Bright Gold for spotlight
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
    thread_message_xp: float  # XP from thread messages
    created_at: str
    updated_at: str
    last_voted_at: Optional[str]
    last_thread_message_at: Optional[str]
    is_deleted: bool
    channel_id: Optional[str] = None
    linked_submissions: List[str] = None  # message_ids of linked submissions (shared votes)
    
    def __post_init__(self):
        if self.linked_submissions is None:
            self.linked_submissions = []
        # Initialize thread_message_xp if not present (for backward compatibility)
        if not hasattr(self, 'thread_message_xp'):
            self.thread_message_xp = 0.0
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        data['submission_type'] = self.submission_type.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Submission':
        """Create from dictionary, filtering out unexpected fields"""
        import inspect
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        
        if 'submission_type' in filtered_data:
            filtered_data['submission_type'] = SubmissionType(filtered_data['submission_type'])
        
        return cls(**filtered_data)
    
    def calculate_xp(self) -> int:
        """Calculate total XP for this submission"""
        vote_xp = sum(count * XP_VALUES.get(emoji, 0) for emoji, count in self.votes.items())
        return vote_xp + self.thread_message_xp
    
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
        try:
            major, minor = map(int, self.version.split('.'))
            self.version = f"{major + 1}.0"
        except ValueError:
            # Handle cases where version might not be standard x.y
            self.version = f"{self.version}.1"
        self.updated_at = datetime.now().isoformat()
        return self.version
    
    def mark_deleted(self):
        """Mark submission as deleted"""
        self.is_deleted = True
        self.updated_at = datetime.now().isoformat()
    
    def record_vote(self, user_id: str, emoji: str):
        """Record a user's vote"""
        if user_id not in self.user_votes:
            self.user_votes[user_id] = emoji
        self.last_voted_at = datetime.now().isoformat()
    
    def add_thread_message(self):
        """Add XP for a thread message"""
        self.thread_message_count += 1
        self.thread_message_xp += THREAD_MESSAGE_XP
        self.last_thread_message_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()
    
    def has_voted(self, user_id: str) -> bool:
        """Check if user has already voted"""
        return user_id in self.user_votes

class SubmissionDatabase:
    """Manages submission data persistence with optimizations for large datasets"""
    
    def __init__(self, bot):
        self.bot = bot
        self.data = {
            "submissions": {},
            "user_projects": {},
            "file_hashes": {},
            "link_registry": {},
            "sticky_messages": {},
            "project_versions": {},
            "settings": {
                "sticky_enabled": True,
                "last_spotlight_week": None
            }
        }
        # Add caching for frequently accessed data
        self._user_xp_cache = {}
        self._cache_timestamp = {}
        self._cache_ttl = 300  # 5 minutes cache
        self._load()
    
    def _load(self):
        """Load database from file"""
        try:
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            if DB_FILE.exists():
                with open(DB_FILE, 'r', encoding='utf-8') as f:
                    loaded_data = json.load(f)
                    for key, value in loaded_data.items():
                        if key == "settings":
                            for setting_key, setting_val in value.items():
                                self.data["settings"][setting_key] = setting_val
                        else:
                            self.data[key] = value
                            
                self.bot.logger.log(MODULE_NAME, f"Loaded {len(self.data['submissions'])} submissions from database")
            else:
                self._save()
                self.bot.logger.log(MODULE_NAME, "Created new submission database")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load database", e)
    
    def _save(self):
        """Save database to file"""
        try:
            # Use atomic write to prevent corruption
            temp_file = DB_FILE.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            temp_file.replace(DB_FILE)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save database", e)
    
    def _get_title_hash(self, title: str) -> str:
        """Generate short hash for title"""
        return hashlib.md5(title.lower().strip().encode()).hexdigest()[:8]

    def set_sticky_enabled(self, enabled: bool):
        """Toggle sticky messages"""
        self.data["settings"]["sticky_enabled"] = enabled
        self._save()
        
    def is_sticky_enabled(self) -> bool:
        return self.data["settings"].get("sticky_enabled", True)

    def get_last_spotlight_week(self) -> Optional[str]:
        return self.data["settings"].get("last_spotlight_week")
    
    def set_last_spotlight_week(self, week_str: str):
        self.data["settings"]["last_spotlight_week"] = week_str
        self._save()
    
    def link_submissions(self, msg_id_1: str, msg_id_2: str):
        """Link two submissions to share votes"""
        sub1 = self.get_submission(msg_id_1)
        sub2 = self.get_submission(msg_id_2)
        
        if not sub1 or not sub2:
            return
        
        if msg_id_2 not in sub1.linked_submissions:
            sub1.linked_submissions.append(msg_id_2)
        if msg_id_1 not in sub2.linked_submissions:
            sub2.linked_submissions.append(msg_id_1)
        
        merged_votes = sub1.votes.copy()
        for emoji, count in sub2.votes.items():
            merged_votes[emoji] = merged_votes.get(emoji, 0) + count
        
        merged_user_votes = {**sub1.user_votes, **sub2.user_votes}
        
        sub1.votes = merged_votes
        sub2.votes = merged_votes
        sub1.user_votes = merged_user_votes
        sub2.user_votes = merged_user_votes
        
        self.update_submission(sub1)
        self.update_submission(sub2)
        
        self.bot.logger.log(MODULE_NAME, f"Linked submissions: {msg_id_1} <-> {msg_id_2}")
    
    def add_submission(self, message_id: str, user_id: str, submission_type: SubmissionType, 
                      title: Optional[str], description: Optional[str], 
                      media_links: List[str], thumbnail: Optional[str],
                      file_hashes: List[str], channel_id: Optional[str] = None) -> Tuple[Submission, bool, Optional[str]]:
        """Add a new submission to the database"""
        is_new_version = False
        version = "1.0"
        existing_submission = None
        linked_message_id = None
        
        detected_version = None
        if title:
            combined_text = title + " " + (description if description else "")
            version_match = re.search(r'(?:v|ver|version)\.?\s*?(\d+(?:\.\d+)+)', combined_text, re.IGNORECASE)
            if version_match:
                detected_version = version_match.group(1)

        if submission_type == SubmissionType.PROJECT and title:
            title_hash = self._get_title_hash(title)
            project_id = f"p-{user_id[-6:]}-{title_hash}"
            version_key = f"{user_id}:{title_hash}"
            
            if version_key in self.data["project_versions"]:
                is_new_version = True
                existing_message_ids = self.data["project_versions"][version_key]
                
                latest_version = "1.0"
                for msg_id in existing_message_ids:
                    if msg_id in self.data["submissions"]:
                        sub = Submission.from_dict(self.data["submissions"][msg_id])
                        if sub.version > latest_version:
                            latest_version = sub.version
                            existing_submission = sub
                
                if detected_version:
                    version = detected_version
                else:
                    try:
                        major, minor = map(int, latest_version.split('.'))
                        version = f"{major + 1}.0"
                    except:
                         version = "2.0"
                         
                self.data["project_versions"][version_key].append(message_id)
            else:
                if detected_version:
                    version = detected_version
                self.data["project_versions"][version_key] = [message_id]
        else:
            project_id = f"a-{user_id[-6:]}-{message_id[-8:]}"
        
        for file_hash in file_hashes:
            if file_hash in self.data["file_hashes"]:
                existing_user_id, existing_msg_id, existing_proj_id = self.data["file_hashes"][file_hash]
                if existing_user_id == user_id:
                    existing_sub = self.get_submission(existing_msg_id)
                    if existing_sub and existing_sub.submission_type == SubmissionType.ARTWORK:
                        linked_message_id = existing_msg_id
                        break
        
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
            thread_message_xp=0.0,
            created_at=existing_submission.created_at if existing_submission else datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            last_voted_at=existing_submission.last_voted_at if existing_submission else None,
            last_thread_message_at=None,
            is_deleted=False,
            channel_id=channel_id,
            linked_submissions=[]
        )
        
        self.data["submissions"][message_id] = submission.to_dict()
        
        if user_id not in self.data["user_projects"]:
            self.data["user_projects"][user_id] = []
        
        if project_id not in self.data["user_projects"][user_id]:
            self.data["user_projects"][user_id].append(project_id)
        
        for file_hash in file_hashes:
            self.data["file_hashes"][file_hash] = (user_id, message_id, project_id)
        
        for link in media_links:
            self.data["link_registry"][link] = (user_id, message_id, project_id)
        
        if linked_message_id:
            self.link_submissions(message_id, linked_message_id)
        
        # Invalidate cache for this user
        self._user_xp_cache.pop(user_id, None)
        
        self._save()
        return submission, is_new_version, linked_message_id
    
    def get_submission(self, message_id: str) -> Optional[Submission]:
        """Get submission by message ID"""
        data = self.data["submissions"].get(message_id)
        if not data:
            return None
        try:
            return Submission.from_dict(data)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to parse submission {message_id}", e)
            return None
    
    def update_submission(self, submission: Submission):
        """Update an existing submission"""
        self.data["submissions"][submission.message_id] = submission.to_dict()
        
        for file_hash in submission.file_hashes:
            self.data["file_hashes"][file_hash] = (submission.user_id, submission.message_id, submission.project_id)
        
        for link in submission.media_links:
            self.data["link_registry"][link] = (submission.user_id, submission.message_id, submission.project_id)
        
        # Invalidate cache for this user
        self._user_xp_cache.pop(submission.user_id, None)
        
        self._save()
    
    def handle_vote(self, message_id: str, user_id: str, emoji: str, count: int) -> bool:
        """Centralized vote handling with linked submission support"""
        submission = self.get_submission(message_id)
        if not submission:
            return False
        
        if submission.has_voted(user_id):
            self.bot.logger.log(MODULE_NAME, 
                              f"Vote spam detected: {user_id} tried to vote again on {message_id}", 
                              "WARNING")
            return False
        
        submission.record_vote(user_id, emoji)
        submission.votes[emoji] = count
        
        for linked_msg_id in submission.linked_submissions:
            linked_sub = self.get_submission(linked_msg_id)
            if linked_sub:
                linked_sub.votes = submission.votes.copy()
                linked_sub.user_votes = submission.user_votes.copy()
                self.update_submission(linked_sub)
        
        self.update_submission(submission)
        return True
    
    def update_vote_count(self, message_id: str, emoji: str, count: int):
        """Update vote count for a submission and its linked submissions"""
        submission = self.get_submission(message_id)
        if submission:
            submission.votes[emoji] = count
            submission.last_voted_at = datetime.now().isoformat()
            
            for linked_msg_id in submission.linked_submissions:
                linked_sub = self.get_submission(linked_msg_id)
                if linked_sub:
                    linked_sub.votes[emoji] = count
                    linked_sub.last_voted_at = datetime.now().isoformat()
                    self.update_submission(linked_sub)
            
            self.update_submission(submission)
    
    def add_thread_message_xp(self, message_id: str):
        """Add XP for a thread message"""
        submission = self.get_submission(message_id)
        if submission:
            submission.add_thread_message()
            self.update_submission(submission)
            self.bot.logger.log(MODULE_NAME, 
                              f"Added {THREAD_MESSAGE_XP} XP for thread message on {submission.project_id}")
    
    def get_user_xp(self, user_id: str) -> int:
        """Calculate total XP for a user with caching"""
        # Check cache
        cache_key = user_id
        if cache_key in self._user_xp_cache:
            cache_time = self._cache_timestamp.get(cache_key, 0)
            if time.time() - cache_time < self._cache_ttl:
                return self._user_xp_cache[cache_key]
        
        # Emball exclusion
        if str(user_id) == EMBALL_USER_ID:
            return 0
            
        total_xp = 0
        counted_projects = set()
        
        for project_id in self.data["user_projects"].get(user_id, []):
            if project_id in counted_projects:
                continue
                
            latest_submission = None
            for submission_data in self.data["submissions"].values():
                submission = Submission.from_dict(submission_data)
                if submission.project_id == project_id and not submission.is_deleted:
                    if latest_submission is None or submission.version > latest_submission.version:
                        latest_submission = submission
            
            if latest_submission:
                total_xp += latest_submission.calculate_xp()
                counted_projects.add(project_id)
        
        # Cache the result
        self._user_xp_cache[cache_key] = total_xp
        self._cache_timestamp[cache_key] = time.time()
        
        return total_xp
    
    async def get_leaderboard(self, bot, limit: int = 10) -> List[tuple]:
        """Get top users by XP with optimized deletion checking"""
        user_xp = {}
        cutoff_date = datetime.now().replace(tzinfo=None) - timedelta(days=7)  # Make timezone-naive
        
        for user_id in self.data["user_projects"].keys():
            if str(user_id) == EMBALL_USER_ID:
                continue

            counted_projects = set()
            
            for project_id in self.data["user_projects"][user_id]:
                if project_id in counted_projects:
                    continue
                
                latest_submission = None
                for msg_id, submission_data in list(self.data["submissions"].items()):
                    try:
                        submission = Submission.from_dict(submission_data)
                    except Exception as e:
                        self.bot.logger.error(MODULE_NAME, f"Skipping malformed submission {msg_id}", e)
                        continue
                    
                    if submission.project_id == project_id and not submission.is_deleted:
                        # Only check recent submissions for deletion
                        try:
                            submission_date = datetime.fromisoformat(submission.created_at).replace(tzinfo=None)
                        except:
                            submission_date = datetime.now().replace(tzinfo=None)
                        
                        if submission_date > cutoff_date and submission.channel_id:
                            try:
                                channel = bot.get_channel(int(submission.channel_id))
                                if channel:
                                    # Use asyncio.wait_for to add timeout
                                    await asyncio.wait_for(
                                        channel.fetch_message(int(msg_id)),
                                        timeout=2.0
                                    )
                            except asyncio.TimeoutError:
                                self.bot.logger.log(MODULE_NAME, 
                                                  f"Timeout checking submission: {msg_id}", "WARNING")
                            except (discord.NotFound, discord.HTTPException, AttributeError):
                                self.bot.logger.log(MODULE_NAME, 
                                                  f"Detected deleted submission: {msg_id}", "WARNING")
                                submission.mark_deleted()
                                self.update_submission(submission)
                                continue
                            except Exception as e:
                                self.bot.logger.error(MODULE_NAME, f"Error checking submission {msg_id}", e)
                        
                        if latest_submission is None or submission.version > latest_submission.version:
                            latest_submission = submission
                
                if latest_submission:
                    if user_id not in user_xp:
                        user_xp[user_id] = 0
                    user_xp[user_id] += latest_submission.calculate_xp()
                    counted_projects.add(project_id)
        
        sorted_users = sorted(user_xp.items(), key=lambda x: x[1], reverse=True)
        return sorted_users[:limit]
    
    def check_file_duplicate(self, file_hash: str, user_id: str) -> Optional[tuple]:
        """Check if file hash exists from different user"""
        existing = self.data["file_hashes"].get(file_hash)
        if existing:
            existing_user_id, existing_msg_id, existing_proj_id = existing
            if existing_user_id != user_id:
                return existing
        return None
    
    def check_link_duplicate(self, link: str, user_id: str) -> Optional[tuple]:
        """Check if link exists from different user"""
        existing = self.data["link_registry"].get(link)
        if existing:
            existing_user_id, existing_msg_id, existing_proj_id = existing
            if existing_user_id != user_id:
                return existing
        return None
    
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
    
    def cleanup_old_deleted_submissions(self, days: int = 30):
        """Remove old deleted submissions to keep database size manageable"""
        cutoff = datetime.now() - timedelta(days=days)
        removed_count = 0
        
        for msg_id in list(self.data["submissions"].keys()):
            submission_data = self.data["submissions"][msg_id]
            try:
                submission = Submission.from_dict(submission_data)
                if submission.is_deleted:
                    deleted_date = datetime.fromisoformat(submission.updated_at)
                    if deleted_date < cutoff:
                        del self.data["submissions"][msg_id]
                        removed_count += 1
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, f"Error during cleanup of {msg_id}", e)
        
        if removed_count > 0:
            self._save()
            self.bot.logger.log(MODULE_NAME, f"Cleaned up {removed_count} old deleted submissions")
        
        return removed_count


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
        
        title_match = re.search(r'^#{1,3}\s+(.+)$', content, re.MULTILINE)
        if not title_match:
            return (False, None, None, [], 
                   "Your project submission must include a title using `# Title`, `## Title`, or `### Title`")
        
        title = title_match.group(1).strip()
        
        description_lines = re.findall(r'^\s*-\s+(.+)$', content, re.MULTILINE)
        if not description_lines:
            return (False, None, None, [], 
                   "Your project submission must include a description with bullet points using `-`")
        
        description = "\n".join(description_lines)
        
        links = SubmissionValidator.extract_links(content)
        has_attachment = len(message.attachments) > 0
        
        # Image-only submissions go to artwork
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
        """Check for duplicate submissions from different users"""
        user_id = str(message.author.id)
        
        for file_hash in file_hashes:
            existing = self.db.check_file_duplicate(file_hash, user_id)
            if existing:
                existing_user_id, existing_msg_id, existing_proj_id = existing
                user = await self.bot.fetch_user(int(existing_user_id))
                return f"This file appears to be copied from <@{existing_user_id}> ({user.display_name})'s submission. Please only submit your own original work."
        
        for link in media_links:
            existing = self.db.check_link_duplicate(link, user_id)
            if existing:
                existing_user_id, existing_msg_id, existing_proj_id = existing
                user = await self.bot.fetch_user(int(existing_user_id))
                return f"This link was already shared by <@{existing_user_id}> ({user.display_name}). Please only submit your own original work."
        
        return None
    
    async def send_error_dm(self, user: discord.User, error_message: str, submission_type: str):
        """Send a friendly error DM to the user"""
        try:
            embed = discord.Embed(
                color=THEME_COLORS["error"]
            )
            embed.description = (
                f"# ❌ Submission Issue\n\n"
                f"{error_message}"
            )
            
            view = InfoButtonsView(self.bot, self.db)
            await user.send(embed=embed, view=view)
            self.bot.logger.log(MODULE_NAME, f"Sent error DM to {user.display_name}")
            
        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME, 
                              f"Could not DM {user.display_name} (DMs disabled)", "WARNING")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send error DM to {user.display_name}", e)
    
    async def send_version_notification(self, user: discord.User, submission: Submission, 
                                       message: discord.Message) -> bool:
        """Send DM about project version update with undo button"""
        try:
            embed = discord.Embed(
                color=THEME_COLORS["primary"]
            )
            embed.description = (
                f"# 🔄 Version Update\n\n"
                f"Your project **{submission.title}** has been updated to `v{submission.version}`\n\n"
                f"## What This Means\n"
                f"• All versions share the same votes & XP\n"
                f"• Prevents leaderboard clutter\n"
                f"• Your previous version is still tracked\n\n"
                f"**Made a mistake?** Click 'Undo' below to register this as a separate project."
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
    
    async def send_linked_notification(self, user: discord.User, linked_msg_id: str):
        """Notify user that their submission was linked to existing artwork"""
        try:
            embed = discord.Embed(
                color=THEME_COLORS["warning"]
            )
            embed.description = (
                "# 🔗 Submissions Linked\n\n"
                "Your new submission shares artwork with an existing post, so their votes have been linked!\n\n"
                "## What This Means\n"
                "• Both submissions share the same vote count\n"
                "• XP is counted once (no double-dipping)\n"
                "• Prevents vote manipulation\n"
                "• Both posts remain visible\n\n"
                "This is automatic and helps maintain fair leaderboard rankings."
            )
            
            view = InfoButtonsView(self.bot, self.db)
            await user.send(embed=embed, view=view)
            
            self.bot.logger.log(MODULE_NAME, f"Sent link notification to {user.display_name}")
            
        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME, 
                              f"Could not DM {user.display_name} (DMs disabled)", "WARNING")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, 
                                f"Failed to send link notification to {user.display_name}", e)
    
    async def process_emball_submission(self, message: discord.Message, submission: Submission):
        """Special handling for Emball's posts"""
        try:
            guild = message.guild
            announcements = discord.utils.get(guild.channels, name=ANNOUNCEMENTS_CHANNEL_NAME)
            
            if not announcements:
                self.bot.logger.log(MODULE_NAME, "Announcements channel not found for Emball forwarding", "WARNING")
                return

            # Check for keywords to ping role
            content_lower = message.content.lower()
            should_ping = any(keyword in content_lower for keyword in EMBALL_KEYWORDS)
            
            ping_text = ""
            if should_ping:
                role = discord.utils.get(guild.roles, name=EMBALL_ROLE_NAME)
                if role:
                    ping_text = f"{role.mention} "
            
            # Construct embed for announcement
            embed = discord.Embed(
                title=f"New Release: {submission.title}",
                description=submission.description,
                color=THEME_COLORS["project"],
                url=message.jump_url
            )
            if submission.thumbnail:
                embed.set_image(url=submission.thumbnail)
            
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
            embed.set_footer(text="Check it out in #projects!")
            
            # Send to announcements
            announcement_msg = await announcements.send(content=ping_text, embed=embed)
            
            # Link the announcement message to the project message for logic consistency
            # (Shares vote count logic, though we won't add reaction buttons to announcement)
            self.db.link_submissions(message.id, str(announcement_msg.id))
            
            self.bot.logger.log(MODULE_NAME, f"Forwarded Emball's project to #announcements: {announcement_msg.id}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to process Emball submission", e)

    async def process_submission(self, message: discord.Message, channel_type: str):
        """Process a submission in projects or artwork channel"""
        try:
            self.bot.logger.log(MODULE_NAME, 
                              f"Processing {channel_type} submission from {message.author.display_name}")
            
            if channel_type == "project":
                is_valid, title, description, media_links, error = SubmissionValidator.validate_project(message)
                thumbnail = None
                
                for att in message.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        thumbnail = att.url
                        break
            else:
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
            
            file_hashes = []
            for att in message.attachments:
                file_hash = await self.compute_file_hash(att)
                if file_hash:
                    file_hashes.append(file_hash)
            
            duplicate_error = await self.check_duplicates(message, file_hashes, media_links)
            if duplicate_error:
                await self.send_error_dm(message.author, duplicate_error, channel_type)
                await message.delete()
                self.bot.logger.log(MODULE_NAME, 
                                  f"Deleted duplicate {channel_type} submission from {message.author.display_name}")
                return
            
            submission_type = SubmissionType.PROJECT if channel_type == "project" else SubmissionType.ARTWORK
            submission, is_new_version, linked_msg_id = self.db.add_submission(
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
            
            if is_new_version:
                await self.send_version_notification(message.author, submission, message)
            
            if linked_msg_id:
                await self.send_linked_notification(message.author, linked_msg_id)
            
            thread_name = title[:100] if title else f"{message.author.display_name}'s artwork"
            if is_new_version:
                thread_name = f"{thread_name} v{submission.version}"
            thread = await message.create_thread(name=thread_name, auto_archive_duration=10080)
            
            await message.add_reaction(REACTION_FIRE)
            await message.add_reaction(REACTION_NEUTRAL)
            await message.add_reaction(REACTION_TRASH)
            
            # Special handling for Emball in projects channel
            if str(message.author.id) == EMBALL_USER_ID and channel_type == "project":
                await self.process_emball_submission(message, submission)

            version_str = f" v{submission.version}" if is_new_version else ""
            self.bot.logger.log(MODULE_NAME, 
                              f"✅ Successfully registered {channel_type} submission: {submission.project_id}{version_str}")
            
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
            
            if submission.submission_type == SubmissionType.PROJECT:
                is_valid, title, description, media_links, error = SubmissionValidator.validate_project(after)
                
                thumbnail = None
                for att in after.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        thumbnail = att.url
                        break
            else:
                is_valid, thumbnail, error = SubmissionValidator.validate_artwork(after)
                title = None
                description = None
                media_links = [thumbnail] if thumbnail else []
            
            if not is_valid:
                submission.mark_deleted()
                self.db.update_submission(submission)
                await after.delete()
                await self.send_error_dm(after.author, 
                    "Your edit made the submission invalid. " + error, 
                    submission.submission_type.value)
                return
            
            file_hashes = []
            for att in after.attachments:
                file_hash = await self.compute_file_hash(att)
                if file_hash:
                    file_hashes.append(file_hash)
            
            submission.update_content(title, description)
            submission.update_media(media_links, thumbnail, file_hashes)
            self.db.update_submission(submission)
            
            self.bot.logger.log(MODULE_NAME, 
                              f"✅ Updated submission {submission.project_id} after edit")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to process edit", e)
    
    async def update_sticky_message(self, channel: discord.TextChannel):
        """Update the sticky dashboard message"""
        if not self.db.is_sticky_enabled():
            return

        try:
            if channel.name == PROJECTS_CHANNEL_NAME:
                embed = discord.Embed(
                    color=THEME_COLORS["project"]
                )
                embed.description = (
                    "# 👋 Welcome to Projects!\n"
                    "Share your creations and get valuable feedback from the community\n\n"
                    "## 📋 How to Submit\n"
                    "```markdown\n"
                    "# Your Project Title\n"
                    "- Feature description\n"
                    "- Another cool feature\n"
                    "[Link](https://your-project.com)\n"
                    "```\n"
                    f"## 🗳️ Voting System\n"
                    f"React with {REACTION_FIRE} {REACTION_NEUTRAL} {REACTION_TRASH} to vote on submissions and give XP to the creator. "
                    f"Engage in discussion threads to award additional XP!"
                )
            else:
                embed = discord.Embed(
                    color=THEME_COLORS["artwork"]
                )
                embed.description = (
                    "# 👋 Welcome to Artwork!\n"
                    "Share your creative work and inspire the community\n\n"
                    "## 📷 How to Submit\n"
                    "Simply attach one or more images to your message. That's it!\n\n"
                    f"## 🗳️ Voting System\n"
                    f"React with {REACTION_FIRE} {REACTION_NEUTRAL} {REACTION_TRASH} to vote on submissions and give XP to the creator. "
                    f"Engage in discussion threads to award additional XP!"
                )
            
            embed.set_footer(text="Embot Community • React to vote on submissions")
            
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
            
    async def run_spotlight_friday(self):
        """Execute Spotlight Friday logic"""
        self.bot.logger.log(MODULE_NAME, "Running Spotlight Friday...")
        
        # 1. Find eligible submissions from last 7 days
        cutoff = datetime.now() - timedelta(days=7)
        eligible_submissions = []
        
        for msg_id, sub_data in self.db.data["submissions"].items():
            sub = Submission.from_dict(sub_data)
            
            # Filter criteria
            if (sub.is_deleted or 
                str(sub.user_id) == EMBALL_USER_ID or 
                datetime.fromisoformat(sub.created_at) < cutoff):
                continue
                
            eligible_submissions.append(sub)
        
        if not eligible_submissions:
            self.bot.logger.log(MODULE_NAME, "No eligible submissions for Spotlight Friday", "WARNING")
            return

        # 2. Sort by votes (Fire - Trash)
        def get_score(s):
             # Calculate pure vote score, ignore thread XP for spotlight to focus on popularity
             return (s.votes.get(REACTION_FIRE, 0) * 5) + (s.votes.get(REACTION_STAR, 0) * 10) - (s.votes.get(REACTION_TRASH, 0) * 5)

        eligible_submissions.sort(key=get_score, reverse=True)
        winner = eligible_submissions[0]
        score = get_score(winner)
        
        if score <= 0:
             self.bot.logger.log(MODULE_NAME, "Top submission has non-positive score, skipping spotlight", "WARNING")
             return

        # 3. Post to Announcements
        try:
            guild = self.bot.guilds[0] # Assuming primary guild
            announcements = discord.utils.get(guild.channels, name=ANNOUNCEMENTS_CHANNEL_NAME)
            if not announcements:
                self.bot.logger.error(MODULE_NAME, f"Could not find #{ANNOUNCEMENTS_CHANNEL_NAME} for Spotlight")
                return

            user = await self.bot.fetch_user(int(winner.user_id))
            
            embed = discord.Embed(
                title=f"🌟 Spotlight Friday: {winner.title or 'Untitled Artwork'}",
                description=f"This week's community favorite!\n\n{winner.description or ''}",
                color=THEME_COLORS["spotlight"],
                url=f"https://discord.com/channels/{guild.id}/{winner.channel_id}/{winner.message_id}"
            )
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            
            if winner.thumbnail:
                embed.set_image(url=winner.thumbnail)
            
            embed.add_field(name="Score", value=f"🔥 {winner.votes.get(REACTION_FIRE, 0)} Votes", inline=True)
            if winner.version != "1.0":
                embed.add_field(name="Version", value=winner.version, inline=True)
                
            embed.set_footer(text="Spotlight Friday • The best of the week!")
            
            await announcements.send(embed=embed)
            self.bot.logger.log(MODULE_NAME, f"Posted Spotlight for {winner.project_id}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to post Spotlight", e)


class InfoButtonsView(discord.ui.View):
    """Reusable view with info buttons for DMs"""
    
    def __init__(self, bot, db: SubmissionDatabase):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
    
    async def show_more_info(self, interaction: discord.Interaction):
        """Show detailed info about Embot projects"""
        embed = discord.Embed(
            color=THEME_COLORS["primary"]
        )
        embed.description = (
            "# ℹ️ Embot Projects Guide\n"
            "Everything you need to know about sharing and earning XP\n\n"
            "## 📋 Project Format\n"
            "**Required Elements:**\n"
            "• Title using `#`, `##`, or `###`\n"
            "• Description with bullet points (`-`)\n"
            "• At least one link, image, or file\n\n"
            "```markdown\n"
            "# My Awesome Game\n"
            "- Built with Unity\n"
            "- 2D platformer mechanics\n"
            "[Play Now](https://example.com)\n"
            "```\n\n"
            f"## 💎 XP System Breakdown\n"
            f"{REACTION_FIRE} **Fire** — Great work!\n"
            f"{REACTION_NEUTRAL} **Neutral** — Seen it\n"
            f"{REACTION_TRASH} **Trash** — Needs work\n"
            f"{REACTION_STAR} **Star** — Amazing! (special)\n"
            f"💬 **Thread Message** — Each reply awards XP\n\n"
            "Note: Each user can only vote once per submission\n\n"
            "## 🔄 Version System\n"
            "Reposting with the **same title**?\n"
            "• Automatically creates new version (v2.0, v3.0...)\n"
            "• *Tip: Put 'vX.X' in your title to set a specific version*\n"
            "• All versions share votes & XP\n"
            "• Prevents leaderboard spam\n"
            "• Option to undo if mistake\n\n"
            "## 🔗 Linked Submissions\n"
            "Reusing artwork as a thumbnail?\n"
            "• Submissions auto-link\n"
            "• Votes shared between them\n"
            "• XP counted once (no double-dipping)\n\n"
            "## 🎨 Artwork Guidelines\n"
            "Post in #artwork:\n"
            "• Just attach images — no formatting needed\n"
            "• Same XP and voting system applies\n"
            "• Can be reused in project posts\n\n"
            "## 🏆 Climbing the Ranks\n"
            "• Earn XP from votes & engagement\n"
            "• Only latest version counts for XP\n"
            "• Check leaderboard anytime\n"
            "• Track stats in 'My Projects'"
        )
        embed.set_footer(text="Embot Community • Version 1.0")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def show_stats(self, interaction: discord.Interaction):
        """Show community statistics"""
        stats = self.db.get_stats()
        
        user_xp = self.db.get_user_xp(str(interaction.user.id))
        user_submissions = len(self.db.data["user_projects"].get(str(interaction.user.id), []))
        
        embed = discord.Embed(
            color=THEME_COLORS["primary"]
        )
        embed.description = (
            "# 📊 Community Stats\n\n"
            f"**{stats['total_submissions']}** submissions · "
            f"**{stats['total_users']}** contributors · "
            f"**{stats['total_votes']}** total votes\n\n"
            f"## Breakdown\n"
            f"🚀 Projects: `{stats['total_projects']}`\n"
            f"🎨 Artwork: `{stats['total_artwork']}`\n\n"
            f"## Your Profile\n"
            f"💎 XP: **{user_xp:.1f}**\n"
            f"📦 Submissions: **{user_submissions}**"
        )
        embed.set_footer(text=f"Stats for @{interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary, emoji="ℹ️")
    async def more_info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Button handler for more info"""
        await self.show_more_info(interaction)
    
    @discord.ui.button(label="View Stats", style=discord.ButtonStyle.secondary, emoji="📊")
    async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Button handler for stats"""
        await self.show_stats(interaction)


class VersionUndoView(discord.ui.View):
    """View with undo button for version updates"""
    
    def __init__(self, bot, db: SubmissionDatabase, message_id: str, user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.message_id = message_id
        self.user_id = user_id
        
        # Add info buttons
        info_view = InfoButtonsView(bot, db)
        for item in info_view.children:
            self.add_item(item)
    
    @discord.ui.button(label="Undo - Register Separately", style=discord.ButtonStyle.danger, emoji="↩️", row=0)
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
            
            submission.version = "1.0"
            submission.project_id = f"project_{submission.user_id}_{submission.message_id}"
            self.db.update_submission(submission)
            
            await interaction.response.send_message(
                f"✅ Project **{submission.title}** is now registered separately with version 1.0",
                ephemeral=False
            )
            
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
    """View with buttons for the sticky message"""
    
    def __init__(self, bot, db: SubmissionDatabase):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
    
    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.primary, emoji="🏆", row=0)
    async def leaderboard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show leaderboard"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get leaderboard directly (it's already async)
            leaderboard = await self.db.get_leaderboard(self.bot, limit=10)
            
            embed = discord.Embed(
                color=THEME_COLORS["gold"]
            )
            
            if not leaderboard:
                embed.description = (
                    "# 🏆 Leaderboard\n\n"
                    "No submissions yet! Be the first to contribute and claim the top spot."
                )
            else:
                leaderboard_lines = []
                medals = {0: "🥇", 1: "🥈", 2: "🥉"}
                
                for i, (user_id, xp) in enumerate(leaderboard):
                    try:
                        user = await self.bot.fetch_user(int(user_id))
                        username = user.display_name
                    except:
                        username = f"User {user_id[-6:]}"
                    
                    if i in medals:
                        prefix = medals[i]
                    else:
                        prefix = f"`#{i+1}`"
                    
                    leaderboard_lines.append(f"{prefix} **{username}** · `{xp:.1f} XP`")
                
                embed.description = (
                    "# 🏆 Community Leaderboard\n"
                    "Top contributors ranked by total XP\n\n"
                    + "\n".join(leaderboard_lines)
                )
            
            user_xp = self.db.get_user_xp(str(interaction.user.id))
            user_submissions = len(self.db.data["user_projects"].get(str(interaction.user.id), []))
            embed.set_footer(
                text=f"Your rank: {user_xp:.1f} XP · {user_submissions} submission(s)",
                icon_url=interaction.user.display_avatar.url
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to show leaderboard", e)
            try:
                await interaction.followup.send("❌ Failed to load leaderboard", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(label="My Projects", style=discord.ButtonStyle.secondary, emoji="📂", row=0)
    async def my_projects_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show user's projects"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            user_id = str(interaction.user.id)
            project_ids = self.db.data["user_projects"].get(user_id, [])
            
            if not project_ids:
                embed = discord.Embed(
                    color=THEME_COLORS["dark"]
                )
                embed.description = (
                    "# 📂 Your Portfolio\n\n"
                    "You haven't submitted anything yet!\n\n"
                    "> Submit a project or artwork to start building your portfolio and earning XP."
                )
                embed.set_footer(text="Get started in #projects or #artwork")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Collect user's submissions (grouped by project_id, showing latest version)
            user_submissions = {}
            for msg_id, sub_data in self.db.data["submissions"].items():
                try:
                    sub = Submission.from_dict(sub_data)
                    if sub.user_id == user_id and not sub.is_deleted:
                        if sub.project_id not in user_submissions or sub.version > user_submissions[sub.project_id].version:
                            user_submissions[sub.project_id] = sub
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, f"Error parsing submission {msg_id} in My Projects", e)
                    continue
            
            if not user_submissions:
                embed = discord.Embed(
                    color=THEME_COLORS["dark"]
                )
                embed.description = (
                    "# 📂 Your Portfolio\n\n"
                    "Your submissions may have been deleted or are being processed.\n\n"
                    "> Submit a project or artwork to start building your portfolio and earning XP."
                )
                embed.set_footer(text="Get started in #projects or #artwork")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            total_xp = self.db.get_user_xp(user_id)
            total_votes = sum(sum(sub.votes.values()) for sub in user_submissions.values())
            
            embed = discord.Embed(
                color=THEME_COLORS["primary"]
            )
            
            # Profile header
            embed.description = (
                f"# 📂 {interaction.user.display_name}'s Portfolio\n\n"
                f"💎 **{total_xp:.1f} XP** · 🗳️ **{total_votes} votes** · 📦 **{len(user_submissions)} submissions**\n\n"
            )
            
            # Show up to 5 most recent projects
            sorted_subs = sorted(user_submissions.values(), 
                               key=lambda x: x.updated_at, reverse=True)[:5]
            
            projects_section = "## Recent Work\n"
            for sub in sorted_subs:
                # Determine type badge
                type_badge = "`🚀 Project`" if sub.submission_type == SubmissionType.PROJECT else "`🎨 Art`"
                
                # Title and version
                title_display = sub.title if sub.title else "Untitled Artwork"
                version_badge = f" `v{sub.version}`" if sub.version != "1.0" else ""
                
                # Calculate XP breakdown
                xp = sub.calculate_xp()
                thread_xp = sub.thread_message_xp
                
                # Vote summary (only show non-zero)
                votes_display = " · ".join(
                    f"{emoji}`{count}`" 
                    for emoji, count in sub.votes.items() 
                    if count > 0
                ) or "_no votes yet_"
                
                # Build project entry
                projects_section += (
                    f"\n### {title_display}{version_badge}\n"
                    f"{type_badge} · `{xp:.1f} XP`"
                )
                
                if thread_xp > 0:
                    projects_section += f" · 💬 `+{thread_xp:.1f}` from {sub.thread_message_count} replies"
                
                projects_section += f"\n{votes_display}\n"
                
                try:
                    timestamp = int(datetime.fromisoformat(sub.updated_at).timestamp())
                    projects_section += f"> Updated <t:{timestamp}:R>\n"
                except:
                    projects_section += f"> Updated recently\n"
            
            embed.description += projects_section
            
            # Footer
            if len(user_submissions) > 5:
                embed.set_footer(
                    text=f"Showing 5 of {len(user_submissions)} submissions",
                    icon_url=interaction.user.display_avatar.url
                )
            else:
                embed.set_footer(
                    text="Your complete portfolio",
                    icon_url=interaction.user.display_avatar.url
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to show user projects", e)
            try:
                await interaction.followup.send("❌ Failed to load your projects", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(label="Stats", style=discord.ButtonStyle.secondary, emoji="📊", row=0)
    async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show community statistics"""
        info_view = InfoButtonsView(self.bot, self.db)
        await info_view.show_stats(interaction)
    
    @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary, emoji="ℹ️", row=0)
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show detailed info"""
        info_view = InfoButtonsView(self.bot, self.db)
        await info_view.show_more_info(interaction)


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

    # --- Spotlight Friday Task ---
    @tasks.loop(minutes=1)
    async def spotlight_checker():
        """Check time for Spotlight Friday (Friday 3PM CST)"""
        try:
            cst_tz = timezone(timedelta(hours=-6))
            now = datetime.now(cst_tz)
            
            if now.weekday() == 4 and now.hour == 15:
                current_week = now.strftime("%Y-%W")
                last_run = db.get_last_spotlight_week()
                
                if current_week != last_run:
                    await manager.run_spotlight_friday()
                    db.set_last_spotlight_week(current_week)
                    
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Error in spotlight checker", e)

    @spotlight_checker.before_loop
    async def before_spotlight():
        await bot.wait_until_ready()

    spotlight_checker.start()
    
    # --- Database Cleanup Task ---
    @tasks.loop(hours=24)
    async def database_cleanup():
        """Clean up old deleted submissions daily"""
        try:
            removed = db.cleanup_old_deleted_submissions(days=30)
            if removed > 0:
                bot.logger.log(MODULE_NAME, f"Daily cleanup removed {removed} old deleted submissions")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Error in database cleanup", e)

    @database_cleanup.before_loop
    async def before_cleanup():
        await bot.wait_until_ready()
        # Wait 1 hour after startup before first cleanup
        await asyncio.sleep(3600)

    database_cleanup.start()
    
    # --- Console Command Handlers ---
    async def handle_toggle_sticky(args):
        """Toggle sticky messages on/off"""
        current_state = db.is_sticky_enabled()
        new_state = not current_state
        db.set_sticky_enabled(new_state)
        
        status_text = "ENABLED" if new_state else "DISABLED"
        print(f"✅ Sticky messages are now {status_text}")
        
        if not new_state:
            for channel_id, msg_id in db.data["sticky_messages"].items():
                try:
                    channel = bot.get_channel(int(channel_id))
                    if channel:
                        msg = await channel.fetch_message(int(msg_id))
                        await msg.delete()
                except:
                    pass
            print("Cleaned up existing sticky messages.")

    async def handle_db_cleanup(args):
        """Manually trigger database cleanup"""
        days = 30
        if args.strip():
            try:
                days = int(args.strip())
            except ValueError:
                print("⚠️ Invalid number of days, using default (30)")
        
        print(f"🔄 Cleaning up submissions deleted more than {days} days ago...")
        removed = db.cleanup_old_deleted_submissions(days)
        
        # Calculate database size
        import os
        db_size = os.path.getsize(DB_FILE) / (1024 * 1024)  # MB
        
        print(f"✅ Removed {removed} old submissions")
        print(f"📊 Database size: {db_size:.2f} MB")
        print(f"📦 Active submissions: {len([s for s in db.data['submissions'].values() if not Submission.from_dict(s).is_deleted])}")
    
    async def handle_db_stats(args):
        """Show detailed database statistics"""
        stats = db.get_stats()
        
        import os
        db_size = os.path.getsize(DB_FILE) / (1024 * 1024)  # MB
        
        deleted_count = len([s for s in db.data['submissions'].values() if Submission.from_dict(s).is_deleted])
        
        print("\n┌─────────────────────────────────────────────────────────────────┐")
        print("│                      DATABASE STATISTICS                        │")
        print("├─────────────────────────────────────────────────────────────────┤")
        print(f"│ File Size: {db_size:.2f} MB{' ' * (49 - len(f'{db_size:.2f} MB'))}│")
        print(f"│ Active Submissions: {stats['total_submissions']}{' ' * (44 - len(str(stats['total_submissions'])))}│")
        print(f"│ Deleted Submissions: {deleted_count}{' ' * (43 - len(str(deleted_count)))}│")
        print(f"│ Projects: {stats['total_projects']}{' ' * (50 - len(str(stats['total_projects'])))}│")
        print(f"│ Artwork: {stats['total_artwork']}{' ' * (51 - len(str(stats['total_artwork'])))}│")
        print(f"│ Users: {stats['total_users']}{' ' * (53 - len(str(stats['total_users'])))}│")
        print(f"│ Total Votes: {stats['total_votes']}{' ' * (47 - len(str(stats['total_votes'])))}│")
        print(f"│ File Hashes: {len(db.data['file_hashes'])}{' ' * (47 - len(str(len(db.data['file_hashes']))))}│")
        print(f"│ Link Registry: {len(db.data['link_registry'])}{' ' * (45 - len(str(len(db.data['link_registry']))))}│")
        print("└─────────────────────────────────────────────────────────────────┘\n")

    if hasattr(bot, 'console_commands'):
        bot.console_commands['toggle_sticky'] = {
            'description': 'Toggle dashboard sticky messages on/off',
            'handler': handle_toggle_sticky
        }
        bot.console_commands['db_cleanup'] = {
            'description': 'Clean up old deleted submissions [days]',
            'handler': handle_db_cleanup
        }
        bot.console_commands['db_stats'] = {
            'description': 'Show detailed database statistics',
            'handler': handle_db_stats
        }
    
    # [Rest of the event listeners remain the same]
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
            
            count = 0
            for reaction in message.reactions:
                if str(reaction.emoji) == emoji:
                    count = reaction.count
                    break
            
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
    
    @bot.listen('on_message')
    async def on_thread_message(message: discord.Message):
        """Award XP for messages in submission threads"""
        try:
            if message.author.bot:
                return
            
            if not isinstance(message.channel, discord.Thread):
                return
            
            parent_message_id = str(message.channel.id)
            
            if message.channel.parent:
                try:
                    parent_channel = message.channel.parent
                    if parent_channel.name.lower() not in [PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME]:
                        return
                    
                    starter_message = message.channel.starter_message
                    if not starter_message:
                        starter_message = await message.channel.parent.fetch_message(message.channel.id)
                    
                    submission = db.get_submission(str(starter_message.id))
                    if submission and not submission.is_deleted:
                        db.add_thread_message_xp(str(starter_message.id))
                        
                except (discord.NotFound, discord.HTTPException):
                    pass
                    
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to process thread message", e)
    
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
            
            if not db.is_sticky_enabled():
                await interaction.response.send_message(
                    "❌ Sticky messages are currently disabled in settings.",
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