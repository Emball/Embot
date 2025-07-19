# voice_messages.py
import discord
import os
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta
from discord.ext import tasks
from collections import deque
import shutil

logger = logging.getLogger('voice_messages')

class VoiceMessageManager:
    def __init__(self):
        self.current_month_dir = Path(__file__).parent / "voice_messages"
        self.archive_dir = Path(__file__).parent / "vm_archive"
        self.current_month_dir.mkdir(exist_ok=True)
        self.archive_dir.mkdir(exist_ok=True)
        self.recent_drops = deque(maxlen=20)
        self.file_usage = {}
        self._initialize_file_tracking()
        logger.info("Voice message manager initialized")

    def _initialize_file_tracking(self):
        """Initialize file usage tracking"""
        for file in self.get_current_month_files():
            self.file_usage[str(file)] = 0
        logger.debug(f"Tracking {len(self.file_usage)} voice message files")

    def get_current_month_files(self):
        """Get current month's voice files"""
        return list(self.current_month_dir.glob("*.ogg"))

    def get_archive_files(self):
        """Get archived voice files"""
        archive_files = []
        for year_dir in self.archive_dir.glob("*"):
            if year_dir.is_dir():
                for month_dir in year_dir.glob("*"):
                    if month_dir.is_dir():
                        archive_files.extend(month_dir.glob("*.ogg"))
        return archive_files

    def _archive_old_messages(self):
        """Archive files from previous month"""
        now = datetime.now()
        prev_month = now.month - 1 if now.month > 1 else 12
        prev_year = now.year if now.month > 1 else now.year - 1
        archive_path = self.archive_dir / str(prev_year) / f"{prev_month:02d}"
        
        if archive_path.exists():
            return

        archive_path.mkdir(parents=True)
        for file in self.get_current_month_files():
            try:
                shutil.move(str(file), str(archive_path / file.name))
                self.file_usage.pop(str(file), None)
                logger.debug(f"Archived voice message: {file.name}")
            except Exception as e:
                logger.error(f"Error archiving {file}: {e}")

    def _prune_old_archives(self):
        """Remove archives older than 1 year"""
        cutoff = datetime.now() - timedelta(days=365)
        for year_dir in self.archive_dir.glob("*"):
            try:
                year = int(year_dir.name)
                for month_dir in year_dir.glob("*"):
                    month = int(month_dir.name)
                    if datetime(year, month, 1) < cutoff:
                        try:
                            shutil.rmtree(month_dir)
                            logger.info(f"Pruned old archive: {month_dir}")
                            if not any(year_dir.iterdir()):
                                shutil.rmtree(year_dir)
                        except Exception as e:
                            logger.error(f"Error pruning {month_dir}: {e}")
            except ValueError:
                continue

    def _get_weighted_file_list(self):
        """Get files with weights based on usage frequency"""
        current_files = self.get_current_month_files()
        archive_files = self.get_archive_files()
        
        all_files = []
        weights = []
        
        # Current month files (higher weight for less used files)
        for file in current_files:
            usage = self.file_usage.get(str(file), 0)
            weight = 0.9 / (1 + usage)  # Inverse weighting
            all_files.append(file)
            weights.append(weight)
        
        # Archive files (lower base weight)
        for file in archive_files:
            usage = self.file_usage.get(str(file), 0)
            weight = 0.1 / (10 + usage)  # Much lower base weight
            all_files.append(file)
            weights.append(weight)
        
        # Normalize weights
        total_weight = sum(weights)
        if total_weight > 0:
            normalized_weights = [w/total_weight for w in weights]
        else:
            normalized_weights = None  # Fall back to uniform
        
        return all_files, normalized_weights

    def get_random_voice_message(self):
        """Get a random voice message with improved weighting"""
        self._archive_old_messages()
        self._prune_old_archives()
        
        files, weights = self._get_weighted_file_list()
        
        if not files:
            logger.warning("No voice message files available")
            return None

        # Avoid recent repeats
        available_files = [f for f in files if str(f) not in self.recent_drops]
        
        if available_files:
            try:
                selected_file = random.choices(
                    available_files,
                    weights=[weights[files.index(f)] for f in available_files],
                    k=1
                )[0]
            except Exception as e:
                logger.error(f"Weighted selection failed, using random: {e}")
                selected_file = random.choice(available_files)
        else:
            # If all files were recently used, pick least used
            available_files = files
            selected_file = min(available_files, key=lambda f: self.file_usage.get(str(f), 0))
        
        # Update tracking
        self.recent_drops.append(str(selected_file))
        self.file_usage[str(selected_file)] = self.file_usage.get(str(selected_file), 0) + 1
        logger.debug(f"Selected voice message: {selected_file.name}")
        return selected_file

    async def save_voice_message(self, attachment):
        """Save a new voice message attachment"""
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        filename = self.current_month_dir / f"voice-message-{timestamp}.ogg"
        try:
            await attachment.save(filename)
            self.file_usage[str(filename)] = 0
            logger.info(f"Saved new voice message: {filename.name}")
            return True
        except Exception as e:
            logger.error(f"Error saving voice message: {e}")
            return False

voice_manager = VoiceMessageManager()

async def handle_general_voice_message(bot, message):
    """Handle voice message interactions"""
    try:
        if message.channel == bot.general_channel and message.attachments:
            for att in message.attachments:
                if att.filename.endswith(".ogg"):
                    await voice_manager.save_voice_message(att)

        if bot.user in message.mentions:
            if file := voice_manager.get_random_voice_message():
                try:
                    await message.channel.send(file=discord.File(file))
                    logger.info(f"Sent voice message to {message.channel}")
                except Exception as e:
                    logger.error(f"Error sending voice message: {e}")
    except Exception as e:
        logger.error(f"Voice message handling error: {e}")

@tasks.loop(minutes=30)
async def random_voice_drop(bot):
    """Task for random voice message drops"""
    try:
        if random.random() < 0.3 and bot.general_channel:
            if file := voice_manager.get_random_voice_message():
                await bot.general_channel.send(file=discord.File(file))
                logger.info("Random voice drop executed")
    except Exception as e:
        logger.error(f"Random voice drop failed: {e}")

def setup(bot):  # Changed from nothing to this
    """Setup voice messages extension"""
    logger.info("Voice messages extension loaded (no setup required)")