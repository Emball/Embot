# levels.py
import discord
import json
import math
import logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from config import (
    REACTION_WEIGHTS,
    XP_REWARDS,
    LOG_FORMAT,
    LOG_DATE_FORMAT
)

logger = logging.getLogger('levels')

LEVELS_FILE = "levels.json"
BASE_XP = 100
XP_MULTIPLIER = 1.5
MAX_PROGRESS_BAR_LENGTH = 10

class LevelSystem:
    def __init__(self, bot):
        self.bot = bot
        self.data = self._load_data()
        self.reaction_tracker = set()
        logger.info("Level system initialized")

    def _load_data(self):
        """Load level data with proper defaultdict conversion"""
        try:
            if Path(LEVELS_FILE).exists():
                with open(LEVELS_FILE, 'r') as f:
                    data = json.load(f)
                
                # Convert reactions_received to defaultdict
                for user_data in data.values():
                    if "reactions_received" in user_data:
                        if isinstance(user_data["reactions_received"], dict):
                            user_data["reactions_received"] = defaultdict(
                                int, 
                                {k: v for k, v in user_data["reactions_received"].items() 
                                if k in REACTION_WEIGHTS}
                            )
                        else:
                            user_data["reactions_received"] = defaultdict(int)
                logger.info("Loaded level data from file")
                return data
        except Exception as e:
            logger.error(f"Error loading levels data: {e}")
        return {}

    def _save_data(self):
        """Save level data with error handling"""
        try:
            with open(LEVELS_FILE, 'w') as f:
                json.dump(self.data, f, indent=2)
            logger.debug("Level data saved successfully")
        except Exception as e:
            logger.error(f"Error saving levels data: {e}")

    def _calculate_level(self, xp):
        """Calculate level from XP"""
        if xp < BASE_XP:
            return 0
        return int(math.log(xp / BASE_XP, XP_MULTIPLIER)) + 1

    def _calculate_xp_needed(self, level):
        """Calculate XP needed for a level"""
        if level == 0:
            return BASE_XP
        return int(BASE_XP * (XP_MULTIPLIER ** (level - 1)))

    def _get_progress(self, xp, level):
        """Calculate progress to next level"""
        if level == 0:
            return min(1.0, xp / BASE_XP)
        
        current_level_xp = self._calculate_xp_needed(level)
        next_level_xp = self._calculate_xp_needed(level + 1)
        
        if xp < current_level_xp:
            return 0.0
        
        progress = (xp - current_level_xp) / (next_level_xp - current_level_xp)
        return min(progress, 1.0)

    async def add_xp(self, user_id, xp, message=None):
        """Add XP to a user with logging"""
        user_id = str(user_id)
        logger.debug(f"Adding {xp} XP to user {user_id}")
        
        if user_id not in self.data:
            self.data[user_id] = {
                "xp": 0,
                "last_message": None,
                "voice_time": 0,
                "reactions_received": defaultdict(int)
            }
        
        self.data[user_id]["xp"] = max(0, self.data[user_id]["xp"] + xp)
        
        old_level = self._calculate_level(self.data[user_id]["xp"] - xp)
        new_level = self._calculate_level(self.data[user_id]["xp"])
        
        if new_level > old_level and message:
            logger.info(f"User {user_id} leveled up from {old_level} to {new_level}")
            await self._send_level_up_message(user_id, old_level, new_level, message)
        
        self._save_data()

    async def _send_level_up_message(self, user_id, old_level, new_level, message):
        """Send level up announcement"""
        try:
            user = await self.bot.fetch_user(int(user_id))
            xp = self.data[user_id]["xp"]
            reactions = self.data[user_id]["reactions_received"]
            
            if old_level > 0:
                level_start_xp = self._calculate_xp_needed(old_level)
                level_end_xp = self._calculate_xp_needed(old_level + 1)
                progress = min(1.0, (xp - level_start_xp) / (level_end_xp - level_start_xp))
            else:
                progress = min(1.0, xp / BASE_XP)
            
            # Build reaction tally string
            reaction_tally = []
            for emoji in REACTION_WEIGHTS:
                if emoji in reactions:
                    reaction_tally.append(f"{emoji} {reactions[emoji]}")
            
            embed = discord.Embed(
                title="🎉 Level Up!",
                description=f"**{user.display_name}** has reached level **{new_level}**!",
                color=discord.Color.gold()
            )
            
            progress_bar = self._create_progress_bar(progress)
            embed.add_field(
                name=f"Level {old_level} Completion", 
                value=f"{progress_bar}\n{progress*100:.1f}% completed",
                inline=False
            )
            
            if reaction_tally:
                embed.add_field(
                    name="Reactions Received",
                    value=" | ".join(reaction_tally),
                    inline=False
                )
            
            embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
            embed.add_field(name="Next Level Goal", value=f"{self._calculate_xp_needed(new_level + 1):,} XP", inline=True)
            
            if user.avatar:
                embed.set_thumbnail(url=user.avatar.url)
            
            general_channel = discord.utils.get(
                message.guild.text_channels, 
                name="general"
            ) or message.guild.system_channel
            
            if general_channel:
                await general_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Error sending level up message: {e}")

    def _create_progress_bar(self, progress):
        """Create visual progress bar"""
        filled = round(progress * MAX_PROGRESS_BAR_LENGTH)
        empty = MAX_PROGRESS_BAR_LENGTH - filled
        return "🟩" * filled + "⬛" * empty

    def get_user_stats(self, user_id):
        """Get formatted user stats"""
        user_id = str(user_id)
        if user_id not in self.data:
            return None
        
        xp = self.data[user_id]["xp"]
        level = self._calculate_level(xp)
        
        return {
            "level": level,
            "xp": xp,
            "next_level_xp": self._calculate_xp_needed(level + 1),
            "current_level_xp": self._calculate_xp_needed(level),
            "progress": self._get_progress(xp, level),
            "reactions_received": dict(self.data[user_id]["reactions_received"])
        }

    async def handle_message_xp(self, message):
        """Handle message XP with cooldown"""
        if message.author.bot:
            return
            
        user_id = str(message.author.id)
        
        if user_id not in self.data:
            self.data[user_id] = {
                "xp": 0,
                "last_message": None,
                "voice_time": 0,
                "reactions_received": defaultdict(int)
            }
        
        last_msg = self.data[user_id].get("last_message")
        now = datetime.utcnow().timestamp()
        
        if last_msg is None or (now - last_msg) > 60:
            await self.add_xp(message.author.id, XP_REWARDS["message"], message)
            self.data[user_id]["last_message"] = now
            self._save_data()

    async def handle_voice_xp(self, member, before, after):
        """Handle voice XP tracking"""
        if member.bot:
            return
            
        user_id = str(member.id)
        
        if user_id not in self.data:
            self.data[user_id] = {
                "xp": 0,
                "last_message": None,
                "voice_time": 0,
                "reactions_received": defaultdict(int)
            }
        
        if before.channel is None and after.channel is not None:
            self.data[user_id]["voice_join_time"] = datetime.utcnow().timestamp()
            self._save_data()
        
        elif before.channel is not None and after.channel is None:
            join_time = self.data[user_id].get("voice_join_time")
            if join_time:
                minutes = (datetime.utcnow().timestamp() - join_time) / 60
                xp = int(minutes * XP_REWARDS["voice_minute"])
                if xp > 0:
                    await self.add_xp(member.id, xp)
                self.data[user_id]["voice_time"] += minutes
                if "voice_join_time" in self.data[user_id]:
                    del self.data[user_id]["voice_join_time"]
                self._save_data()

