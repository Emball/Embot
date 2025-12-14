import discord
from discord.ext import tasks
from discord.ui import View, Button
import os
import random
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone
import asyncio
import aiohttp
import base64
import subprocess
import json
from collections import Counter
import re
import math

MODULE_NAME = "VMS"

# Configuration
VMS_ROOT = Path("data/voice_messages")
CACHE_DIR = VMS_ROOT / "cache"
ARCHIVE_DIR = VMS_ROOT / "archived"
TRANSCRIPTS_FILE = VMS_ROOT / "transcripts.json"
VM_TRACKING_FILE = VMS_ROOT / "vm_tracking.json"  # NEW: Track VM usage
GENERAL_CHANNEL_NAME = "general"

# Message-based thresholds
MIN_MESSAGES_BETWEEN = 40
MAX_MESSAGES_BETWEEN = 80
INACTIVITY_TIMEOUT_HOURS = 2

# Time thresholds
CACHE_DAYS = 150
ARCHIVE_DAYS = 365
ARCHIVE_CHANCE = 0.15

# Cooldown configuration (prevents spam)
PING_COOLDOWN_SECONDS = 30
RANDOM_VM_COOLDOWN_SECONDS = 60  # 1 minute between random VMs

# Sentience configuration
RELEVANT_VM_CHANCE = 0.5  # 50% chance to use relevant VM
MESSAGE_CONTEXT_COUNT = 20  # Look at last 20 messages for context
MIN_KEYWORD_MATCHES = 2  # Minimum keyword matches to consider relevant

# NEW: VM Selection Configuration
VM_COOLDOWN_DAYS = 7  # Don't repeat same VM within 7 days
LONG_VM_THRESHOLD_SECONDS = 60  # VMs longer than 1 minute are "long"
LONG_VM_REDUCTION_FACTOR = 0.3  # Reduce selection chance by 70% for long VMs
MAX_SCORE_WEIGHT = 0.6  # Maximum weight for keyword score vs recency/cooldown
KEYWORD_SCORE_NORMALIZATION = True  # Normalize keyword scores by transcript length
MIN_TRANSCRIPT_LENGTH_CHARS = 10  # Minimum transcript length to consider


