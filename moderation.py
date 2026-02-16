# [file name]: moderation.py
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import re
from datetime import datetime, timedelta
import asyncio
from typing import Optional
import json
import os
from pathlib import Path

MODULE_NAME = "MODERATION"

# ==================== HARDCODED CONFIGURATION ====================
# Channel resolution is handled by logger.py (bot._logger_event_logger).

CONFIG = {
    "elevated_roles": ["Moderator", "Admin", "Owner"],
    "moderation": {"min_reason_length": 10, "muted_role_name": "Muted"}
}

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

# Elevated roles for moderation
ELEVATED_ROLES = CONFIG.get("elevated_roles", ["Moderator", "Admin", "Owner"])

# Minimum reason length
MIN_REASON_LENGTH = CONFIG.get("moderation", {}).get("min_reason_length", 10)

# Error messages
ERROR_NO_PERMISSION = "❌ You need a moderation role (Moderator, Admin, or Owner) to use this command."
ERROR_REASON_REQUIRED = "❌ You must provide a reason for this action."
ERROR_REASON_TOO_SHORT = f"❌ Reason must be at least {MIN_REASON_LENGTH} characters long."
ERROR_CANNOT_ACTION_SELF = "❌ You cannot perform this action on yourself."
ERROR_CANNOT_ACTION_BOT = "❌ I cannot perform this action on myself."
ERROR_HIGHER_ROLE = "❌ You cannot perform this action on someone with a higher or equal role."


def has_elevated_role(member: discord.Member) -> bool:
    """Check if member has an elevated moderation role or is the server owner"""
    # Server owner always has elevated permissions
    if member.guild.owner_id == member.id:
        return True
    
    return any(role.name in ELEVATED_ROLES for role in member.roles)


async def validate_reason(reason: Optional[str]) -> tuple:
    """Validate reason meets minimum length requirement"""
    if not reason or reason.strip() == "" or reason == "No reason provided":
        return False, ERROR_REASON_REQUIRED
    
    if len(reason) < MIN_REASON_LENGTH:
        return False, ERROR_REASON_TOO_SHORT
    
    return True, None


