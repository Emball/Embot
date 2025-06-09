# levels.py
import discord
import json
import math
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from config import *

# Configuration remains the same
LEVELS_FILE = "levels.json"
BASE_XP = 100
XP_MULTIPLIER = 1.5
REACTION_WEIGHTS = {
    "🔥": 5,
    "😐": 0,
    "🗑️": -5
}
MESSAGE_XP = 5
VOICE_XP_PER_MINUTE = 1
MAX_PROGRESS_BAR_LENGTH = 10  # Reduced from 15 to make it more mobile-friendly

class LevelSystem:
    def __init__(self, bot):
        self.bot = bot
        self.data = self._load_data()
        self.reaction_tracker = set()
        
    def _load_data(self):
        try:
            if Path(LEVELS_FILE).exists():
                with open(LEVELS_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading levels data: {e}")
        return {}
    
    def _save_data(self):
        try:
            with open(LEVELS_FILE, 'w') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"Error saving levels data: {e}")
    
    def _calculate_level(self, xp):
        if xp < BASE_XP:
            return 0
        return int(math.log(xp / BASE_XP, XP_MULTIPLIER)) + 1
    
    def _calculate_xp_needed(self, level):
        if level == 0:
            return BASE_XP
        return int(BASE_XP * (XP_MULTIPLIER ** (level - 1)))
    
    def _get_progress(self, xp, level):
        if level == 0:
            return min(1.0, xp / BASE_XP)
        
        current_level_xp = self._calculate_xp_needed(level)
        next_level_xp = self._calculate_xp_needed(level + 1)
        
        if xp < current_level_xp:
            return 0.0
        
        progress = (xp - current_level_xp) / (next_level_xp - current_level_xp)
        return min(progress, 1.0)
    
    async def add_xp(self, user_id, xp, message=None):
        user_id = str(user_id)
        
        if user_id not in self.data:
            self.data[user_id] = {
                "xp": 0,
                "last_message": None,  # Global cooldown
                "voice_time": 0,
                "reactions_received": defaultdict(int)
            }
        
        # Add XP globally
        self.data[user_id]["xp"] += xp
        if self.data[user_id]["xp"] < 0:
            self.data[user_id]["xp"] = 0
        
        # Check for level up
        old_level = self._calculate_level(self.data[user_id]["xp"] - xp)
        new_level = self._calculate_level(self.data[user_id]["xp"])
        
        if new_level > old_level and message:
            await self._send_level_up_message(user_id, old_level, new_level, message)
        
        self._save_data()
    
    async def _send_level_up_message(self, user_id, old_level, new_level, message):
        user = await self.bot.fetch_user(int(user_id))
        xp = self.data[user_id]["xp"]
        reactions = self.data[user_id]["reactions_received"]
        
        if old_level > 0:
            level_start_xp = self._calculate_xp_needed(old_level)
            level_end_xp = self._calculate_xp_needed(old_level + 1)
            progress = min(1.0, (xp - level_start_xp) / (level_end_xp - level_start_xp))
        else:
            progress = min(1.0, xp / BASE_XP)
        
        fire_count = reactions.get("🔥", 0)
        trash_count = reactions.get("🗑️", 0)
        neutral_count = reactions.get("😐", 0)
        
        reaction_tally = f"🔥 {fire_count} | 🗑️ {trash_count} | 😐 {neutral_count}"
        
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
        
        embed.add_field(
            name="Reactions Received",
            value=reaction_tally,
            inline=False
        )
        
        embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
        embed.add_field(name="Next Level Goal", value=f"{self._calculate_xp_needed(new_level + 1):,} XP", inline=True)
        
        try:
            if user.banner:
                embed.set_image(url=user.banner.url)
        except:
            pass
        
        try:
            if not embed.image and user.avatar:
                embed.set_thumbnail(url=user.avatar.url)
        except:
            pass
        
        # Always send to general channel regardless of where level up occurred
        general_channel = discord.utils.get(message.guild.text_channels, name="general")
        if general_channel is None:
            general_channel = message.guild.system_channel or message.guild.text_channels[0]
        
        try:
            await general_channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending level up message: {e}")
    
    def _create_progress_bar(self, progress):
        filled = round(progress * MAX_PROGRESS_BAR_LENGTH)
        empty = MAX_PROGRESS_BAR_LENGTH - filled
        return ":green_square:" * filled + ":black_large_square:" * empty
    
    def get_user_stats(self, user_id):
        user_id = str(user_id)
        if user_id not in self.data:
            return None
        
        xp = self.data[user_id]["xp"]
        level = self._calculate_level(xp)
        progress = self._get_progress(xp, level)
        
        return {
            "level": level,
            "xp": xp,
            "next_level_xp": self._calculate_xp_needed(level + 1),
            "current_level_xp": self._calculate_xp_needed(level),
            "progress": progress,
            "reactions_received": dict(self.data[user_id]["reactions_received"])
        }
    
    async def handle_message_xp(self, message):
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
        
        # Global cooldown (1 minute)
        last_msg = self.data[user_id].get("last_message")
        now = datetime.utcnow().timestamp()
        
        if last_msg is None or (now - last_msg) > 60:
            await self.add_xp(message.author.id, MESSAGE_XP, message)
            self.data[user_id]["last_message"] = now
            self._save_data()
    
    async def handle_voice_xp(self, member, before, after):
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
                xp = int(minutes * VOICE_XP_PER_MINUTE)
                if xp > 0:
                    await self.add_xp(member.id, xp)
                self.data[user_id]["voice_time"] += minutes
                if "voice_join_time" in self.data[user_id]:
                    del self.data[user_id]["voice_join_time"]
                self._save_data()

def setup_level_commands(bot, level_system):
    print("Setting up level commands...")
    
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
            fire_count = stats["reactions_received"].get("🔥", 0)
            trash_count = stats["reactions_received"].get("🗑️", 0)
            neutral_count = stats["reactions_received"].get("😐", 0)
            reaction_tally = f"🔥 {fire_count} | 🗑️ {trash_count} | 😐 {neutral_count}"
            embed.add_field(name="Reactions Received", value=reaction_tally, inline=False)
        
        try:
            if target_user.avatar:
                embed.set_thumbnail(url=target_user.avatar.url)
        except:
            pass
        
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="leaderboard", description="Show the top 10 users by XP")
    async def leaderboard(interaction: discord.Interaction):
        # Global leaderboard (all servers)
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
                if user:
                    user_level = level_system._calculate_level(data["xp"])
                    description.append(f"{rank}. {user.display_name} - Level {user_level} ({data['xp']:,} XP)")
            except:
                user_level = level_system._calculate_level(data["xp"])
                description.append(f"{rank}. Unknown User - Level {user_level} ({data['xp']:,} XP)")
        
        embed.description = "\n".join(description)
        await interaction.response.send_message(embed=embed)