class VMSManager:
    """Manages voice message caching, archiving, and intelligent playback"""
    
    def __init__(self, bot):
        self.bot = bot
        self.last_post_time = None
        self.last_ping_response_time = None
        self.message_count = 0
        self.target_message_count = 0
        self.last_message_time = None
        self.posting_lock = asyncio.Lock()
        
        # Transcript management
        self.transcripts = {}  # {file_path: {text, language, keywords}}
        self.transcription_queue = asyncio.Queue()
        self.background_transcription_active = False
        
        # NEW: VM usage tracking
        self.vm_tracking = {}  # {file_path: [last_used_timestamp1, last_used_timestamp2, ...]}
        
        self._setup_directories()
        self._load_transcripts()
        self._load_vm_tracking()  # NEW: Load tracking data
        self._schedule_next_post()
    
    def _setup_directories(self):
        """Create directory structure if it doesn't exist"""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.bot.logger.log(MODULE_NAME, f"Directory structure ready: {VMS_ROOT}")
    
    def _load_transcripts(self):
        """Load existing transcripts from JSON file"""
        try:
            if TRANSCRIPTS_FILE.exists():
                with open(TRANSCRIPTS_FILE, 'r', encoding='utf-8') as f:
                    self.transcripts = json.load(f)
                self.bot.logger.log(MODULE_NAME, f"Loaded {len(self.transcripts)} existing transcripts")
            else:
                self.transcripts = {}
                self.bot.logger.log(MODULE_NAME, "No existing transcripts found, starting fresh")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error loading transcripts", e)
            self.transcripts = {}
    
    # NEW: Load VM tracking data
    def _load_vm_tracking(self):
        """Load VM usage tracking data from JSON file"""
        try:
            if VM_TRACKING_FILE.exists():
                with open(VM_TRACKING_FILE, 'r', encoding='utf-8') as f:
                    self.vm_tracking = json.load(f)
                self.bot.logger.log(MODULE_NAME, f"Loaded tracking data for {len(self.vm_tracking)} VMs")
            else:
                self.vm_tracking = {}
                self.bot.logger.log(MODULE_NAME, "No existing VM tracking data found")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error loading VM tracking data", e)
            self.vm_tracking = {}
    
    # NEW: Save VM tracking data
    def _save_vm_tracking(self):
        """Save VM usage tracking to JSON file"""
        try:
            with open(VM_TRACKING_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.vm_tracking, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error saving VM tracking data", e)
    
    # NEW: Record VM usage
    def record_vm_usage(self, vm_path):
        """Record when a VM was played"""
        try:
            vm_key = str(vm_path)
            now = datetime.now(timezone.utc).isoformat()
            
            if vm_key not in self.vm_tracking:
                self.vm_tracking[vm_key] = []
            
            self.vm_tracking[vm_key].append(now)
            
            # Keep only last 10 usages to avoid file bloat
            if len(self.vm_tracking[vm_key]) > 10:
                self.vm_tracking[vm_key] = self.vm_tracking[vm_key][-10:]
            
            self._save_vm_tracking()
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error recording VM usage for {vm_path}", e)
    
    # NEW: Check if VM is in cooldown
    def is_vm_in_cooldown(self, vm_path):
        """Check if a VM was used recently (within cooldown period)"""
        try:
            vm_key = str(vm_path)
            
            if vm_key not in self.vm_tracking or not self.vm_tracking[vm_key]:
                return False
            
            # Get most recent usage
            last_used_str = self.vm_tracking[vm_key][-1]
            last_used = datetime.fromisoformat(last_used_str.replace('Z', '+00:00'))
            
            time_since = datetime.now(timezone.utc) - last_used
            return time_since.days < VM_COOLDOWN_DAYS
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error checking VM cooldown for {vm_path}", e)
            return False
    
    # NEW: Check if VM is too long
    def is_vm_too_long(self, vm_path):
        """Check if a VM exceeds the length threshold"""
        try:
            duration = self._get_audio_duration(vm_path)
            return duration > LONG_VM_THRESHOLD_SECONDS
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Could not check duration for {vm_path.name}, assuming normal length", "WARNING")
            return False
    
    def _save_transcripts(self):
        """Save transcripts to JSON file"""
        try:
            with open(TRANSCRIPTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.transcripts, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error saving transcripts", e)
    
    def _extract_keywords(self, text):
        """Extract meaningful keywords from transcript text"""
        # Remove common words and punctuation
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 
                     'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been',
                     'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
                     'could', 'should', 'may', 'might', 'can', 'this', 'that', 'these',
                     'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they', 'what', 'which',
                     'who', 'when', 'where', 'why', 'how', 'all', 'each', 'every', 'both',
                     'few', 'more', 'most', 'some', 'such', 'no', 'nor', 'not', 'only',
                     'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'just', 'don',
                     'now', 'oh', 'yeah', 'um', 'uh', 'like', 'know', 'get', 'got', 'going'}
        
        # Convert to lowercase and split into words
        words = re.findall(r'\b[a-z]{3,}\b', text.lower())
        
        # Filter out stop words and get unique keywords
        keywords = [w for w in words if w not in stop_words]
        
        return keywords
    
    def save_transcript(self, vm_path, transcript_result):
        """Save transcript for a VM file"""
        try:
            # Convert Path to string for JSON serialization
            vm_key = str(vm_path)
            
            keywords = self._extract_keywords(transcript_result['text'])
            
            self.transcripts[vm_key] = {
                'text': transcript_result['text'],
                'language': transcript_result.get('language', 'unknown'),
                'keywords': keywords,
                'transcribed_at': datetime.now(timezone.utc).isoformat()
            }
            
            self._save_transcripts()
            
            self.bot.logger.log(MODULE_NAME, 
                f"Saved transcript for {vm_path.name} ({len(keywords)} keywords)")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error saving transcript for {vm_path}", e)
    
    def get_transcript(self, vm_path):
        """Get transcript for a VM file"""
        vm_key = str(vm_path)
        return self.transcripts.get(vm_key)
    
    def _schedule_next_post(self):
        """Schedule the next VM post with random message count"""
        self.target_message_count = random.randint(MIN_MESSAGES_BETWEEN, MAX_MESSAGES_BETWEEN)
        self.message_count = 0
        self.bot.logger.log(MODULE_NAME, 
            f"Next VM scheduled after {self.target_message_count} messages")
    
    def _get_file_creation_time(self, file_path):
        """Get the original creation time of a file"""
        try:
            stat = file_path.stat()
            if hasattr(stat, 'st_birthtime'):
                return datetime.fromtimestamp(stat.st_birthtime, timezone.utc)
            else:
                return datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error getting file time for {file_path}", e)
            return datetime.now(timezone.utc)
    
    def _preserve_file_times(self, source, destination):
        """Copy file while preserving timestamps"""
        try:
            shutil.copy2(source, destination)
            stat = source.stat()
            os.utime(destination, (stat.st_atime, stat.st_mtime))
            self.bot.logger.log(MODULE_NAME, f"Preserved timestamps for {destination.name}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error preserving file times", e)
    
    def get_vm_files(self, directory):
        """Get all voice message files from a directory with their creation times"""
        files = []
        for file in directory.iterdir():
            if file.is_file() and file.suffix.lower() in ['.ogg', '.mp3', '.m4a', '.wav']:
                creation_time = self._get_file_creation_time(file)
                files.append((file, creation_time))
        return files
    
    def _get_audio_duration(self, file_path):
        """Get audio duration using ffprobe"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)],
                capture_output=True,
                text=True,
                timeout=10
            )
            duration = float(result.stdout.strip())
            return duration
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Could not get duration for {file_path.name}, using default", "WARNING")
            return 1.0
    
    def _generate_waveform(self):
        """Generate a dummy waveform for voice messages"""
        waveform_bytes = bytes([random.randint(0, 255) for _ in range(256)])
        return base64.b64encode(waveform_bytes).decode('utf-8')
    
    async def _send_as_voice_message(self, channel, file_path):
        """Send a file as a Discord voice message using the API"""
        try:
            file_size = file_path.stat().st_size
            duration = self._get_audio_duration(file_path)
            
            async with aiohttp.ClientSession() as session:
                upload_request_url = f"https://discord.com/api/v10/channels/{channel.id}/attachments"
                
                upload_request_data = {
                    "files": [
                        {
                            "filename": "voice-message.ogg",
                            "file_size": file_size,
                            "id": "2"
                        }
                    ]
                }
                
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bot {self.bot.http.token}"
                }
                
                async with session.post(upload_request_url, json=upload_request_data, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.bot.logger.error(MODULE_NAME, f"Failed to get upload URL: {resp.status} - {error_text}")
                        return None
                    
                    upload_data = await resp.json()
                    upload_url = upload_data['attachments'][0]['upload_url']
                    upload_filename = upload_data['attachments'][0]['upload_filename']
                
                with open(file_path, 'rb') as f:
                    file_data = f.read()
                
                upload_headers = {
                    "Content-Type": "audio/ogg",
                }
                
                async with session.put(upload_url, data=file_data, headers=upload_headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.bot.logger.error(MODULE_NAME, f"Failed to upload file: {resp.status} - {error_text}")
                        return None
                
                message_url = f"https://discord.com/api/v10/channels/{channel.id}/messages"
                waveform = self._generate_waveform()
                
                message_data = {
                    "flags": 8192,
                    "attachments": [
                        {
                            "id": "0",
                            "filename": "voice-message.ogg",
                            "uploaded_filename": upload_filename,
                            "duration_secs": duration,
                            "waveform": waveform
                        }
                    ]
                }
                
                async with session.post(message_url, json=message_data, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.bot.logger.error(MODULE_NAME, f"Failed to send voice message: {resp.status} - {error_text}")
                        return None
                    
                    message_data = await resp.json()
                
                # NEW: Record VM usage after successful send
                self.record_vm_usage(file_path)
                
                self.bot.logger.log(MODULE_NAME, f"Successfully sent voice message: {file_path.name}")
                return message_data
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error sending voice message", e)
            return None
    
    async def _send_as_voice_message_reply(self, message, file_path):
        """Send a file as a Discord voice message reply using the API"""
        try:
            file_size = file_path.stat().st_size
            duration = self._get_audio_duration(file_path)
            
            async with aiohttp.ClientSession() as session:
                upload_request_url = f"https://discord.com/api/v10/channels/{message.channel.id}/attachments"
                
                upload_request_data = {
                    "files": [
                        {
                            "filename": "voice-message.ogg",
                            "file_size": file_size,
                            "id": "2"
                        }
                    ]
                }
                
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bot {self.bot.http.token}"
                }
                
                async with session.post(upload_request_url, json=upload_request_data, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.bot.logger.error(MODULE_NAME, f"Failed to get upload URL: {resp.status} - {error_text}")
                        return None
                    
                    upload_data = await resp.json()
                    upload_url = upload_data['attachments'][0]['upload_url']
                    upload_filename = upload_data['attachments'][0]['upload_filename']
                
                with open(file_path, 'rb') as f:
                    file_data = f.read()
                
                upload_headers = {
                    "Content-Type": "audio/ogg",
                }
                
                async with session.put(upload_url, data=file_data, headers=upload_headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.bot.logger.error(MODULE_NAME, f"Failed to upload file: {resp.status} - {error_text}")
                        return None
                
                message_url = f"https://discord.com/api/v10/channels/{message.channel.id}/messages"
                waveform = self._generate_waveform()
                
                message_data = {
                    "flags": 8192,
                    "message_reference": {
                        "message_id": message.id
                    },
                    "attachments": [
                        {
                            "id": "0",
                            "filename": "voice-message.ogg",
                            "uploaded_filename": upload_filename,
                            "duration_secs": duration,
                            "waveform": waveform
                        }
                    ]
                }
                
                async with session.post(message_url, json=message_data, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.bot.logger.error(MODULE_NAME, f"Failed to send voice message reply: {resp.status} - {error_text}")
                        return None
                    
                    reply_data = await resp.json()
                
                # NEW: Record VM usage after successful send
                self.record_vm_usage(file_path)
                
                self.bot.logger.log(MODULE_NAME, f"Successfully sent voice message reply: {file_path.name}")
                return reply_data
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error sending voice message reply", e)
            return None
    
    async def cleanup_and_archive(self):
        """Move old cache files to archive and delete old archive files"""
        now = datetime.now(timezone.utc)
        moved_count = 0
        deleted_count = 0
        
        try:
            cache_files = self.get_vm_files(CACHE_DIR)
            for file_path, creation_time in cache_files:
                age_days = (now - creation_time).days
                
                if age_days >= CACHE_DAYS:
                    archive_path = ARCHIVE_DIR / file_path.name
                    
                    if archive_path.exists():
                        stem = archive_path.stem
                        suffix = archive_path.suffix
                        timestamp = creation_time.strftime("%Y%m%d_%H%M%S")
                        archive_path = ARCHIVE_DIR / f"{stem}_{timestamp}{suffix}"
                    
                    self._preserve_file_times(file_path, archive_path)
                    
                    # Move transcript reference
                    old_key = str(file_path)
                    new_key = str(archive_path)
                    if old_key in self.transcripts:
                        self.transcripts[new_key] = self.transcripts.pop(old_key)
                    
                    # Move tracking data
                    if old_key in self.vm_tracking:
                        self.vm_tracking[new_key] = self.vm_tracking.pop(old_key)
                    
                    file_path.unlink()
                    
                    moved_count += 1
                    self.bot.logger.log(MODULE_NAME, 
                        f"Archived {file_path.name} (age: {age_days} days)")
            
            archive_files = self.get_vm_files(ARCHIVE_DIR)
            for file_path, creation_time in archive_files:
                age_days = (now - creation_time).days
                
                if age_days >= ARCHIVE_DAYS:
                    # Remove transcript
                    vm_key = str(file_path)
                    if vm_key in self.transcripts:
                        del self.transcripts[vm_key]
                    
                    # Remove tracking data
                    if vm_key in self.vm_tracking:
                        del self.vm_tracking[vm_key]
                    
                    file_path.unlink()
                    
                    deleted_count += 1
                    self.bot.logger.log(MODULE_NAME, 
                        f"Deleted {file_path.name} from archive (age: {age_days} days)")
            
            if moved_count > 0 or deleted_count > 0:
                self._save_transcripts()
                self._save_vm_tracking()
                self.bot.logger.log(MODULE_NAME, 
                    f"Cleanup complete: {moved_count} archived, {deleted_count} deleted")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error during cleanup", e)
    
    def is_voice_message(self, message):
        """Check if a message is a voice message"""
        if not message.attachments:
            return False
        
        for attachment in message.attachments:
            if hasattr(attachment, 'is_voice_message') and attachment.is_voice_message():
                return True
            if attachment.content_type and 'audio' in attachment.content_type:
                if attachment.waveform is not None:
                    return True
        
        return False
    
    async def save_voice_message(self, message):
        """Save a voice message from Discord to cache and return the file path"""
        if not self.is_voice_message(message):
            return None
        
        try:
            attachment = message.attachments[0]
            
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            author_name = message.author.name.replace(' ', '_')[:20]
            
            ext = Path(attachment.filename).suffix or '.ogg'
            filename = f"vm_{author_name}_{timestamp}{ext}"
            file_path = CACHE_DIR / filename
            
            await attachment.save(file_path)
            
            msg_time = message.created_at.timestamp()
            os.utime(file_path, (msg_time, msg_time))
            
            self.bot.logger.log(MODULE_NAME, 
                f"Saved voice message from {message.author}: {filename}")
            return file_path
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error saving voice message", e)
            return None
    
    async def get_recent_messages_context(self, channel, limit=MESSAGE_CONTEXT_COUNT):
        """Get recent messages from channel for context matching"""
        try:
            messages = []
            async for msg in channel.history(limit=limit):
                if msg.content and not msg.author.bot:
                    messages.append(msg.content)
            return messages
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error getting message context", e)
            return []
    
    # UPDATED: Improved relevant VM selection with normalization and cooldowns
    def select_relevant_vm(self, context_messages):
        """Select a VM that's relevant to recent conversation context with fairness improvements"""
        try:
            # Extract keywords from context
            context_text = " ".join(context_messages)
            context_keywords = self._extract_keywords(context_text)
            
            if not context_keywords:
                self.bot.logger.log(MODULE_NAME, "No context keywords, falling back to random")
                return None
            
            # Count keyword frequency in context
            context_word_freq = Counter(context_keywords)
            total_context_words = len(context_keywords)
            
            if total_context_words == 0:
                self.bot.logger.log(MODULE_NAME, "No valid context keywords, falling back to random")
                return None
            
            # NEW: Collect all eligible VMs with their scores
            eligible_vms = []
            
            # Check cache files
            cache_files = self.get_vm_files(CACHE_DIR)
            for file_path, creation_time in cache_files:
                # Skip VMs in cooldown
                if self.is_vm_in_cooldown(file_path):
                    continue
                
                # Skip excessively long VMs (reduce chance instead of skipping entirely)
                is_long = self.is_vm_too_long(file_path)
                
                transcript = self.get_transcript(file_path)
                if transcript and 'keywords' in transcript:
                    # NEW: Calculate normalized match score
                    vm_keywords = transcript['keywords']
                    
                    if not vm_keywords or len(vm_keywords) < MIN_TRANSCRIPT_LENGTH_CHARS:
                        continue
                    
                    # Calculate raw match count
                    raw_matches = sum(
                        context_word_freq.get(keyword, 0) 
                        for keyword in vm_keywords
                    )
                    
                    if raw_matches < MIN_KEYWORD_MATCHES:
                        continue
                    
                    # NEW: Normalize by transcript length to prevent long VMs from dominating
                    if KEYWORD_SCORE_NORMALIZATION:
                        # Use log normalization to reduce bias from long transcripts
                        keyword_count = len(vm_keywords)
                        normalized_matches = raw_matches / math.log(keyword_count + 1)
                        
                        # Also normalize by context length
                        normalized_score = normalized_matches / math.log(total_context_words + 1)
                    else:
                        normalized_score = raw_matches / total_context_words
                    
                    # Apply penalty for long VMs
                    final_score = normalized_score
                    if is_long:
                        final_score *= LONG_VM_REDUCTION_FACTOR
                    
                    # NEW: Apply recency bonus (newer VMs get slight preference)
                    age_days = (datetime.now(timezone.utc) - creation_time).days
                    recency_factor = max(0.5, 1.0 - (age_days / 365))  # VMs up to 1 year old
                    
                    # Combine scores with weights
                    combined_score = (
                        final_score * MAX_SCORE_WEIGHT + 
                        recency_factor * (1 - MAX_SCORE_WEIGHT)
                    )
                    
                    eligible_vms.append((file_path, combined_score, is_long, raw_matches))
            
            # Check archive files (with archive chance)
            if random.random() < ARCHIVE_CHANCE:
                archive_files = self.get_vm_files(ARCHIVE_DIR)
                for file_path, creation_time in archive_files:
                    if self.is_vm_in_cooldown(file_path):
                        continue
                    
                    is_long = self.is_vm_too_long(file_path)
                    
                    transcript = self.get_transcript(file_path)
                    if transcript and 'keywords' in transcript:
                        vm_keywords = transcript['keywords']
                        
                        if not vm_keywords or len(vm_keywords) < MIN_TRANSCRIPT_LENGTH_CHARS:
                            continue
                        
                        raw_matches = sum(
                            context_word_freq.get(keyword, 0) 
                            for keyword in vm_keywords
                        )
                        
                        if raw_matches < MIN_KEYWORD_MATCHES:
                            continue
                        
                        if KEYWORD_SCORE_NORMALIZATION:
                            keyword_count = len(vm_keywords)
                            normalized_matches = raw_matches / math.log(keyword_count + 1)
                            normalized_score = normalized_matches / math.log(total_context_words + 1)
                        else:
                            normalized_score = raw_matches / total_context_words
                        
                        final_score = normalized_score
                        if is_long:
                            final_score *= LONG_VM_REDUCTION_FACTOR
                        
                        # Archive penalty (slight reduction)
                        final_score *= 0.8
                        
                        age_days = (datetime.now(timezone.utc) - creation_time).days
                        recency_factor = max(0.3, 1.0 - (age_days / 730))  # VMs up to 2 years old
                        
                        combined_score = (
                            final_score * MAX_SCORE_WEIGHT + 
                            recency_factor * (1 - MAX_SCORE_WEIGHT)
                        )
                        
                        eligible_vms.append((file_path, combined_score, is_long, raw_matches))
            
            if not eligible_vms:
                self.bot.logger.log(MODULE_NAME, "No relevant VMs found, falling back to random")
                return None
            
            # NEW: Sort by score and use weighted random selection from top candidates
            eligible_vms.sort(key=lambda x: x[1], reverse=True)
            
            # Take top candidates (more for better randomness, but not too many)
            top_candidates = eligible_vms[:10] if len(eligible_vms) > 10 else eligible_vms
            
            # Apply weights for random selection (higher score = higher chance)
            weights = [score ** 2 for _, score, _, _ in top_candidates]  # Square to emphasize differences
            
            # Select weighted random
            selected_index = random.choices(range(len(top_candidates)), weights=weights, k=1)[0]
            selected, score, is_long, raw_matches = top_candidates[selected_index]
            
            # Log selection details
            duration = self._get_audio_duration(selected)
            self.bot.logger.log(MODULE_NAME, 
                f"Selected relevant VM: {selected.name} "
                f"(score: {score:.3f}, matches: {raw_matches}, "
                f"duration: {duration:.1f}s, long: {is_long})")
            
            return selected
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error selecting relevant VM", e)
            return None
    
    # UPDATED: Improved random VM selection with cooldowns and length consideration
    def select_random_vm(self):
        """Select a random voice message, with consideration for cooldowns and length"""
        try:
            use_archive = random.random() < ARCHIVE_CHANCE
            
            # Collect eligible VMs
            eligible_vms = []
            
            if use_archive:
                archive_files = self.get_vm_files(ARCHIVE_DIR)
                for file_path, creation_time in archive_files:
                    # Skip VMs in cooldown
                    if self.is_vm_in_cooldown(file_path):
                        continue
                    
                    # Apply length penalty
                    is_long = self.is_vm_too_long(file_path)
                    age_days = (datetime.now(timezone.utc) - creation_time).days
                    
                    # Calculate weight: newer VMs have higher chance
                    weight = max(0.3, 1.0 - (age_days / 730))  # Up to 2 years
                    if is_long:
                        weight *= LONG_VM_REDUCTION_FACTOR
                    
                    eligible_vms.append((file_path, weight, is_long))
            
            # Always include cache files (if no eligible archive files or as fallback)
            cache_files = self.get_vm_files(CACHE_DIR)
            for file_path, creation_time in cache_files:
                if self.is_vm_in_cooldown(file_path):
                    continue
                
                is_long = self.is_vm_too_long(file_path)
                age_days = (datetime.now(timezone.utc) - creation_time).days
                
                weight = max(0.5, 1.0 - (age_days / 365))  # Up to 1 year
                if is_long:
                    weight *= LONG_VM_REDUCTION_FACTOR
                
                eligible_vms.append((file_path, weight, is_long))
            
            if not eligible_vms:
                self.bot.logger.log(MODULE_NAME, "No eligible VMs available (all in cooldown?)", "WARNING")
                
                # Emergency fallback: allow VMs in cooldown
                all_files = self.get_vm_files(CACHE_DIR)
                if use_archive:
                    all_files.extend(self.get_vm_files(ARCHIVE_DIR))
                
                if all_files:
                    selected, creation_time = random.choice(all_files)
                    age_days = (datetime.now(timezone.utc) - creation_time).days
                    duration = self._get_audio_duration(selected)
                    self.bot.logger.log(MODULE_NAME, 
                        f"Emergency fallback: {selected.name} (age: {age_days} days, duration: {duration:.1f}s)")
                    return selected
                else:
                    self.bot.logger.log(MODULE_NAME, "No voice messages available at all", "WARNING")
                    return None
            
            # Weighted random selection
            weights = [weight for _, weight, _ in eligible_vms]
            selected_index = random.choices(range(len(eligible_vms)), weights=weights, k=1)[0]
            selected, weight, is_long = eligible_vms[selected_index]
            
            creation_time = self._get_file_creation_time(selected)
            age_days = (datetime.now(timezone.utc) - creation_time).days
            duration = self._get_audio_duration(selected)
            
            source = "archive" if use_archive and selected.parent == ARCHIVE_DIR else "cache"
            self.bot.logger.log(MODULE_NAME, 
                f"Selected from {source}: {selected.name} "
                f"(age: {age_days} days, duration: {duration:.1f}s, "
                f"long: {is_long}, weight: {weight:.3f})")
            
            return selected
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error selecting VM", e)
            return None
    
    def can_respond_to_ping(self):
        """Check if enough time has passed since last ping response"""
        if self.last_ping_response_time is None:
            return True
        
        time_since = (datetime.now(timezone.utc) - self.last_ping_response_time).total_seconds()
        return time_since >= PING_COOLDOWN_SECONDS
    
    def can_post_random_vm(self):
        """Check if enough time has passed since last random VM post"""
        if self.last_post_time is None:
            return True
        
        time_since = (datetime.now(timezone.utc) - self.last_post_time).total_seconds()
        return time_since >= RANDOM_VM_COOLDOWN_SECONDS
    
    async def on_general_message(self, message):
        """Track messages in general channel and post VMs when threshold is reached"""
        try:
            if message.channel.name != GENERAL_CHANNEL_NAME:
                return
        
            self.last_message_time = datetime.now(timezone.utc)
            self.message_count += 1
        
            if self.message_count >= self.target_message_count:
                if not self.can_post_random_vm():
                    self.bot.logger.log(MODULE_NAME, 
                        f"Cooldown active, skipping scheduled VM")
                    self._schedule_next_post()
                    return
                
                self.bot.logger.log(MODULE_NAME, 
                    f"Message threshold reached ({self.message_count}/{self.target_message_count})")
                await self.post_random_vm()
    
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in on_general_message", e)
    
    def is_channel_inactive(self):
        """Check if the channel has been inactive for too long"""
        if self.last_message_time is None:
            return False
        
        time_since_last = datetime.now(timezone.utc) - self.last_message_time
        return time_since_last > timedelta(hours=INACTIVITY_TIMEOUT_HOURS)
    
    async def post_random_vm(self, reply_to=None, is_ping_response=False):
        """Post a VM - either random or contextually relevant"""
        if self.posting_lock.locked():
            self.bot.logger.log(MODULE_NAME, "Post already in progress, skipping duplicate")
            return False
        
        async with self.posting_lock:
            try:
                # Check cooldowns
                if is_ping_response:
                    if not self.can_respond_to_ping():
                        cooldown_remaining = PING_COOLDOWN_SECONDS - (datetime.now(timezone.utc) - self.last_ping_response_time).total_seconds()
                        self.bot.logger.log(MODULE_NAME, 
                            f"Ping cooldown active ({cooldown_remaining:.0f}s remaining)")
                        return False
                else:
                    if not self.can_post_random_vm():
                        cooldown_remaining = RANDOM_VM_COOLDOWN_SECONDS - (datetime.now(timezone.utc) - self.last_post_time).total_seconds()
                        self.bot.logger.log(MODULE_NAME, 
                            f"Random VM cooldown active ({cooldown_remaining:.0f}s remaining)")
                        return False
                
                # Find target channel
                if reply_to:
                    target_channel = reply_to.channel
                else:
                    general_channel = None
                    for guild in self.bot.guilds:
                        channel = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL_NAME)
                        if channel:
                            general_channel = channel
                            break
                    
                    if not general_channel:
                        self.bot.logger.log(MODULE_NAME, 
                            f"Channel '{GENERAL_CHANNEL_NAME}' not found", "WARNING")
                        return False
                    
                    target_channel = general_channel
                
                # Decide between relevant and random VM
                use_relevant = random.random() < RELEVANT_VM_CHANCE
                vm_file = None
                
                if use_relevant:
                    self.bot.logger.log(MODULE_NAME, "Attempting to select relevant VM")
                    context_messages = await self.get_recent_messages_context(target_channel)
                    if context_messages:
                        vm_file = self.select_relevant_vm(context_messages)
                
                # Fall back to random if relevant selection failed
                if not vm_file:
                    self.bot.logger.log(MODULE_NAME, "Selecting random VM")
                    vm_file = self.select_random_vm()
                
                if not vm_file:
                    self.bot.logger.log(MODULE_NAME, "No VM to post", "WARNING")
                    return False
                
                # Send VM
                if reply_to:
                    await self._send_as_voice_message_reply(reply_to, vm_file)
                    self.bot.logger.log(MODULE_NAME, "Posted VM reply")
                else:
                    await self._send_as_voice_message(target_channel, vm_file)
                    self.bot.logger.log(MODULE_NAME, "Posted VM")
                
                # Update timestamps
                if is_ping_response:
                    self.last_ping_response_time = datetime.now(timezone.utc)
                else:
                    self.last_post_time = datetime.now(timezone.utc)
                    self._schedule_next_post()
                
                return True
                
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Error posting VM", e)
                return False
    
    async def background_transcribe_all(self):
        """Background task to transcribe all untranscribed VMs - ADMIN ONLY"""
        self.bot.logger.log(MODULE_NAME, "Starting background transcription of all VMs")
        self.background_transcription_active = True
        
        try:
            # Get all VM files
            all_files = []
            all_files.extend(self.get_vm_files(CACHE_DIR))
            all_files.extend(self.get_vm_files(ARCHIVE_DIR))
            
            untranscribed = []
            for file_path, _ in all_files:
                if not self.get_transcript(file_path):
                    untranscribed.append(file_path)
            
            total = len(untranscribed)
            if total == 0:
                self.bot.logger.log(MODULE_NAME, "All VMs already transcribed")
                self.background_transcription_active = False
                return
            
            self.bot.logger.log(MODULE_NAME, 
                f"Found {total} untranscribed VMs, starting background processing")
            
            processed = 0
            failed = 0
            
            # Get transcribe manager
            if not hasattr(self.bot, 'transcribe_manager'):
                self.bot.logger.log(MODULE_NAME, 
                    "Transcribe manager not available, cannot process", "ERROR")
                self.background_transcription_active = False
                return
            
            transcribe_mgr = self.bot.transcribe_manager
            
            # Process newest first
            untranscribed.sort(key=lambda x: self._get_file_creation_time(x), reverse=True)
            
            for vm_path in untranscribed:
                try:
                    # Convert to WAV for transcription
                    temp_wav = Path("data/transcribe_temp") / f"bg_{vm_path.stem}.wav"
                    temp_wav.parent.mkdir(parents=True, exist_ok=True)
                    
                    conversion_success = await transcribe_mgr.convert_to_wav(vm_path, temp_wav)
                    if not conversion_success:
                        self.bot.logger.log(MODULE_NAME, 
                            f"Failed to convert {vm_path.name}", "WARNING")
                        failed += 1
                        continue
                    
                    # Transcribe
                    result = await transcribe_mgr.transcribe_audio(temp_wav)
                    
                    # Clean up temp file
                    if temp_wav.exists():
                        temp_wav.unlink()
                    
                    if result:
                        self.save_transcript(vm_path, result)
                        processed += 1
                        
                        if processed % 10 == 0:
                            self.bot.logger.log(MODULE_NAME, 
                                f"Background transcription progress: {processed}/{total}")
                    else:
                        failed += 1
                    
                    # Small delay to not overwhelm system
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, 
                        f"Error transcribing {vm_path.name}", e)
                    failed += 1
            
            self.bot.logger.log(MODULE_NAME, 
                f"Background transcription complete: {processed} succeeded, {failed} failed")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in background transcription", e)
        finally:
            self.background_transcription_active = False
    
    async def get_stats(self):
        """Get statistics about cached and archived VMs"""
        cache_files = self.get_vm_files(CACHE_DIR)
        archive_files = self.get_vm_files(ARCHIVE_DIR)
        
        now = datetime.now(timezone.utc)
        
        cache_ages = [(now - ct).days for _, ct in cache_files]
        archive_ages = [(now - ct).days for _, ct in archive_files]
        
        # Count transcribed VMs
        transcribed_count = 0
        # Count long VMs
        long_vm_count = 0
        # Count VMs in cooldown
        vms_in_cooldown = 0
        
        for file_path, _ in cache_files + archive_files:
            if self.get_transcript(file_path):
                transcribed_count += 1
            
            if self.is_vm_too_long(file_path):
                long_vm_count += 1
            
            if self.is_vm_in_cooldown(file_path):
                vms_in_cooldown += 1
        
        total_vms = len(cache_files) + len(archive_files)
        available_vms = total_vms - vms_in_cooldown
        
        stats = {
            'cache_count': len(cache_files),
            'archive_count': len(archive_files),
            'cache_avg_age': sum(cache_ages) / len(cache_ages) if cache_ages else 0,
            'archive_avg_age': sum(archive_ages) / len(archive_ages) if archive_ages else 0,
            'total_size_mb': sum(f.stat().st_size for f, _ in cache_files + archive_files) / (1024 * 1024),
            'message_count': self.message_count,
            'target_message_count': self.target_message_count,
            'last_post': self.last_post_time,
            'last_message': self.last_message_time,
            'transcribed_count': transcribed_count,
            'total_vms': total_vms,
            # NEW: Additional stats
            'long_vm_count': long_vm_count,
            'long_vm_percentage': (long_vm_count / total_vms * 100) if total_vms > 0 else 0,
            'vms_in_cooldown': vms_in_cooldown,
            'available_vms': available_vms,
            'transcription_percentage': (transcribed_count / total_vms * 100) if total_vms > 0 else 0
        }
        
        return stats
    
    @tasks.loop(hours=6)
    async def cleanup_loop(self):
        """Periodic task to clean up old files"""
        self.bot.logger.log(MODULE_NAME, "Running cleanup task")
        await self.cleanup_and_archive()
    
    @cleanup_loop.before_loop
    async def before_cleanup_loop(self):
        """Wait until bot is ready before starting cleanup loop"""
        await self.bot.wait_until_ready()
        self.bot.logger.log(MODULE_NAME, "VM manager initialized")
        await self.cleanup_and_archive()


