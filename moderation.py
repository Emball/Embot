# [file name]: moderation.py
import discord
from discord import app_commands
from discord.ext import commands
import re
from datetime import datetime, timedelta
import asyncio
from typing import Optional
import json

MODULE_NAME = "MODERATION"

# Severity categories
CHILD_SAFETY = ["child porn", "Teen leaks"]  # Most severe
RACIAL_SLURS = ["chink", "beaner", "n i g g e r", "nigger", "nigger'", "Nigger", 
                "niggers", "niiger", "niigger"]  # Severe
TOS_VIOLATIONS = []
BANNED_WORDS = [
    "embis", "embis'", "Embis", "embis!", "Embis!", "embis's", "embiss", "embiz",
    "https://www.youtube.com/watch?v=fXvOrWWB3Vg", "https://youtu.be/fXvOrWWB3Vg",
    "https://youtu.be/fXvOrWWB3Vg?si=rSS11Yf2si_MVauu", "leaked porn", "nudes leak",
    "mbis", "m'bis", "Mbis", "mbs", "mebis", "Michael Blake Sinclair", 
    "Michael Sinclair", "montear", "www.youtube.com/watch?v=fXvOrWWB3Vg", 
    "youtube.com/watch?v=fXvOrWWB3Vg"
]  # Regular deletions only