async def send_error_dm(user: discord.User, error_message: str) -> bool:
    """
    Send error message via DM to avoid channel clutter.
    Returns True if DM was sent successfully, False otherwise.
    """
    try:
        embed = discord.Embed(
            title="❌ Command Error",
            description=error_message,
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        return False
    except Exception:
        return False


async def send_tracked_response(interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False) -> Optional[int]:
    """Send response and return message ID for tracking"""
    try:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        
        if not ephemeral:
            response = await interaction.original_response()
            return response.id
        
        return None
    except Exception:
        return None





async def log_mod_action_with_tracking(bot, interaction: discord.Interaction, action: str, user_id: Optional[int],
                                      user: Optional[str], reason: str, inchat_msg_id: Optional[int],
                                      botlog_msg_id: Optional[int], duration: Optional[str] = None,
                                      additional: Optional[dict] = None) -> Optional[str]:
    """Log moderation action to oversight system and track embeds"""
    if not hasattr(bot, 'mod_oversight'):
        return None
    
    try:
        action_id = await bot.mod_oversight.log_mod_action({
            'action': action,
            'moderator_id': interaction.user.id,
            'moderator': str(interaction.user),
            'user_id': user_id,
            'user': user,
            'reason': reason,
            'guild_id': interaction.guild.id,
            'channel_id': interaction.channel.id,
            'message_id': interaction.id,
            'duration': duration,
            'additional': additional or {}
        })
        
        if inchat_msg_id:
            bot.mod_oversight.track_embed(inchat_msg_id, action_id, 'inchat')
        if botlog_msg_id:
            bot.mod_oversight.track_embed(botlog_msg_id, action_id, 'botlog')
        
        return action_id
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to log action to oversight", e)
        return None


class BanAppealView(ui.View):
    """View with appeal button for banned users"""
    
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
    
    @ui.button(label="Submit Appeal", style=discord.ButtonStyle.primary, emoji="📝")
    async def appeal_button(self, interaction: discord.Interaction, button: ui.Button):
        """Handle appeal button click"""
        from mod_oversight import BanAppealModal
        
        if not hasattr(interaction.client, 'mod_oversight'):
            await interaction.response.send_message("❌ Appeal system not available.", ephemeral=True)
            return
        
        modal = BanAppealModal(interaction.client.mod_oversight, self.guild_id)
        await interaction.response.send_modal(modal)


class RolePersistenceManager:
    """Manages role persistence for users who leave and rejoin"""
    
    def __init__(self, bot):
        self.bot = bot
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.roles_file = str(data_dir / "member_roles.json")
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
        """Save roles to file atomically"""
        try:
            import tempfile
            # Write to temporary file first
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(self.roles_file), suffix='.tmp')
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(self.role_cache, f, indent=2)
                # Atomic replace
                os.replace(temp_path, self.roles_file)
            except:
                # Clean up temp file if something fails
                try:
                    os.unlink(temp_path)
                except:
                    pass
                raise
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
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.strikes_file = str(data_dir / "moderation_strikes.json")
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
        """Save strikes to file atomically"""
        try:
            import tempfile
            # Write to temporary file first
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(self.strikes_file), suffix='.tmp')
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(self.strikes, f, indent=2)
                # Atomic replace
                os.replace(temp_path, self.strikes_file)
            except:
                # Clean up temp file if something fails
                try:
                    os.unlink(temp_path)
                except:
                    pass
                raise
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
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.mutes_file = str(data_dir / "muted_users.json")
        self.muted_role_name = CONFIG.get("moderation", {}).get("muted_role_name", "Muted")
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
        """Save mutes to file atomically"""
        try:
            import tempfile
            # Write to temporary file first
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(self.mutes_file), suffix='.tmp')
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(self.mutes, f, indent=2)
                # Atomic replace
                os.replace(temp_path, self.mutes_file)
            except:
                # Clean up temp file if something fails
                try:
                    os.unlink(temp_path)
                except:
                    pass
                raise
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save mutes", e)
    
    def add_mute(self, guild_id, user_id, reason, moderator, duration_seconds=None):
        """Add a mute record with expiry time for persistent timers"""
        guild_key = str(guild_id)
        user_key = str(user_id)
        
        if guild_key not in self.mutes:
            self.mutes[guild_key] = {}
        
        expiry_time = None
        if duration_seconds:
            expiry_time = (datetime.utcnow() + timedelta(seconds=duration_seconds)).isoformat()
        
        mute_data = {
            'user_id': user_id,
            'reason': reason,
            'moderator': str(moderator),
            'timestamp': datetime.utcnow().isoformat(),
            'duration_seconds': duration_seconds,
            'expiry_time': expiry_time
        }
        
        self.mutes[guild_key][user_key] = mute_data
        self.save_mutes()
    
    def get_expired_mutes(self):
        """Get all mutes that have expired"""
        expired = []
        now = datetime.utcnow()
        
        for guild_key, users in self.mutes.items():
            for user_key, mute_data in users.items():
                expiry_time = mute_data.get('expiry_time')
                if expiry_time:
                    try:
                        expiry = datetime.fromisoformat(expiry_time)
                        if now >= expiry:
                            expired.append({
                                'guild_id': int(guild_key),
                                'user_id': mute_data['user_id'],
                                'user_key': user_key,
                                'guild_key': guild_key
                            })
                    except (ValueError, AttributeError):
                        pass
        
        return expired
    
    def remove_mute(self, guild_id, user_id):
        """Remove a mute record"""
        guild_key = str(guild_id)
        user_key = str(user_id)
        
        if guild_key in self.mutes and user_key in self.mutes[guild_key]:
            del self.mutes[guild_key][user_key]
            self.save_mutes()
    
    def is_muted(self, guild_id, user_id):
        """Check if a user is muted"""
        guild_key = str(guild_id)
        user_key = str(user_id)
        return guild_key in self.mutes and user_key in self.mutes[guild_key]


