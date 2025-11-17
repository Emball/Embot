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

MODULE_NAME = "VMS"

# Configuration
VMS_ROOT = Path("data/voice_messages")
CACHE_DIR = VMS_ROOT / "cache"
ARCHIVE_DIR = VMS_ROOT / "archived"
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
PING_COOLDOWN_SECONDS = 300  # 5 minutes between ping responses
RANDOM_VM_COOLDOWN_SECONDS = 60  # 1 minute between random VMs


class VMSManager:
    """Manages voice message caching, archiving, and random playback"""
    
    def __init__(self, bot):
        self.bot = bot
        self.last_post_time = None
        self.last_ping_response_time = None  # Track ping cooldown
        self.message_count = 0
        self.target_message_count = 0
        self.last_message_time = None
        self.posting_lock = asyncio.Lock()  # Prevent duplicate posts
        self._setup_directories()
        self._schedule_next_post()
    
    def _setup_directories(self):
        """Create directory structure if it doesn't exist"""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.bot.logger.log(MODULE_NAME, f"Directory structure ready: {VMS_ROOT}")
    
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
                    file_path.unlink()
                    
                    moved_count += 1
                    self.bot.logger.log(MODULE_NAME, 
                        f"Archived {file_path.name} (age: {age_days} days)")
            
            archive_files = self.get_vm_files(ARCHIVE_DIR)
            for file_path, creation_time in archive_files:
                age_days = (now - creation_time).days
                
                if age_days >= ARCHIVE_DAYS:
                    file_path.unlink()
                    
                    deleted_count += 1
                    self.bot.logger.log(MODULE_NAME, 
                        f"Deleted {file_path.name} from archive (age: {age_days} days)")
            
            if moved_count > 0 or deleted_count > 0:
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
        """Save a voice message from Discord to cache"""
        if not self.is_voice_message(message):
            return False
        
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
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error saving voice message", e)
            return False
    
    def select_random_vm(self):
        """Select a random voice message, with small chance from archive"""
        try:
            use_archive = random.random() < ARCHIVE_CHANCE
            
            if use_archive:
                archive_files = self.get_vm_files(ARCHIVE_DIR)
                if archive_files:
                    selected, creation_time = random.choice(archive_files)
                    age_days = (datetime.now(timezone.utc) - creation_time).days
                    self.bot.logger.log(MODULE_NAME, 
                        f"Selected from archive: {selected.name} (age: {age_days} days)")
                    return selected
                else:
                    self.bot.logger.log(MODULE_NAME, "No archive files available, using cache")
            
            cache_files = self.get_vm_files(CACHE_DIR)
            if cache_files:
                selected, creation_time = random.choice(cache_files)
                age_days = (datetime.now(timezone.utc) - creation_time).days
                self.bot.logger.log(MODULE_NAME, 
                    f"Selected from cache: {selected.name} (age: {age_days} days)")
                return selected
            
            self.bot.logger.log(MODULE_NAME, "No voice messages available", "WARNING")
            return None
            
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
                # Check cooldown before posting
                if not self.can_post_random_vm():
                    self.bot.logger.log(MODULE_NAME, 
                        f"Cooldown active, skipping scheduled VM (last post was {(datetime.now(timezone.utc) - self.last_post_time).total_seconds():.0f}s ago)")
                    # Reset counter but keep waiting
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
        """Post a random voice message - VM only, with duplicate prevention"""
        # Use lock to prevent duplicate posts
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
                            f"Ping cooldown active ({cooldown_remaining:.0f}s remaining), ignoring ping")
                        return False
                else:
                    if not self.can_post_random_vm():
                        cooldown_remaining = RANDOM_VM_COOLDOWN_SECONDS - (datetime.now(timezone.utc) - self.last_post_time).total_seconds()
                        self.bot.logger.log(MODULE_NAME, 
                            f"Random VM cooldown active ({cooldown_remaining:.0f}s remaining), skipping")
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
                
                # Select a random VM
                vm_file = self.select_random_vm()
                if not vm_file:
                    self.bot.logger.log(MODULE_NAME, "No VM to post", "WARNING")
                    return False
                
                # Send VM only (no transcription)
                if reply_to:
                    await self._send_as_voice_message_reply(reply_to, vm_file)
                    self.bot.logger.log(MODULE_NAME, "Posted VM reply")
                else:
                    await self._send_as_voice_message(target_channel, vm_file)
                    self.bot.logger.log(MODULE_NAME, "Posted random VM")
                
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
    
    async def get_stats(self):
        """Get statistics about cached and archived VMs"""
        cache_files = self.get_vm_files(CACHE_DIR)
        archive_files = self.get_vm_files(ARCHIVE_DIR)
        
        now = datetime.now(timezone.utc)
        
        cache_ages = [(now - ct).days for _, ct in cache_files]
        archive_ages = [(now - ct).days for _, ct in archive_files]
        
        stats = {
            'cache_count': len(cache_files),
            'archive_count': len(archive_files),
            'cache_avg_age': sum(cache_ages) / len(cache_ages) if cache_ages else 0,
            'archive_avg_age': sum(archive_ages) / len(archive_ages) if archive_ages else 0,
            'total_size_mb': sum(f.stat().st_size for f, _ in cache_files + archive_files) / (1024 * 1024),
            'message_count': self.message_count,
            'target_message_count': self.target_message_count,
            'last_post': self.last_post_time,
            'last_message': self.last_message_time
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
    bot.logger.log(MODULE_NAME, "Setting up VMS module")
    
    vms_manager = VMSManager(bot)
    bot.vms_manager = vms_manager
    
    vms_manager.cleanup_loop.start()
    
    @bot.listen('on_message')
    async def on_voice_message(message):
        """Listen for voice messages and save them, track general messages, respond to pings/replies with cooldown"""
        if message.author.bot:
            return
    
        if message.channel.name == GENERAL_CHANNEL_NAME:
            await vms_manager.on_general_message(message)
        
        bot_mentioned = bot.user in message.mentions
        bot_replied_to = (
            message.reference and 
            message.reference.resolved and 
            message.reference.resolved.author.id == bot.user.id
        )
        
        if bot_mentioned or bot_replied_to:
            bot.logger.log(MODULE_NAME, 
                f"Bot {'mentioned' if bot_mentioned else 'replied to'} by {message.author} in #{message.channel.name}")
            
            try:
                # Pass is_ping_response=True to use ping cooldown
                await vms_manager.post_random_vm(reply_to=message, is_ping_response=True)
            except Exception as e:
                bot.logger.error(MODULE_NAME, "Error sending VM response", e)
        
        if message.attachments:
            await vms_manager.save_voice_message(message)
    
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
                name="📊 Progress",
                value="\n".join(progress_info),
                inline=False
            )
            
            # Add cooldown status
            cooldown_info = []
            if vms_manager.last_post_time:
                time_since_post = (datetime.now(timezone.utc) - vms_manager.last_post_time).total_seconds()
                if time_since_post < RANDOM_VM_COOLDOWN_SECONDS:
                    cooldown_info.append(f"Random VM cooldown: {RANDOM_VM_COOLDOWN_SECONDS - time_since_post:.0f}s")
                else:
                    cooldown_info.append("Random VM cooldown: Ready")
            
            if vms_manager.last_ping_response_time:
                time_since_ping = (datetime.now(timezone.utc) - vms_manager.last_ping_response_time).total_seconds()
                if time_since_ping < PING_COOLDOWN_SECONDS:
                    cooldown_info.append(f"Ping cooldown: {PING_COOLDOWN_SECONDS - time_since_ping:.0f}s")
                else:
                    cooldown_info.append("Ping cooldown: Ready")
            
            if cooldown_info:
                embed.add_field(
                    name="⏰ Cooldowns",
                    value="\n".join(cooldown_info),
                    inline=True
                )
            
            embed.add_field(
                name="📋 Configuration",
                value=f"Message interval: {MIN_MESSAGES_BETWEEN}-{MAX_MESSAGES_BETWEEN}\n"
                      f"Inactivity timeout: {INACTIVITY_TIMEOUT_HOURS}h\n"
                      f"Cache threshold: {CACHE_DAYS} days\n"
                      f"Archive threshold: {ARCHIVE_DAYS} days\n"
                      f"Archive play chance: {ARCHIVE_CHANCE*100:.0f}%\n"
                      f"Ping cooldown: {PING_COOLDOWN_SECONDS}s\n"
                      f"Random VM cooldown: {RANDOM_VM_COOLDOWN_SECONDS}s\n"
                      f"Responds to: mentions & replies\n"
                      f"Format: Voice messages only",
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
                "✅ Successfully posted a random VM!",
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
    
    bot.logger.log(MODULE_NAME, "VMS module setup complete")