class RolePersistenceManager:
    """Manages role persistence for users who leave and rejoin"""
    
    def __init__(self, bot):
        self.bot = bot
        self.roles_file = "member_roles.json"
        self.role_cache = self.load_roles()
    
    def load_roles(self):
        """Load saved roles from file"""
        try:
            with open(self.roles_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load role cache", e)
            return {}
    
    def save_roles(self):
        """Save roles to file"""
        try:
            with open(self.roles_file, 'w') as f:
                json.dump(self.role_cache, f, indent=2)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save role cache", e)
    
    def save_member_roles(self, member: discord.Member):
        """Save a member's roles when they leave"""
        guild_key = str(member.guild.id)
        user_key = str(member.id)
        
        if guild_key not in self.role_cache:
            self.role_cache[guild_key] = {}
        
        # Save all role IDs except @everyone
        role_ids = [role.id for role in member.roles if role.id != member.guild.id]
        
        self.role_cache[guild_key][user_key] = {
            'role_ids': role_ids,
            'saved_at': datetime.utcnow().isoformat(),
            'username': str(member)
        }
        
        self.save_roles()
        self.bot.logger.log(MODULE_NAME, f"Saved {len(role_ids)} roles for {member}")
    
    async def restore_member_roles(self, member: discord.Member):
        """Restore a member's roles when they rejoin"""
        guild_key = str(member.guild.id)
        user_key = str(member.id)
        
        if guild_key not in self.role_cache or user_key not in self.role_cache[guild_key]:
            self.bot.logger.log(MODULE_NAME, f"No saved roles found for {member}")
            return
        
        saved_data = self.role_cache[guild_key][user_key]
        role_ids = saved_data.get('role_ids', [])
        
        if not role_ids:
            return
        
        # Get role objects
        roles_to_add = []
        for role_id in role_ids:
            role = member.guild.get_role(role_id)
            if role:
                roles_to_add.append(role)
        
        if not roles_to_add:
            self.bot.logger.log(MODULE_NAME, f"No valid roles to restore for {member}")
            return
        
        try:
            await member.add_roles(*roles_to_add, reason="Role persistence - restoring previous roles")
            self.bot.logger.log(MODULE_NAME, f"Restored {len(roles_to_add)} roles for {member}")
        except discord.Forbidden:
            self.bot.logger.error(MODULE_NAME, f"No permission to restore roles for {member}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to restore roles for {member}", e)


class StrikeSystem:
    """Manages the two-strike system for auto-mod violations"""
    
    def __init__(self, bot):
        self.bot = bot
        self.strikes_file = "moderation_strikes.json"
        self.strikes = self.load_strikes()
        
    def load_strikes(self):
        """Load strikes from file"""
        try:
            with open(self.strikes_file, 'r') as f:
                data = json.load(f)
                if data:
                    for user_id in data:
                        if isinstance(data[user_id], list):
                            continue
                    return data
                return {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load strikes", e)
            return {}
    
    def save_strikes(self):
        """Save strikes to file"""
        try:
            with open(self.strikes_file, 'w') as f:
                json.dump(self.strikes, f, indent=2)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save strikes", e)
    
    def add_strike(self, user_id, reason):
        """Add a strike to a user and return current strike count"""
        user_key = str(user_id)
        
        if user_key not in self.strikes:
            self.strikes[user_key] = []
        
        strike_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'reason': reason
        }
        
        self.strikes[user_key].append(strike_data)
        self.save_strikes()
        
        strike_count = len(self.strikes[user_key])
        self.bot.logger.log(MODULE_NAME, f"Added strike to user {user_id}. Total strikes: {strike_count}")
        
        return strike_count
    
    def get_strikes(self, user_id):
        """Get strike count for a user"""
        user_key = str(user_id)
        return len(self.strikes.get(user_key, []))
    
    def get_strike_details(self, user_id):
        """Get detailed strike information for a user"""
        user_key = str(user_id)
        return self.strikes.get(user_key, [])
    
    def clear_strikes(self, user_id):
        """Clear all strikes for a user"""
        user_key = str(user_id)
        if user_key in self.strikes:
            del self.strikes[user_key]
            self.save_strikes()
            return True
        return False


class MuteManager:
    """Manages muted users"""
    
    def __init__(self, bot):
        self.bot = bot
        self.mutes_file = "muted_users.json"
        self.muted_role_name = "Muted"
        self.mutes = self.load_mutes()
    
    def load_mutes(self):
        """Load muted users from file"""
        try:
            with open(self.mutes_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load mutes", e)
            return {}
    
    def save_mutes(self):
        """Save mutes to file"""
        try:
            with open(self.mutes_file, 'w') as f:
                json.dump(self.mutes, f, indent=2)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save mutes", e)
    
    async def get_or_create_mute_role(self, guild):
        """Get or create the Muted role"""
        # Look for existing Muted role
        mute_role = discord.utils.get(guild.roles, name=self.muted_role_name)
        
        if not mute_role:
            # Create Muted role
            try:
                mute_role = await guild.create_role(
                    name=self.muted_role_name,
                    color=discord.Color.dark_gray(),
                    reason="Auto-created mute role"
                )
                
                # Set permissions for all channels
                for channel in guild.channels:
                    try:
                        await channel.set_permissions(
                            mute_role,
                            send_messages=False,
                            add_reactions=False,
                            speak=False,
                            reason="Mute role setup"
                        )
                    except:
                        pass
                
                self.bot.logger.log(MODULE_NAME, f"Created Muted role in {guild.name}")
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to create Muted role", e)
                return None
        
        return mute_role
    
    def add_mute(self, guild_id, user_id, reason, duration=None, moderator=None):
        """Add a mute record"""
        guild_key = str(guild_id)
        user_key = str(user_id)
        
        if guild_key not in self.mutes:
            self.mutes[guild_key] = {}
        
        mute_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'reason': reason,
            'moderator': str(moderator) if moderator else "Unknown",
            'duration': duration
        }
        
        if duration:
            mute_data['expires_at'] = (datetime.utcnow() + timedelta(seconds=duration)).isoformat()
        
        self.mutes[guild_key][user_key] = mute_data
        self.save_mutes()
    
    def remove_mute(self, guild_id, user_id):
        """Remove a mute record"""
        guild_key = str(guild_id)
        user_key = str(user_id)
        
        if guild_key in self.mutes and user_key in self.mutes[guild_key]:
            del self.mutes[guild_key][user_key]
            self.save_mutes()
            return True
        return False
    
    def is_muted(self, guild_id, user_id):
        """Check if a user is muted"""
        guild_key = str(guild_id)
        user_key = str(user_id)
        
        return guild_key in self.mutes and user_key in self.mutes[guild_key]


class ModerationManager:
    """Main moderation manager handling auto-mod and commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.strike_system = StrikeSystem(bot)
        self.role_persistence = RolePersistenceManager(bot)
        self.mute_manager = MuteManager(bot)
        self.banned_patterns = self.compile_patterns()
        self.bot_command_tracking = {}
        self.mod_log_channel_id = None
        self.load_config()
    
    def load_config(self):
        """Load moderation configuration"""
        try:
            with open("moderation_config.json", 'r') as f:
                config = json.load(f)
                self.mod_log_channel_id = config.get("mod_log_channel_id")
        except FileNotFoundError:
            pass
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load moderation config", e)
    
    def save_config(self):
        """Save moderation configuration"""
        try:
            config = {"mod_log_channel_id": self.mod_log_channel_id}
            with open("moderation_config.json", 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save moderation config", e)
    
    def compile_patterns(self):
        """Compile regex patterns for banned words"""
        patterns = {}
        
        # Child safety patterns (highest priority)
        patterns['child_safety'] = [re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE) 
                                    for word in CHILD_SAFETY]
        
        # Racial slurs patterns
        patterns['racial_slurs'] = [re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE) 
                                    for word in RACIAL_SLURS]
        
        # TOS violations patterns
        patterns['tos_violations'] = [re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE) 
                                      for word in TOS_VIOLATIONS]
        
        # Regular banned words patterns
        patterns['banned_words'] = [re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE) 
                                    for word in BANNED_WORDS]
        
        return patterns
    
    async def check_content(self, content):
        """Check content against banned patterns"""
        if not content:
            return None, None
        
        # Check child safety (most severe)
        for pattern in self.banned_patterns.get('child_safety', []):
            if pattern.search(content):
                return 'child_safety', pattern.pattern
        
        # Check racial slurs
        for pattern in self.banned_patterns.get('racial_slurs', []):
            if pattern.search(content):
                return 'racial_slurs', pattern.pattern
        
        # Check TOS violations
        for pattern in self.banned_patterns.get('tos_violations', []):
            if pattern.search(content):
                return 'tos_violations', pattern.pattern
        
        # Check regular banned words
        for pattern in self.banned_patterns.get('banned_words', []):
            if pattern.search(content):
                return 'banned_words', pattern.pattern
        
        return None, None
    
    async def log_mod_action(self, action_data):
        """Log moderation actions using the logger module"""
        # This now coordinates with logger.py instead of doing its own thing
        action = action_data.get('action')
        moderator = action_data.get('moderator_obj')
        user_obj = action_data.get('user_obj')
        reason = action_data.get('reason', 'No reason provided')
        
        # Get the logger event system if available
        for guild in self.bot.guilds:
            # Try to get logger from event handlers
            # The logger module will handle the actual embed creation
            if hasattr(self.bot, '_logger_event_logger'):
                await self.bot._logger_event_logger.log_moderation_action(
                    guild,
                    action,
                    moderator,
                    user_obj,
                    reason,
                    **{k: v for k, v in action_data.items() if k not in ['action', 'moderator_obj', 'user_obj', 'reason', 'user', 'user_id', 'moderator']}
                )
                break
    
    async def handle_violation(self, message, violation_type, matched_pattern):
        """Handle auto-mod violations"""
        try:
            await message.delete()
            self.bot.logger.log(MODULE_NAME, f"Deleted message from {message.author} - {violation_type}")
        except:
            pass
        
        # Child safety = immediate ban
        if violation_type == 'child_safety':
            try:
                await message.author.ban(reason=f"Auto-mod: Child safety violation - {matched_pattern}")
                self.bot.logger.log(MODULE_NAME, f"BANNED {message.author} for child safety violation")
                
                action_data = {
                    'action': 'ban',
                    'user': str(message.author),
                    'user_id': message.author.id,
                    'user_obj': message.author,
                    'moderator': 'Auto-Mod',
                    'moderator_obj': message.guild.me,  # Bot is the moderator
                    'reason': f"Child safety violation: {matched_pattern}"
                }
                await self.log_mod_action(action_data)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to ban user for child safety violation", e)
            return
        
        # Racial slurs = 1 day timeout
        if violation_type == 'racial_slurs':
            try:
                timeout_until = datetime.utcnow() + timedelta(days=1)
                await message.author.timeout(timeout_until, reason=f"Auto-mod: Racial slur - {matched_pattern}")
                self.bot.logger.log(MODULE_NAME, f"Timed out {message.author} for 1 day - racial slur")
                
                action_data = {
                    'action': 'timeout',
                    'user': str(message.author),
                    'user_id': message.author.id,
                    'user_obj': message.author,
                    'moderator': 'Auto-Mod',
                    'moderator_obj': message.guild.me,  # Bot is the moderator
                    'reason': f"Racial slur: {matched_pattern}",
                    'duration': '1 day'
                }
                await self.log_mod_action(action_data)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to timeout user", e)
            return
        
        # Other violations = strike system
        strike_count = self.strike_system.add_strike(
            message.author.id,
            f"Auto-mod: {violation_type} - {matched_pattern}"
        )
        
        # Send DM to user
        try:
            embed = discord.Embed(
                title="⚠️ Auto-Moderation Warning",
                description=f"Your message was automatically deleted for violating server rules.",
                color=0xf04747,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Violation Type", value=violation_type.replace('_', ' ').title(), inline=True)
            embed.add_field(name="Strike Count", value=f"{strike_count}/2", inline=True)
            
            if strike_count == 1:
                embed.add_field(
                    name="⚠️ Warning",
                    value="This is your first strike. One more strike will result in a permanent ban.",
                    inline=False
                )
            
            embed.set_footer(text=f"Server: {message.guild.name}")
            await message.author.send(embed=embed)
        except discord.Forbidden:
            pass
        
        # Ban on second strike
        if strike_count >= 2:
            try:
                await message.author.ban(reason=f"Auto-mod: Second strike - {violation_type}")
                self.bot.logger.log(MODULE_NAME, f"BANNED {message.author} after second strike")
                
                action_data = {
                    'action': 'ban',
                    'user': str(message.author),
                    'user_id': message.author.id,
                    'user_obj': message.author,
                    'moderator': 'Auto-Mod',
                    'moderator_obj': message.guild.me,  # Bot is the moderator
                    'reason': f"Second strike: {violation_type}"
                }
                await self.log_mod_action(action_data)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to ban user after second strike", e)


def setup(bot):
    """Setup function called by main.py"""
    
    moderation_manager = ModerationManager(bot)
    
    # Event listeners
    @bot.event
    async def on_message(message):
        """Auto-mod message checking"""
        if message.author.bot:
            await bot.process_commands(message)
            return
        
        if not message.guild:
            await bot.process_commands(message)
            return
        
        # Check message content
        violation_type, matched_pattern = await moderation_manager.check_content(message.content)
        
        if violation_type:
            await moderation_manager.handle_violation(message, violation_type, matched_pattern)
            return
        
        # Check embeds
        for embed in message.embeds:
            if embed.description:
                violation_type, matched_pattern = await moderation_manager.check_content(embed.description)
                if violation_type:
                    await moderation_manager.handle_violation(message, violation_type, matched_pattern)
                    return
        
        await bot.process_commands(message)
    
    @bot.event
    async def on_member_remove(member):
        """Save roles when member leaves"""
        moderation_manager.role_persistence.save_member_roles(member)
    
    @bot.event
    async def on_member_join(member):
        """Restore roles when member rejoins"""
        await moderation_manager.role_persistence.restore_member_roles(member)
    
    # ==================== MODERATION COMMANDS ====================
    
    @bot.tree.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        user="User to ban (mention, ID, or username)",
        reason="Reason for ban",
        delete_days="Days of messages to delete (0-7)"
    )
    @app_commands.default_permissions(ban_members=True)
    async def ban(interaction: discord.Interaction, user: discord.User, 
                  reason: Optional[str] = "No reason provided", delete_days: Optional[int] = 1):
        """Ban a user (works even if they're not in the server)"""
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You don't have permission to ban members.", ephemeral=True)
            return
        
        if user == interaction.user:
            await interaction.response.send_message("❌ You cannot ban yourself.", ephemeral=True)
            return
        
        if user == bot.user:
            await interaction.response.send_message("❌ I cannot ban myself.", ephemeral=True)
            return
        
        # Check if user is a member in the server
        member = interaction.guild.get_member(user.id)
        if member:
            # If they're in the server, check role hierarchy
            if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
                await interaction.response.send_message("❌ You cannot ban someone with a higher or equal role.", ephemeral=True)
                return
        
        delete_days = max(0, min(7, delete_days))
        
        try:
            # Send DM before banning
            try:
                embed = discord.Embed(
                    title="🔨 You have been banned",
                    description=f"You have been banned from **{interaction.guild.name}**",
                    color=0x992d22,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            
            # Ban the user
            await interaction.guild.ban(user, reason=f"{reason} - By {interaction.user}", delete_message_days=delete_days)
            
            # Log action
            action_data = {
                'action': 'ban',
                'user': str(user),
                'user_id': user.id,
                'user_obj': user,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            # Respond
            embed = discord.Embed(
                title="✅ User Banned",
                description=f"**{user}** has been banned from the server.",
                color=0x992d22,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} banned {user}")
            
        except discord.NotFound:
            await interaction.response.send_message("❌ User not found.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban this user.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to ban the user.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Ban command failed", e)
    
    @bot.tree.command(name="unban", description="Unban a user from the server")
    @app_commands.describe(
        user_id="User ID to unban",
        reason="Reason for unban"
    )
    @app_commands.default_permissions(ban_members=True)
    async def unban(interaction: discord.Interaction, user_id: str, reason: Optional[str] = "No reason provided"):
        """Unban a user"""
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You don't have permission to unban members.", ephemeral=True)
            return
        
        try:
            user_id_int = int(user_id)
            user = await bot.fetch_user(user_id_int)
            
            await interaction.guild.unban(user, reason=f"{reason} - By {interaction.user}")
            
            action_data = {
                'action': 'unban',
                'user': str(user),
                'user_id': user.id,
                'user_obj': user,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ User Unbanned",
                description=f"**{user}** has been unbanned from the server.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} unbanned {user}")
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID provided.", ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message("❌ User not found or not banned.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to unban the user.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Unban command failed", e)
    
    @bot.tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(
        member="Member to kick",
        reason="Reason for kick"
    )
    @app_commands.default_permissions(kick_members=True)
    async def kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
        """Kick a member"""
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ You don't have permission to kick members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot kick yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot kick myself.", ephemeral=True)
            return
        
        if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            await interaction.response.send_message("❌ You cannot kick someone with a higher or equal role.", ephemeral=True)
            return
        
        try:
            # Send DM before kicking
            try:
                embed = discord.Embed(
                    title="👢 You have been kicked",
                    description=f"You have been kicked from **{interaction.guild.name}**",
                    color=0xe67e22,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                embed.set_footer(text="You can rejoin if you have an invite link")
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            await member.kick(reason=f"{reason} - By {interaction.user}")
            
            action_data = {
                'action': 'kick',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Kicked",
                description=f"**{member}** has been kicked from the server.",
                color=0xe67e22,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} kicked {member}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to kick the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Kick command failed", e)
    
    @bot.tree.command(name="timeout", description="Timeout a member")
    @app_commands.describe(
        member="Member to timeout",
        duration="Duration in minutes",
        reason="Reason for timeout"
    )
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(interaction: discord.Interaction, member: discord.Member, 
                     duration: int, reason: Optional[str] = "No reason provided"):
        """Timeout a member"""
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ You don't have permission to timeout members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot timeout yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot timeout myself.", ephemeral=True)
            return
        
        if duration < 1 or duration > 40320:  # Max 28 days
            await interaction.response.send_message("❌ Duration must be between 1 minute and 28 days (40320 minutes).", ephemeral=True)
            return
        
        try:
            timeout_until = datetime.utcnow() + timedelta(minutes=duration)
            await member.timeout(timeout_until, reason=f"{reason} - By {interaction.user}")
            
            action_data = {
                'action': 'timeout',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason,
                'duration': f"{duration} minutes"
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Timed Out",
                description=f"**{member}** has been timed out for **{duration}** minutes.",
                color=0xe74c3c,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Duration", value=f"{duration} minutes", inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} timed out {member} for {duration} minutes")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to timeout the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Timeout command failed", e)
    
    @bot.tree.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(interaction: discord.Interaction, member: discord.Member):
        """Remove timeout from a member"""
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ You don't have permission to remove timeouts.", ephemeral=True)
            return
        
        try:
            await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
            
            embed = discord.Embed(
                title="✅ Timeout Removed",
                description=f"**{member}**'s timeout has been removed.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} removed timeout from {member}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to remove the timeout.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Untimeout command failed", e)
    
    @bot.tree.command(name="mute", description="Mute a member")
    @app_commands.describe(
        member="Member to mute",
        reason="Reason for mute",
        duration="Duration in minutes (optional)"
    )
    @app_commands.default_permissions(manage_roles=True)
    async def mute(interaction: discord.Interaction, member: discord.Member, 
                   reason: Optional[str] = "No reason provided", duration: Optional[int] = None):
        """Mute a member"""
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to mute members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot mute yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot mute myself.", ephemeral=True)
            return
        
        try:
            mute_role = await moderation_manager.mute_manager.get_or_create_mute_role(interaction.guild)
            if not mute_role:
                await interaction.response.send_message("❌ Failed to get or create Muted role.", ephemeral=True)
                return
            
            await member.add_roles(mute_role, reason=f"{reason} - By {interaction.user}")
            
            # Track mute
            moderation_manager.mute_manager.add_mute(
                interaction.guild.id,
                member.id,
                reason,
                duration * 60 if duration else None,
                interaction.user
            )
            
            action_data = {
                'action': 'mute',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason
            }
            if duration:
                action_data['duration'] = f"{duration} minutes"
            
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Muted",
                description=f"**{member}** has been muted.",
                color=0xf39c12,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            if duration:
                embed.add_field(name="Duration", value=f"{duration} minutes", inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} muted {member}")
            
            # Auto-unmute after duration
            if duration:
                await asyncio.sleep(duration * 60)
                if mute_role in member.roles:
                    await member.remove_roles(mute_role, reason="Mute duration expired")
                    moderation_manager.mute_manager.remove_mute(interaction.guild.id, member.id)
                    bot.logger.log(MODULE_NAME, f"Auto-unmuted {member} after {duration} minutes")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to mute the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Mute command failed", e)
    
    @bot.tree.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Member to unmute")
    @app_commands.default_permissions(manage_roles=True)
    async def unmute(interaction: discord.Interaction, member: discord.Member):
        """Unmute a member"""
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to unmute members.", ephemeral=True)
            return
        
        try:
            mute_role = discord.utils.get(interaction.guild.roles, name="Muted")
            if not mute_role or mute_role not in member.roles:
                await interaction.response.send_message("❌ This member is not muted.", ephemeral=True)
                return
            
            await member.remove_roles(mute_role, reason=f"Unmuted by {interaction.user}")
            moderation_manager.mute_manager.remove_mute(interaction.guild.id, member.id)
            
            action_data = {
                'action': 'unmute',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': 'Manual unmute'
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Unmuted",
                description=f"**{member}** has been unmuted.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} unmuted {member}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to unmute the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Unmute command failed", e)
    
    @bot.tree.command(name="softban", description="Softban a member (ban then immediately unban to delete messages)")
    @app_commands.describe(
        member="Member to softban",
        reason="Reason for softban",
        delete_days="Days of messages to delete (0-7)"
    )
    @app_commands.default_permissions(ban_members=True)
    async def softban(interaction: discord.Interaction, member: discord.Member, 
                      reason: Optional[str] = "No reason provided", delete_days: Optional[int] = 1):
        """Softban a member"""
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You don't have permission to softban members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot softban yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot softban myself.", ephemeral=True)
            return
        
        delete_days = max(0, min(7, delete_days))
        
        try:
            # Ban then unban
            await member.ban(reason=f"Softban: {reason} - By {interaction.user}", delete_message_days=delete_days)
            await interaction.guild.unban(member, reason=f"Softban unban - By {interaction.user}")
            
            action_data = {
                'action': 'softban',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Softbanned",
                description=f"**{member}** has been softbanned (messages deleted, can rejoin).",
                color=0x992d22,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} softbanned {member}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to softban the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Softban command failed", e)
    
    @bot.tree.command(name="purge", description="Delete multiple messages")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        user="Optional: Only delete messages from this user"
    )
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
        """Purge messages"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You don't have permission to purge messages.", ephemeral=True)
            return
        
        if amount < 1 or amount > 100:
            await interaction.response.send_message("❌ Amount must be between 1 and 100.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            def check(m):
                if user:
                    return m.author.id == user.id
                return True
            
            deleted = await interaction.channel.purge(limit=amount, check=check)
            
            embed = discord.Embed(
                title="✅ Messages Purged",
                description=f"Deleted **{len(deleted)}** messages{f' from {user.mention}' if user else ''}.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            bot.logger.log(MODULE_NAME, f"{interaction.user} purged {len(deleted)} messages")
            
        except Exception as e:
            await interaction.followup.send("❌ An error occurred while trying to purge messages.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Purge command failed", e)
    
    @bot.tree.command(name="slowmode", description="Set channel slowmode")
    @app_commands.describe(
        seconds="Slowmode delay in seconds (0 to disable)",
        channel="Channel to apply slowmode to (default: current)"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(interaction: discord.Interaction, seconds: int, 
                       channel: Optional[discord.TextChannel] = None):
        """Set slowmode"""
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ You don't have permission to manage channels.", ephemeral=True)
            return
        
        target_channel = channel or interaction.channel
        
        if seconds < 0 or seconds > 21600:
            await interaction.response.send_message("❌ Slowmode must be between 0 and 21600 seconds (6 hours).", ephemeral=True)
            return
        
        try:
            await target_channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
            
            action_data = {
                'action': 'slowmode',
                'user': 'N/A',
                'user_id': 'N/A',
                'user_obj': None,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': f"Slowmode set to {seconds} seconds",
                'channel': target_channel.mention
            }
            await moderation_manager.log_mod_action(action_data)
            
            if seconds == 0:
                embed = discord.Embed(
                    title="✅ Slowmode Disabled",
                    description=f"Slowmode has been disabled in {target_channel.mention}.",
                    color=0x2ecc71
                )
            else:
                embed = discord.Embed(
                    title="✅ Slowmode Enabled",
                    description=f"Slowmode set to **{seconds}** seconds in {target_channel.mention}.",
                    color=0x3498db
                )
            
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} set slowmode to {seconds}s in {target_channel.name}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to set slowmode.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Slowmode command failed", e)
    
    @bot.tree.command(name="lock", description="Lock a channel")
    @app_commands.describe(
        channel="Channel to lock (default: current)",
        reason="Reason for locking"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def lock(interaction: discord.Interaction, 
                   channel: Optional[discord.TextChannel] = None,
                   reason: Optional[str] = "No reason provided"):
        """Lock a channel"""
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ You don't have permission to lock channels.", ephemeral=True)
            return
        
        target_channel = channel or interaction.channel
        
        try:
            # Deny send messages for @everyone
            overwrites = target_channel.overwrites_for(interaction.guild.default_role)
            overwrites.send_messages = False
            await target_channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrites,
                reason=f"Channel locked by {interaction.user}: {reason}"
            )
            
            action_data = {
                'action': 'lock',
                'user': 'N/A',
                'user_id': 'N/A',
                'user_obj': None,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason,
                'channel': target_channel.mention
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="🔒 Channel Locked",
                description=f"{target_channel.mention} has been locked.",
                color=0x95a5a6,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} locked {target_channel.name}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to lock the channel.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Lock command failed", e)
    
    @bot.tree.command(name="unlock", description="Unlock a channel")
    @app_commands.describe(
        channel="Channel to unlock (default: current)",
        reason="Reason for unlocking"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(interaction: discord.Interaction, 
                     channel: Optional[discord.TextChannel] = None,
                     reason: Optional[str] = "No reason provided"):
        """Unlock a channel"""
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ You don't have permission to unlock channels.", ephemeral=True)
            return
        
        target_channel = channel or interaction.channel
        
        try:
            # Allow send messages for @everyone
            overwrites = target_channel.overwrites_for(interaction.guild.default_role)
            overwrites.send_messages = None  # Reset to default
            await target_channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrites,
                reason=f"Channel unlocked by {interaction.user}: {reason}"
            )
            
            action_data = {
                'action': 'unlock',
                'user': 'N/A',
                'user_id': 'N/A',
                'user_obj': None,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason,
                'channel': target_channel.mention
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="🔓 Channel Unlocked",
                description=f"{target_channel.mention} has been unlocked.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} unlocked {target_channel.name}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to unlock the channel.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Unlock command failed", e)
    
    @bot.tree.command(name="warn", description="Warn a member")
    @app_commands.describe(
        member="Member to warn",
        reason="Reason for warning"
    )
    @app_commands.default_permissions(manage_messages=True)
    async def warn(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
        """Warn a member"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You don't have permission to warn members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot warn yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot warn myself.", ephemeral=True)
            return
        
        try:
            try:
                embed = discord.Embed(
                    title="⚠️ Warning",
                    description=f"You have been warned by a moderator.",
                    color=0xfaa61a,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                embed.add_field(name="Server", value=interaction.guild.name, inline=True)
                embed.set_footer(text="Please follow server rules to avoid further action")
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            action_data = {
                'action': 'warn',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': reason
            }
            
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Warned",
                description=f"**{member}** has been warned.",
                color=0xfaa61a,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} warned {member}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to warn the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Warn command failed", e)
    
    @bot.tree.command(name="strikes", description="Check a user's strike count")
    @app_commands.describe(member="Member to check strikes for")
    @app_commands.default_permissions(manage_messages=True)
    async def strikes(interaction: discord.Interaction, member: discord.Member):
        """Check strikes command"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You don't have permission to view strikes.", ephemeral=True)
            return
        
        strike_count = moderation_manager.strike_system.get_strikes(member.id)
        strike_details = moderation_manager.strike_system.get_strike_details(member.id)
        
        embed = discord.Embed(
            title="⚡ Strike Information",
            description=f"**{member}** has **{strike_count}** strike(s)",
            color=0x992d22 if strike_count >= 2 else (0xf04747 if strike_count == 1 else 0x57f287),
            timestamp=datetime.utcnow()
        )
        
        embed.set_thumbnail(url=member.display_avatar.url)
        
        if strike_count > 0:
            strike_list = []
            for i, strike in enumerate(strike_details, 1):
                strike_time = datetime.fromisoformat(strike['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                strike_list.append(f"**{i}.** {strike_time}\n└─ {strike['reason']}")
            
            embed.add_field(name="Strike History", value="\n".join(strike_list), inline=False)
            
            if strike_count == 1:
                embed.add_field(name="⚠️ Warning", value="Next strike will result in a permanent ban", inline=False)
        else:
            embed.add_field(name="Status", value="✅ No strikes on record", inline=False)
        
        embed.set_footer(text=f"User ID: {member.id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="clear_strikes", description="Clear all strikes for a user")
    @app_commands.describe(member="Member to clear strikes for")
    @app_commands.default_permissions(administrator=True)
    async def clear_strikes(interaction: discord.Interaction, member: discord.Member):
        """Clear strikes command"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You don't have permission to clear strikes.", ephemeral=True)
            return
        
        if moderation_manager.strike_system.clear_strikes(member.id):
            action_data = {
                'action': 'clear_strikes',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'moderator_obj': interaction.user,
                'reason': 'Strikes cleared by administrator'
            }
            
            await moderation_manager.log_mod_action(action_data)
            
            await interaction.response.send_message(
                f"✅ Cleared all strikes for **{member}**",
                ephemeral=True
            )
            bot.logger.log(MODULE_NAME, f"{interaction.user} cleared strikes for {member}")
        else:
            await interaction.response.send_message(
                f"❌ **{member}** has no strikes to clear.",
                ephemeral=True
            )
    
    @bot.tree.command(name="modstats", description="View moderation statistics")
    @app_commands.default_permissions(manage_messages=True)
    async def modstats(interaction: discord.Interaction):
        """View moderation statistics"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You don't have permission to view moderation stats.", ephemeral=True)
            return
        
        total_users_with_strikes = len(moderation_manager.strike_system.strikes)
        total_strikes = sum(len(strikes) for strikes in moderation_manager.strike_system.strikes.values())
        
        users_at_risk = sum(1 for strikes in moderation_manager.strike_system.strikes.values() if len(strikes) == 1)
        users_banned = sum(1 for strikes in moderation_manager.strike_system.strikes.values() if len(strikes) >= 2)
        
        total_saved_roles = sum(
            len(users) 
            for guild_data in moderation_manager.role_persistence.role_cache.values() 
            for users in [guild_data]
        )
        
        embed = discord.Embed(
            title="📊 Moderation Statistics",
            color=0x5865f2,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="Total Strikes", value=f"⚡ {total_strikes}", inline=True)
        embed.add_field(name="Users with Strikes", value=f"👤 {total_users_with_strikes}", inline=True)
        embed.add_field(name="Users at Risk", value=f"⚠️ {users_at_risk}", inline=True)
        
        embed.add_field(
            name="Auto-Mod Categories",
            value="🔴 Child Safety\n🔴 Racial Slurs (1 day timeout)\n🔴 TOS Violations\n⚪ Banned Words",
            inline=False
        )
        
        embed.add_field(
            name="Role Persistence",
            value=f"💾 {total_saved_roles} users with saved roles",
            inline=False
        )
        
        embed.add_field(
            name="Enhanced Features",
            value="🤖 Bot command output monitoring\n📨 Forwarded message detection\n📎 Embed content scanning",
            inline=False
        )
        
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="setmodlog", description="Set the moderation log channel")
    @app_commands.describe(channel="Channel to send moderation logs to")
    @app_commands.default_permissions(administrator=True)
    async def set_mod_log(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set mod log channel"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission to use this command.", ephemeral=True)
            return
        
        moderation_manager.mod_log_channel_id = channel.id
        moderation_manager.save_config()
        
        embed = discord.Embed(
            title="✅ Moderation Log Channel Set",
            description=f"Moderation logs will now be sent to {channel.mention}",
            color=0x2ecc71
        )
        
        await interaction.response.send_message(embed=embed)
        bot.logger.log(MODULE_NAME, f"Mod log channel set to {channel.name} by {interaction.user}")
    
    # ==================== TEXT-BASED COMMAND SUPPORT (? prefix) ====================
    # Add text-based versions of all moderation commands for ? prefix support
    
    @bot.command(name="ban")
    @commands.has_permissions(ban_members=True)
    async def text_ban(ctx, user: discord.User, delete_days: int = 1, *, reason: str = "No reason provided"):
        """Ban a user (text command version)"""
        if user == ctx.author:
            await ctx.send("❌ You cannot ban yourself.")
            return
        if user == bot.user:
            await ctx.send("❌ I cannot ban myself.")
            return
        
        delete_days = max(0, min(delete_days, 7))
        
        try:
            try:
                embed = discord.Embed(
                    title="🔨 Banned from Server",
                    description=f"You have been banned from **{ctx.guild.name}**.",
                    color=0x992d22,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            
            await ctx.guild.ban(user, reason=reason, delete_message_days=delete_days)
            
            action_data = {
                'action': 'ban',
                'user': str(user),
                'user_id': user.id,
                'user_obj': user,
                'moderator': str(ctx.author),
                'moderator_obj': ctx.author,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ User Banned",
                description=f"**{user}** has been banned from the server.",
                color=0x992d22,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
            embed.add_field(name="Messages Deleted", value=f"Last {delete_days} day(s)", inline=True)
            
            await ctx.send(embed=embed)
            bot.logger.log(MODULE_NAME, f"{ctx.author} banned {user}")
            
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to ban this user.")
        except Exception as e:
            await ctx.send("❌ An error occurred while trying to ban the user.")
            bot.logger.error(MODULE_NAME, "Ban command failed", e)
    
    @bot.command(name="kick")
    @commands.has_permissions(kick_members=True)
    async def text_kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Kick a member (text command version)"""
        if member == ctx.author:
            await ctx.send("❌ You cannot kick yourself.")
            return
        if member == bot.user:
            await ctx.send("❌ I cannot kick myself.")
            return
        
        try:
            try:
                embed = discord.Embed(
                    title="👢 Kicked from Server",
                    description=f"You have been kicked from **{ctx.guild.name}**.",
                    color=0xe67e22,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            await member.kick(reason=reason)
            
            action_data = {
                'action': 'kick',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(ctx.author),
                'moderator_obj': ctx.author,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Kicked",
                description=f"**{member}** has been kicked from the server.",
                color=0xe67e22,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=embed)
            bot.logger.log(MODULE_NAME, f"{ctx.author} kicked {member}")
            
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to kick this member.")
        except Exception as e:
            await ctx.send("❌ An error occurred while trying to kick the member.")
            bot.logger.error(MODULE_NAME, "Kick command failed", e)
    
    @bot.command(name="warn")
    @commands.has_permissions(manage_messages=True)
    async def text_warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Warn a member (text command version)"""
        if member == ctx.author:
            await ctx.send("❌ You cannot warn yourself.")
            return
        if member == bot.user:
            await ctx.send("❌ I cannot warn myself.")
            return
        
        try:
            try:
                embed = discord.Embed(
                    title="⚠️ Warning",
                    description=f"You have been warned by a moderator.",
                    color=0xfaa61a,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
                embed.add_field(name="Server", value=ctx.guild.name, inline=True)
                embed.set_footer(text="Please follow server rules to avoid further action")
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            action_data = {
                'action': 'warn',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(ctx.author),
                'moderator_obj': ctx.author,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Warned",
                description=f"**{member}** has been warned.",
                color=0xfaa61a,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=embed)
            bot.logger.log(MODULE_NAME, f"{ctx.author} warned {member}")
            
        except Exception as e:
            await ctx.send("❌ An error occurred while trying to warn the member.")
            bot.logger.error(MODULE_NAME, "Warn command failed", e)
    
    @bot.command(name="mute")
    @commands.has_permissions(manage_roles=True)
    async def text_mute(ctx, member: discord.Member, duration: Optional[str] = None, *, reason: str = "No reason provided"):
        """Mute a member (text command version)"""
        if member == ctx.author:
            await ctx.send("❌ You cannot mute yourself.")
            return
        if member == bot.user:
            await ctx.send("❌ I cannot mute myself.")
            return
        
        duration_seconds = None
        duration_str = "Permanent"
        
        if duration:
            duration = duration.lower()
            match = re.match(r'^(\d+)([smhd])$', duration)
            if match:
                value, unit = int(match.group(1)), match.group(2)
                if unit == 's':
                    duration_seconds = value
                    duration_str = f"{value} second{'s' if value != 1 else ''}"
                elif unit == 'm':
                    duration_seconds = value * 60
                    duration_str = f"{value} minute{'s' if value != 1 else ''}"
                elif unit == 'h':
                    duration_seconds = value * 3600
                    duration_str = f"{value} hour{'s' if value != 1 else ''}"
                elif unit == 'd':
                    duration_seconds = value * 86400
                    duration_str = f"{value} day{'s' if value != 1 else ''}"
        
        try:
            muted_role = discord.utils.get(ctx.guild.roles, name=moderation_manager.mute_manager.muted_role_name)
            
            if not muted_role:
                muted_role = await ctx.guild.create_role(
                    name=moderation_manager.mute_manager.muted_role_name,
                    color=discord.Color.dark_gray(),
                    reason="Creating Muted role for moderation"
                )
                
                for channel in ctx.guild.channels:
                    try:
                        await channel.set_permissions(muted_role, send_messages=False, speak=False)
                    except:
                        pass
            
            await member.add_roles(muted_role, reason=reason)
            moderation_manager.mute_manager.add_mute(ctx.guild.id, member.id, reason, ctx.author, duration_seconds)
            
            try:
                embed = discord.Embed(
                    title="🔇 You Have Been Muted",
                    description=f"You have been muted in **{ctx.guild.name}**.",
                    color=0xf39c12,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Duration", value=duration_str, inline=True)
                embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            action_data = {
                'action': 'mute',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(ctx.author),
                'moderator_obj': ctx.author,
                'reason': reason,
                'duration': duration_str
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Muted",
                description=f"**{member}** has been muted.",
                color=0xf39c12,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Duration", value=duration_str, inline=True)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=embed)
            bot.logger.log(MODULE_NAME, f"{ctx.author} muted {member} for {duration_str}")
            
            if duration_seconds:
                await asyncio.sleep(duration_seconds)
                if moderation_manager.mute_manager.is_muted(ctx.guild.id, member.id):
                    try:
                        await member.remove_roles(muted_role, reason="Mute duration expired")
                        moderation_manager.mute_manager.remove_mute(ctx.guild.id, member.id)
                        bot.logger.log(MODULE_NAME, f"Auto-unmuted {member} after {duration_str}")
                    except:
                        pass
            
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to mute this member.")
        except Exception as e:
            await ctx.send("❌ An error occurred while trying to mute the member.")
            bot.logger.error(MODULE_NAME, "Mute command failed", e)
    
    @bot.command(name="unmute")
    @commands.has_permissions(manage_roles=True)
    async def text_unmute(ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Unmute a member (text command version)"""
        try:
            muted_role = discord.utils.get(ctx.guild.roles, name=moderation_manager.mute_manager.muted_role_name)
            
            if not muted_role or muted_role not in member.roles:
                await ctx.send(f"❌ **{member}** is not muted.")
                return
            
            await member.remove_roles(muted_role, reason=reason)
            moderation_manager.mute_manager.remove_mute(ctx.guild.id, member.id)
            
            try:
                embed = discord.Embed(
                    title="🔊 You Have Been Unmuted",
                    description=f"You have been unmuted in **{ctx.guild.name}**.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            action_data = {
                'action': 'unmute',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(ctx.author),
                'moderator_obj': ctx.author,
                'reason': reason
            }
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Unmuted",
                description=f"**{member}** has been unmuted.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=embed)
            bot.logger.log(MODULE_NAME, f"{ctx.author} unmuted {member}")
            
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to unmute this member.")
        except Exception as e:
            await ctx.send("❌ An error occurred while trying to unmute the member.")
            bot.logger.error(MODULE_NAME, "Unmute command failed", e)
    
    @bot.command(name="purge")
    @commands.has_permissions(manage_messages=True)
    async def text_purge(ctx, amount: int, member: Optional[discord.Member] = None):
        """Purge messages (text command version)"""
        if amount < 1 or amount > 1000:
            await ctx.send("❌ Please specify a number between 1 and 1000.")
            return
        
        try:
            if member:
                deleted = await ctx.channel.purge(limit=amount + 1, check=lambda m: m.author == member)
            else:
                deleted = await ctx.channel.purge(limit=amount + 1)
            
            count = len(deleted) - 1
            
            action_data = {
                'action': 'purge',
                'user': None,
                'user_id': None,
                'user_obj': None,
                'moderator': str(ctx.author),
                'moderator_obj': ctx.author,
                'reason': f"Purged {count} message(s)" + (f" from {member}" if member else ""),
                'channel': ctx.channel.mention,
                'amount': count
            }
            await moderation_manager.log_mod_action(action_data)
            
            msg = await ctx.send(f"✅ Deleted {count} message(s)" + (f" from {member.mention}" if member else ""))
            await asyncio.sleep(3)
            await msg.delete()
            
            bot.logger.log(MODULE_NAME, f"{ctx.author} purged {count} messages in {ctx.channel}")
            
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to delete messages.")
        except Exception as e:
            await ctx.send("❌ An error occurred while trying to purge messages.")
            bot.logger.error(MODULE_NAME, "Purge command failed", e)

    bot.logger.log(MODULE_NAME, "Moderation module setup complete (slash + text commands)")