class ModerationManager:
    """Main moderation manager"""
    
    def __init__(self, bot):
        self.bot = bot
        self.strike_system = StrikeSystem(bot)
        self.mute_manager = MuteManager(bot)
        self.role_persistence = RolePersistenceManager(bot)


def setup(bot):
    """Setup function called by main.py"""

    def _mod_log(bot):
        """Return the logger's EventLogger if available, else None."""
        return getattr(bot, '_logger_event_logger', None)

    # Initialize managers
    moderation_manager = ModerationManager(bot)
    
    # Make manager accessible
    bot.moderation_manager = moderation_manager
    
    # ==================== SLASH COMMANDS ====================
    
    @bot.tree.command(name="ban", description="Ban a user from the server")
    @app_commands.describe(
        user="User to ban",
        reason="Reason for ban (minimum 10 characters)",
        delete_days="Days of messages to delete (0-7)"
    )
    @app_commands.default_permissions(ban_members=True)
    async def ban(interaction: discord.Interaction, user: discord.User, reason: str, delete_days: Optional[int] = 1):
        """Ban a user"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        # Validate reason
        valid, error_msg = await validate_reason(reason)
        if not valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Self-check
        if user == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        # Bot check
        if user == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        # Role hierarchy
        member = interaction.guild.get_member(user.id)
        if member:
            if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
                await interaction.response.send_message(ERROR_HIGHER_ROLE, ephemeral=True)
                return
        
        delete_days = max(0, min(7, delete_days))
        
        try:
            # Send DM with appeal button BEFORE banning
            try:
                dm_embed = discord.Embed(
                    title="🔨 You have been banned",
                    description=f"You have been banned from **{interaction.guild.name}**",
                    color=0x992d22,
                    timestamp=datetime.utcnow()
                )
                dm_embed.add_field(name="Reason", value=reason, inline=False)
                dm_embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                dm_embed.add_field(
                    name="📝 Appeal Process",
                    value="If you believe this ban was unjustified, you can submit an appeal using the button below.",
                    inline=False
                )
                dm_embed.set_footer(text="Appeals are reviewed by server staff")
                
                appeal_view = BanAppealView(interaction.guild.id)
                await user.send(embed=dm_embed, view=appeal_view)
            except discord.Forbidden:
                pass
            
            # Perform ban
            await interaction.guild.ban(user, reason=f"{reason} - By {interaction.user}", delete_message_days=delete_days)
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="✅ User Banned",
                description=f"{user.mention} has been banned from the server.",
                color=0x992d22,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            inchat_embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_ban(
                interaction.guild, user, interaction.user, reason, delete_days, interaction.channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='ban',
                user_id=user.id,
                user=str(user),
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id,
                additional={'delete_days': delete_days}
            )
            
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
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You don't have permission to unban members.", ephemeral=True)
            return
        
        try:
            user_id_int = int(user_id)
            user = await bot.fetch_user(user_id_int)
            
            await interaction.guild.unban(user, reason=f"{reason} - By {interaction.user}")
            
            embed = discord.Embed(
                title="✅ User Unbanned",
                description=f"{user.mention} has been unbanned from the server.",
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
        reason="Reason for kick (minimum 10 characters)"
    )
    @app_commands.default_permissions(kick_members=True)
    async def kick(interaction: discord.Interaction, member: discord.Member, reason: str):
        """Kick a member"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        # Validate reason
        valid, error_msg = await validate_reason(reason)
        if not valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Self-check
        if member == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        # Bot check
        if member == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        # Role hierarchy
        if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            await interaction.response.send_message(ERROR_HIGHER_ROLE, ephemeral=True)
            return
        
        try:
            # Send DM before kicking
            try:
                dm_embed = discord.Embed(
                    title="👢 You have been kicked",
                    description=f"You have been kicked from **{interaction.guild.name}**",
                    color=0xe67e22,
                    timestamp=datetime.utcnow()
                )
                dm_embed.add_field(name="Reason", value=reason, inline=False)
                dm_embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                dm_embed.set_footer(text="You can rejoin if you have an invite link")
                await member.send(embed=dm_embed)
            except discord.Forbidden:
                pass
            
            # Perform kick
            await member.kick(reason=f"{reason} - By {interaction.user}")
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="✅ Member Kicked",
                description=f"{member.mention} has been kicked from the server.",
                color=0xe67e22,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_kick(
                interaction.guild, member, interaction.user, reason, interaction.channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='kick',
                user_id=member.id,
                user=str(member),
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} kicked {member}")
            
        except Exception as e:
            try:
                await interaction.response.send_message("❌ An error occurred while trying to kick the member.", ephemeral=True)
            except:
                pass
            bot.logger.error(MODULE_NAME, "Kick command failed", e)
    
    @bot.tree.command(name="timeout", description="Timeout a member")
    @app_commands.describe(
        member="Member to timeout",
        duration="Duration in minutes",
        reason="Reason for timeout (minimum 10 characters)"
    )
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str):
        """Timeout a member"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        # Validate reason
        valid, error_msg = await validate_reason(reason)
        if not valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Self-check
        if member == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        # Bot check
        if member == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        if duration < 1 or duration > 40320:
            await interaction.response.send_message("❌ Duration must be between 1 minute and 28 days (40320 minutes).", ephemeral=True)
            return
        
        try:
            timeout_until = datetime.utcnow() + timedelta(minutes=duration)
            await member.timeout(timeout_until, reason=f"{reason} - By {interaction.user}")
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="✅ Member Timed Out",
                description=f"{member.mention} has been timed out for **{duration}** minutes.",
                color=0xe74c3c,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            inchat_embed.add_field(name="Duration", value=f"{duration} minutes", inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_timeout(
                interaction.guild, member, interaction.user, reason,
                f"{duration} minutes", interaction.channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='timeout',
                user_id=member.id,
                user=str(member),
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id,
                duration=f"{duration} minutes"
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} timed out {member} for {duration} minutes")
            
        except Exception as e:
            try:
                await interaction.response.send_message("❌ An error occurred while trying to timeout the member.", ephemeral=True)
            except:
                pass
            bot.logger.error(MODULE_NAME, "Timeout command failed", e)
    
    @bot.tree.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(interaction: discord.Interaction, member: discord.Member):
        """Remove timeout from a member"""
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ You don't have permission to moderate members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        try:
            await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
            
            embed = discord.Embed(
                title="✅ Timeout Removed",
                description=f"{member.mention}'s timeout has been removed.",
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
        duration="Duration (e.g., 10m, 1h, 1d) - leave empty for permanent"
    )
    @app_commands.default_permissions(manage_roles=True)
    async def mute(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", duration: Optional[str] = None):
        """Mute a member"""
        
        # Check Discord permission OR elevated role
        if not interaction.user.guild_permissions.manage_roles and not has_elevated_role(interaction.user):
            await interaction.response.send_message("❌ You don't have permission to mute members.", ephemeral=True)
            return
        
        # Self-check
        if member == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        # Bot check
        if member == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        # Parse duration
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
            # Get or create muted role
            muted_role_name = CONFIG.get("moderation", {}).get("muted_role_name", "Muted")
            muted_role = discord.utils.get(interaction.guild.roles, name=muted_role_name)
            
            if not muted_role:
                muted_role = await interaction.guild.create_role(
                    name=muted_role_name,
                    color=discord.Color.dark_gray(),
                    reason="Creating Muted role for moderation"
                )
                
                for channel in interaction.guild.channels:
                    try:
                        await channel.set_permissions(muted_role, send_messages=False, speak=False)
                    except Exception as e:
                        bot.logger.error(MODULE_NAME, f"Failed to set mute permissions in channel {channel.name}", e)
            
            await member.add_roles(muted_role, reason=reason)
            moderation_manager.mute_manager.add_mute(interaction.guild.id, member.id, reason, interaction.user, duration_seconds)
            
            # Send DM
            try:
                dm_embed = discord.Embed(
                    title="🔇 You Have Been Muted",
                    description=f"You have been muted in **{interaction.guild.name}**.",
                    color=0xf39c12,
                    timestamp=datetime.utcnow()
                )
                dm_embed.add_field(name="Reason", value=reason, inline=False)
                dm_embed.add_field(name="Duration", value=duration_str, inline=True)
                dm_embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                await member.send(embed=dm_embed)
            except discord.Forbidden:
                pass
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="✅ Member Muted",
                description=f"{member.mention} has been muted.",
                color=0xf39c12,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Duration", value=duration_str, inline=True)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_mute(
                interaction.guild, member, interaction.user, reason, duration_str, interaction.channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='mute',
                user_id=member.id,
                user=str(member),
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id,
                duration=duration_str
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} muted {member} for {duration_str}")
            
            # Unmute is now handled by the background task check_expired_mutes
            # which persists across restarts
            
        except discord.Forbidden:
            try:
                await interaction.response.send_message("❌ I don't have permission to mute this member.", ephemeral=True)
            except:
                pass
        except Exception as e:
            try:
                await interaction.response.send_message("❌ An error occurred while trying to mute the member.", ephemeral=True)
            except:
                pass
            bot.logger.error(MODULE_NAME, "Mute command failed", e)
    
    @bot.tree.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Member to unmute")
    @app_commands.default_permissions(manage_roles=True)
    async def unmute(interaction: discord.Interaction, member: discord.Member):
        """Unmute a member"""
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You don't have permission to manage roles.", ephemeral=True)
            return
        
        try:
            muted_role_name = CONFIG.get("moderation", {}).get("muted_role_name", "Muted")
            mute_role = discord.utils.get(interaction.guild.roles, name=muted_role_name)
            if not mute_role or mute_role not in member.roles:
                await interaction.response.send_message("❌ This member is not muted.", ephemeral=True)
                return
            
            await member.remove_roles(mute_role, reason=f"Unmuted by {interaction.user}")
            moderation_manager.mute_manager.remove_mute(interaction.guild.id, member.id)
            
            embed = discord.Embed(
                title="✅ Member Unmuted",
                description=f"{member.mention} has been unmuted.",
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
        reason="Reason for softban (minimum 10 characters)",
        delete_days="Days of messages to delete (0-7)"
    )
    @app_commands.default_permissions(ban_members=True)
    async def softban(interaction: discord.Interaction, member: discord.Member, reason: str, delete_days: Optional[int] = 1):
        """Softban a member"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        # Validate reason
        valid, error_msg = await validate_reason(reason)
        if not valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Self-check
        if member == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        # Bot check
        if member == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        delete_days = max(0, min(7, delete_days))
        
        try:
            # Ban then unban
            await member.ban(reason=f"Softban: {reason} - By {interaction.user}", delete_message_days=delete_days)
            await interaction.guild.unban(member, reason=f"Softban unban - By {interaction.user}")
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="✅ Member Softbanned",
                description=f"{member.mention} has been softbanned (messages deleted, can rejoin).",
                color=0x992d22,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            inchat_embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_softban(
                interaction.guild, member, interaction.user, reason, delete_days, interaction.channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='softban',
                user_id=member.id,
                user=str(member),
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id,
                additional={'delete_days': delete_days}
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} softbanned {member}")
            
        except Exception as e:
            try:
                await interaction.response.send_message("❌ An error occurred while trying to softban the member.", ephemeral=True)
            except:
                pass
            bot.logger.error(MODULE_NAME, "Softban command failed", e)
    
    @bot.tree.command(name="purge", description="Delete multiple messages")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        user="Optional: Only delete messages from this user"
    )
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
        """Purge messages"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        if amount < 1 or amount > 100:
            await interaction.response.send_message("❌ Amount must be between 1 and 100.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        try:
            def check(m):
                if user:
                    return m.author.id == user.id
                return True
            
            deleted = await interaction.channel.purge(limit=amount, check=check)
            
            # Build reason
            reason = f"Purged {len(deleted)} message(s)" + (f" from {user}" if user else "")
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="✅ Messages Purged",
                description=f"Deleted **{len(deleted)}** messages{f' from {user.mention}' if user else ''}.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            inchat_embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
            
            await interaction.followup.send(embed=inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_purge(
                interaction.guild, interaction.user, len(deleted), interaction.channel, user
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='purge',
                user_id=user.id if user else None,
                user=str(user) if user else None,
                reason=reason,
                inchat_msg_id=None,
                botlog_msg_id=botlog_msg_id,
                additional={'amount': len(deleted)}
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} purged {len(deleted)} messages in {interaction.channel}")
            
        except Exception as e:
            await interaction.followup.send("❌ An error occurred while trying to purge messages.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Purge command failed", e)
    
    @bot.tree.command(name="slowmode", description="Set channel slowmode")
    @app_commands.describe(
        seconds="Slowmode delay in seconds (0 to disable)",
        channel="Channel to apply slowmode to (default: current)"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(interaction: discord.Interaction, seconds: int, channel: Optional[discord.TextChannel] = None):
        """Set slowmode"""
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ You don't have permission to manage channels.", ephemeral=True)
            return
        
        target_channel = channel or interaction.channel
        
        if seconds < 0 or seconds > 21600:
            await interaction.response.send_message("❌ Slowmode must be between 0 and 21600 seconds (6 hours).", ephemeral=True)
            return
        
        try:
            await target_channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
            
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
    
    @bot.tree.command(name="warn", description="Warn a member")
    @app_commands.describe(
        member="Member to warn",
        reason="Reason for warning (minimum 10 characters)"
    )
    @app_commands.default_permissions(manage_messages=True)
    async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
        """Warn a member"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        # Validate reason
        valid, error_msg = await validate_reason(reason)
        if not valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Self-check
        if member == interaction.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return
        
        # Bot check
        if member == bot.user:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_BOT, ephemeral=True)
            return
        
        try:
            # Add strike
            strike_count = moderation_manager.strike_system.add_strike(member.id, reason)
            
            # Send DM
            try:
                dm_embed = discord.Embed(
                    title="⚠️ Warning",
                    description=f"You have been warned in **{interaction.guild.name}**",
                    color=0xf39c12,
                    timestamp=datetime.utcnow()
                )
                dm_embed.add_field(name="Reason", value=reason, inline=False)
                dm_embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
                dm_embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
                await member.send(embed=dm_embed)
            except discord.Forbidden:
                pass
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="⚠️ Member Warned",
                description=f"{member.mention} has been warned.",
                color=0xf39c12,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            inchat_embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_warn(
                interaction.guild, member, interaction.user, reason, strike_count, interaction.channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='warn',
                user_id=member.id,
                user=str(member),
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id,
                additional={'strike_count': strike_count}
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} warned {member} (Total warnings: {strike_count})")
            
        except Exception as e:
            try:
                await interaction.response.send_message("❌ An error occurred while trying to warn the member.", ephemeral=True)
            except:
                pass
            bot.logger.error(MODULE_NAME, "Warn command failed", e)
    
    @bot.tree.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check warnings for")
    @app_commands.default_permissions(manage_messages=True)
    async def warnings(interaction: discord.Interaction, member: discord.Member):
        """View member warnings"""
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You don't have permission to view warnings.", ephemeral=True)
            return
        
        strikes = moderation_manager.strike_system.get_strike_details(member.id)
        
        if not strikes:
            await interaction.response.send_message(f"✅ **{member}** has no warnings.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"⚠️ Warnings for {member}",
            description=f"Total warnings: **{len(strikes)}**",
            color=0xf39c12,
            timestamp=datetime.utcnow()
        )
        
        for i, strike in enumerate(strikes[-10:], 1):
            timestamp = datetime.fromisoformat(strike['timestamp']).strftime("%Y-%m-%d %H:%M UTC")
            embed.add_field(
                name=f"Warning {i}",
                value=f"**Reason:** {strike['reason']}\n**Date:** {timestamp}",
                inline=False
            )
        
        if len(strikes) > 10:
            embed.set_footer(text=f"Showing last 10 of {len(strikes)} warnings")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="clearwarnings", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.default_permissions(administrator=True)
    async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
        """Clear member warnings"""
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission to clear warnings.", ephemeral=True)
            return
        
        success = moderation_manager.strike_system.clear_strikes(member.id)
        
        if success:
            embed = discord.Embed(
                title="✅ Warnings Cleared",
                description=f"All warnings have been cleared for **{member}**.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} cleared warnings for {member}")
        else:
            await interaction.response.send_message(f"**{member}** has no warnings to clear.", ephemeral=True)
    
    @bot.tree.command(name="lock", description="Lock a channel to prevent members from sending messages")
    @app_commands.describe(
        channel="Channel to lock (default: current)",
        reason="Reason for locking (minimum 10 characters)"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def lock(interaction: discord.Interaction, reason: str, channel: Optional[discord.TextChannel] = None):
        """Lock a channel"""
        
        # Check for elevated role
        if not has_elevated_role(interaction.user):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        
        # Validate reason
        valid, error_msg = await validate_reason(reason)
        if not valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        target_channel = channel or interaction.channel
        
        try:
            # Lock channel by denying send_messages for @everyone
            await target_channel.set_permissions(
                interaction.guild.default_role,
                send_messages=False,
                reason=f"{reason} - By {interaction.user}"
            )
            
            # In-chat embed
            inchat_embed = discord.Embed(
                title="🔒 Channel Locked",
                description=f"{target_channel.mention} has been locked.",
                color=0xe74c3c,
                timestamp=datetime.utcnow()
            )
            inchat_embed.add_field(name="Reason", value=reason, inline=False)
            inchat_embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            inchat_msg_id = await send_tracked_response(interaction, inchat_embed)
            
            botlog_msg_id = await _mod_log(bot).log_lock(
                interaction.guild, interaction.user, reason, target_channel
            ) if _mod_log(bot) else None
            
            # Log to oversight
            await log_mod_action_with_tracking(
                bot=bot,
                interaction=interaction,
                action='lock',
                user_id=None,
                user=None,
                reason=reason,
                inchat_msg_id=inchat_msg_id,
                botlog_msg_id=botlog_msg_id,
                additional={'channel': target_channel.id}
            )
            
            bot.logger.log(MODULE_NAME, f"{interaction.user} locked {target_channel.name}")
            
        except Exception as e:
            try:
                await interaction.response.send_message("❌ An error occurred while trying to lock the channel.", ephemeral=True)
            except:
                pass
            bot.logger.error(MODULE_NAME, "Lock command failed", e)
    
    @bot.tree.command(name="unlock", description="Unlock a channel to allow members to send messages")
    @app_commands.describe(
        channel="Channel to unlock (default: current)"
    )
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel"""
        
        # Check for Discord permission
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ You don't have permission to manage channels.", ephemeral=True)
            return
        
        target_channel = channel or interaction.channel
        
        try:
            # Unlock channel by resetting send_messages for @everyone
            await target_channel.set_permissions(
                interaction.guild.default_role,
                send_messages=None,
                reason=f"Unlocked by {interaction.user}"
            )
            
            embed = discord.Embed(
                title="🔓 Channel Unlocked",
                description=f"{target_channel.mention} has been unlocked.",
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} unlocked {target_channel.name}")
            
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to unlock the channel.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Unlock command failed", e)
    
    # ==================== AUTO-MOD MESSAGE SCANNING ====================
    
    @bot.listen()
    async def on_message(message):
        """Auto-mod message scanning"""
        if message.author.bot or not message.guild:
            return
        
        content_lower = message.content.lower()
        
        # Check for child safety violations (instant ban)
        for word in CHILD_SAFETY:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    await message.guild.ban(message.author, reason=f"Auto-ban: Child safety violation - '{word}'")
                    
                    bot.logger.log(MODULE_NAME, f"AUTO-BAN: {message.author} for child safety violation", "WARNING")
                    if _mod_log(bot):
                        await _mod_log(bot).log_autoban(
                            message.guild, message.author,
                            "Child safety violation", message.channel
                        )
                    
                    return
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-ban failed", e)
                    return
        
        # Check for racial slurs (two-strike system)
        for word in RACIAL_SLURS:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    
                    strike_count = moderation_manager.strike_system.add_strike(message.author.id, f"Racial slur used: '{word}'")
                    
                    if strike_count >= 2:
                        # Ban on second strike
                        await message.guild.ban(message.author, reason=f"Auto-ban: Repeated racial slurs (2 strikes)")
                        
                        bot.logger.log(MODULE_NAME, f"AUTO-BAN: {message.author} for repeated racial slurs (2 strikes)", "WARNING")
                        if _mod_log(bot):
                            await _mod_log(bot).log_autoban_strike(
                                message.guild, message.author,
                                strike_count, "Repeated use of racial slurs", message.channel
                            )
                    else:
                        # First strike - warning
                        try:
                            embed = discord.Embed(
                                title="⚠️ Warning - Strike 1/2",
                                description="Your message was deleted for containing inappropriate language.",
                                color=0xf39c12
                            )
                            embed.add_field(name="Action", value="This is your first strike. A second strike will result in an automatic ban.", inline=False)
                            await message.author.send(embed=embed)
                        except:
                            pass
                        
                        bot.logger.log(MODULE_NAME, f"STRIKE 1: {message.author} for racial slur", "WARNING")
                    
                    return
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-mod racial slur handling failed", e)
                    return
        
        # Check for banned words (simple deletion)
        for word in BANNED_WORDS:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    
                    bot.logger.log(MODULE_NAME, f"Deleted message from {message.author} for banned word: '{word}'")
                    
                    return
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Message deletion failed", e)
                    return
    
    # ==================== MEMBER EVENTS ====================
    
    @bot.listen()
    async def on_member_remove(member):
        """Save roles when member leaves"""
        moderation_manager.role_persistence.save_member_roles(member)
    
    @bot.listen()
    async def on_member_join(member):
        """Restore roles when member rejoins"""
        await moderation_manager.role_persistence.restore_member_roles(member)
    
    # ==================== MUTE TIMER BACKGROUND TASK ====================
    
    @tasks.loop(minutes=1)
    async def check_expired_mutes():
        """Background task to check for expired mutes and remove them"""
        try:
            expired_mutes = moderation_manager.mute_manager.get_expired_mutes()
            
            for mute in expired_mutes:
                guild = bot.get_guild(mute['guild_id'])
                if not guild:
                    continue
                
                member = guild.get_member(mute['user_id'])
                if not member:
                    # User left server, just remove from records
                    moderation_manager.mute_manager.remove_mute(mute['guild_id'], mute['user_id'])
                    continue
                
                # Get muted role
                muted_role_name = CONFIG.get("moderation", {}).get("muted_role_name", "Muted")
                muted_role = discord.utils.get(guild.roles, name=muted_role_name)
                
                if muted_role and muted_role in member.roles:
                    try:
                        await member.remove_roles(muted_role, reason="Mute duration expired")
                        bot.logger.log(MODULE_NAME, f"Auto-unmuted {member} after mute expiry")
                    except Exception as e:
                        bot.logger.error(MODULE_NAME, f"Failed to auto-unmute {member}", e)
                
                # Remove from records
                moderation_manager.mute_manager.remove_mute(mute['guild_id'], mute['user_id'])
        
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Error in mute expiry checker", e)
    
    @check_expired_mutes.before_loop
    async def before_check_expired_mutes():
        """Wait for bot to be ready before starting mute checker"""
        await bot.wait_until_ready()
    
    # Start the mute checker
    check_expired_mutes.start()
    bot.logger.log(MODULE_NAME, "Started background mute expiry checker")
    
    bot.logger.log(MODULE_NAME, "Moderation module setup complete with oversight integration")