def setup(bot):  # Changed from setup_level_commands
    """Setup level-related slash commands"""
    logger.info("Setting up level commands...")
    
    @bot.tree.command(name="level", description="Check your current level and XP")
    async def level(interaction: discord.Interaction, user: discord.User = None):
        target_user = user or interaction.user
        stats = level_system.get_user_stats(target_user.id)
        
        if not stats:
            await interaction.response.send_message(
                f"{target_user.display_name} hasn't earned any XP yet!", 
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title=f"{target_user.display_name}'s Level Stats",
            color=discord.Color.blurple()
        )
        
        progress_bar = level_system._create_progress_bar(stats["progress"])
        
        embed.add_field(name="Level", value=str(stats["level"]), inline=True)
        embed.add_field(name="XP", value=f"{stats['xp']:,}/{stats['next_level_xp']:,}", inline=True)
        embed.add_field(
            name="Progress", 
            value=f"{progress_bar}\n{stats['progress']*100:.1f}% to level {stats['level']+1}", 
            inline=False
        )
        
        if stats["reactions_received"]:
            reaction_tally = []
            for emoji in REACTION_WEIGHTS:
                if emoji in stats["reactions_received"]:
                    reaction_tally.append(f"{emoji} {stats['reactions_received'][emoji]}")
            embed.add_field(name="Reactions Received", value=" | ".join(reaction_tally), inline=False)
        
        if target_user.avatar:
            embed.set_thumbnail(url=target_user.avatar.url)
        
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="leaderboard", description="Show the top 10 users by XP")
    async def leaderboard(interaction: discord.Interaction):
        sorted_users = sorted(
            level_system.data.items(),
            key=lambda x: x[1]["xp"],
            reverse=True
        )[:10]
        
        if not sorted_users:
            await interaction.response.send_message("No users have earned XP yet!", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🏆 Global XP Leaderboard",
            color=discord.Color.blue()
        )
        
        description = []
        for rank, (user_id, data) in enumerate(sorted_users, 1):
            try:
                user = await interaction.client.fetch_user(int(user_id))
                level = level_system._calculate_level(data["xp"])
                description.append(f"{rank}. {user.display_name} - Level {level} ({data['xp']:,} XP)")
            except:
                level = level_system._calculate_level(data["xp"])
                description.append(f"{rank}. Unknown User - Level {level} ({data['xp']:,} XP)")
        
        embed.description = "\n".join(description)
        await interaction.response.send_message(embed=embed)