def setup(bot):
    """Setup function called by main bot to initialize this module"""

    # Prevent duplicate listener registration
    listener_name = f"_{MODULE_NAME.lower()}_listener_registered"
    if hasattr(bot, listener_name):
        bot.logger.log(MODULE_NAME, "Module already setup, skipping duplicate registration")
        return
    setattr(bot, listener_name, True)

    bot.logger.log(MODULE_NAME, "Setting up VMS module")
    
    vms_manager = VMSManager(bot)
    bot.vms_manager = vms_manager
    
    vms_manager.cleanup_loop.start()
    
    @bot.listen('on_message')
    async def on_voice_message(message):
        """Listen for messages - handle general tracking and bot mentions/replies"""
        if message.author.bot:
            return
        
        # Skip DMs
        if not message.guild:
            return

        channel_name = getattr(message.channel, 'name', None)
        if channel_name == GENERAL_CHANNEL_NAME:
            await vms_manager.on_general_message(message)
        
        bot_mentioned = bot.user in message.mentions
        bot_replied_to = (
            message.reference and 
            message.reference.resolved and 
            message.reference.resolved.author.id == bot.user.id
        )
        
        if bot_mentioned or bot_replied_to:
            bot.logger.log(MODULE_NAME, 
                f"Bot {'mentioned' if bot_mentioned else 'replied to'} by {message.author} in #{channel_name}")
            
            try:
                await vms_manager.post_random_vm(reply_to=message, is_ping_response=True)
            except Exception as e:
                bot.logger.error(MODULE_NAME, "Error sending VM response", e)
    
    @bot.tree.command(name="vmstats", description="View voice message statistics")
    async def vmstats(interaction: discord.Interaction):
        """Show VM statistics"""
        try:
            stats = await vms_manager.get_stats()
            
            embed = discord.Embed(
                title="🎙️ Voice Message Statistics",
                color=0x9b59b6,
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="📂 Cache",
                value=f"**{stats['cache_count']}** files\nAvg age: {stats['cache_avg_age']:.1f} days",
                inline=True
            )
            
            embed.add_field(
                name="📦 Archive",
                value=f"**{stats['archive_count']}** files\nAvg age: {stats['archive_avg_age']:.1f} days",
                inline=True
            )
            
            embed.add_field(
                name="💾 Total Size",
                value=f"{stats['total_size_mb']:.2f} MB",
                inline=True
            )
            
            # NEW: VM availability stats
            embed.add_field(
                name="📊 Availability",
                value=f"**{stats['available_vms']}/{stats['total_vms']}** available\n"
                      f"{stats['vms_in_cooldown']} in cooldown",
                inline=True
            )
            
            # Transcription status
            embed.add_field(
                name="📝 Transcriptions",
                value=f"{stats['transcribed_count']}/{stats['total_vms']} ({stats['transcription_percentage']:.1f}%)",
                inline=True
            )
            
            # NEW: Long VM stats
            embed.add_field(
                name="⏰ Long VMs",
                value=f"{stats['long_vm_count']} ({stats['long_vm_percentage']:.1f}%)\n"
                      f">1min reduction: {LONG_VM_REDUCTION_FACTOR*100:.0f}%",
                inline=True
            )
            
            # Background processing status
            if vms_manager.background_transcription_active:
                embed.add_field(
                    name="⚙️ Status",
                    value="Background transcribing...",
                    inline=True
                )
            
            progress_info = [
                f"Messages: {stats['message_count']}/{stats['target_message_count']}",
                f"Progress: {(stats['message_count']/stats['target_message_count']*100):.0f}%"
            ]
            
            if stats['last_post']:
                time_since = datetime.now(timezone.utc) - stats['last_post']
                minutes = time_since.total_seconds() / 60
                progress_info.append(f"Last post: {minutes:.0f}m ago")
            
            if stats['last_message']:
                time_since = datetime.now(timezone.utc) - stats['last_message']
                minutes = time_since.total_seconds() / 60
                progress_info.append(f"Last message: {minutes:.0f}m ago")
            
            embed.add_field(
                name="📈 Progress",
                value="\n".join(progress_info),
                inline=False
            )
            
            # Cooldown status
            cooldown_info = []
            if vms_manager.last_post_time:
                time_since_post = (datetime.now(timezone.utc) - vms_manager.last_post_time).total_seconds()
                if time_since_post < RANDOM_VM_COOLDOWN_SECONDS:
                    cooldown_info.append(f"Random VM: {RANDOM_VM_COOLDOWN_SECONDS - time_since_post:.0f}s")
                else:
                    cooldown_info.append("Random VM: Ready")
            
            if vms_manager.last_ping_response_time:
                time_since_ping = (datetime.now(timezone.utc) - vms_manager.last_ping_response_time).total_seconds()
                if time_since_ping < PING_COOLDOWN_SECONDS:
                    cooldown_info.append(f"Ping: {PING_COOLDOWN_SECONDS - time_since_ping:.0f}s")
                else:
                    cooldown_info.append("Ping: Ready")
            
            if cooldown_info:
                embed.add_field(
                    name="⏰ Cooldowns",
                    value="\n".join(cooldown_info),
                    inline=True
                )
            
            # NEW: Selection configuration
            selection_config = [
                f"VM cooldown: {VM_COOLDOWN_DAYS} days",
                f"Relevant VM chance: {RELEVANT_VM_CHANCE*100:.0f}%",
                f"Keyword score weight: {MAX_SCORE_WEIGHT*100:.0f}%",
                f"Keyword normalization: {'ON' if KEYWORD_SCORE_NORMALIZATION else 'OFF'}"
            ]
            
            embed.add_field(
                name="⚙️ Selection",
                value="\n".join(selection_config),
                inline=True
            )
            
            # Existing configuration
            embed.add_field(
                name="📋 Configuration",
                value=f"Message interval: {MIN_MESSAGES_BETWEEN}-{MAX_MESSAGES_BETWEEN}\n"
                      f"Inactivity timeout: {INACTIVITY_TIMEOUT_HOURS}h\n"
                      f"Cache threshold: {CACHE_DAYS} days\n"
                      f"Archive threshold: {ARCHIVE_DAYS} days\n"
                      f"Archive play chance: {ARCHIVE_CHANCE*100:.0f}%\n"
                      f"Ping cooldown: {PING_COOLDOWN_SECONDS}s\n"
                      f"Random VM cooldown: {RANDOM_VM_COOLDOWN_SECONDS}s",
                inline=False
            )
            
            embed.set_footer(text=f"Requested by {interaction.user}")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "vmstats command failed", e)
            await interaction.response.send_message(
                "❌ Error retrieving VM statistics",
                ephemeral=True
            )
    
    @bot.tree.command(name="vmtest", description="[Admin] Post a random VM immediately")
    async def vmtest(interaction: discord.Interaction):
        """Manually trigger a VM post (admin only)"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True
            )
            return
        
        bot.logger.log(MODULE_NAME, f"Manual VM test requested by {interaction.user}")
        
        await interaction.response.defer(ephemeral=True)
        
        success = await vms_manager.post_random_vm()
        
        if success:
            await interaction.followup.send(
                "✅ Successfully posted a VM!",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ Failed to post VM (check logs for details)",
                ephemeral=True
            )
    
    @bot.tree.command(name="vmcleanup", description="[Admin] Run cleanup/archive process now")
    async def vmcleanup(interaction: discord.Interaction):
        """Manually trigger cleanup (admin only)"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True
            )
            return
        
        bot.logger.log(MODULE_NAME, f"Manual cleanup requested by {interaction.user}")
        
        await interaction.response.defer(ephemeral=True)
        
        await vms_manager.cleanup_and_archive()
        
        stats = await vms_manager.get_stats()
        
        await interaction.followup.send(
            f"✅ Cleanup complete!\n"
            f"📂 Cache: {stats['cache_count']} files\n"
            f"📦 Archive: {stats['archive_count']} files",
            ephemeral=True
        )
    
    @bot.tree.command(name="vmtranscribe", description="[Admin] Start background transcription of all VMs")
    async def vmtranscribe(interaction: discord.Interaction):
        """Manually trigger background transcription (admin only)"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True
            )
            return
        
        if vms_manager.background_transcription_active:
            await interaction.response.send_message(
                "⚙️ Background transcription is already running!",
                ephemeral=True
            )
            return
        
        bot.logger.log(MODULE_NAME, f"Manual transcription requested by {interaction.user}")
        
        await interaction.response.send_message(
            "✅ Starting background transcription of all VMs...",
            ephemeral=True
        )
        
        asyncio.create_task(vms_manager.background_transcribe_all())
    
    @bot.tree.command(name="vmbulktranscribe", description="[Admin] Instructions for bulk transcription")
    async def vmbulktranscribe(interaction: discord.Interaction):
        """Show instructions for using the bulk transcription script"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True
            )
            return
        
        instructions = """
        **🔊 BULK TRANSCRIPTION INSTRUCTIONS**
        
        For large amounts of untranscribed VMs, use the separate bulk transcription script:
        
        1. **Stop the bot** (or run on a separate machine)
        2. **Run**: `python bulk_transcribe.py`
        3. **The script will**:
           - Process VMs from newest to oldest
           - Use parallel processing for maximum speed
           - Save transcripts incrementally
           - Monitor for new VMs while running
           - Show real-time progress
        
        **Features:**
        - ✅ Independent process (won't interrupt bot)
        - ✅ Newest VMs first
        - ✅ Parallel processing (4 at a time)
        - ✅ Real-time progress with ETA
        - ✅ Automatically detects new VMs
        - ✅ Uses GPU acceleration if available
        
        **Note:** After bulk transcription, the bot will automatically use all transcripts for contextual VM selection.
        """
        
        embed = discord.Embed(
            title="Bulk Transcription Instructions",
            description=instructions,
            color=0x3498db
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # NEW: Admin command to reset VM cooldowns
    @bot.tree.command(name="vmresetcooldowns", description="[Admin] Reset cooldowns for all VMs")
    async def vmresetcooldowns(interaction: discord.Interaction):
        """Reset cooldowns for all VMs (admin only)"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True
            )
            return
        
        bot.logger.log(MODULE_NAME, f"VM cooldown reset requested by {interaction.user}")
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            old_count = len(vms_manager.vm_tracking)
            vms_manager.vm_tracking = {}
            vms_manager._save_vm_tracking()
            
            await interaction.followup.send(
                f"✅ Reset cooldowns for {old_count} VMs!",
                ephemeral=True
            )
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Error resetting VM cooldowns", e)
            await interaction.followup.send(
                "❌ Error resetting VM cooldowns",
                ephemeral=True
            )
    
    bot.logger.log(MODULE_NAME, "VMS module setup complete")