# [file name]: moderation.py
import discord
from discord import app_commands
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
TOS_VIOLATIONS = ["deepfakes", "deep fakes", "deepfake", "deep fake"]  # Severe
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


class ModerationManager:
    """Main moderation manager handling auto-mod and commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.strike_system = StrikeSystem(bot)
        self.role_persistence = RolePersistenceManager(bot)
        self.banned_patterns = self.compile_patterns()
        
    def compile_patterns(self):
        """Compile regex patterns for banned words - FIXED to match whole words only"""
        patterns = {}
        
        for category, words in [
            ('child_safety', CHILD_SAFETY),
            ('racial_slur', RACIAL_SLURS),
            ('tos_violation', TOS_VIOLATIONS),
            ('banned_word', BANNED_WORDS)
        ]:
            patterns[category] = []
            for word in words:
                # Use word boundaries to match whole words only
                # For phrases with spaces, we need special handling
                if ' ' in word:
                    # For phrases, escape and use word boundaries around the whole phrase
                    pattern = r'\b' + re.escape(word) + r'\b'
                else:
                    # For single words, use word boundaries
                    pattern = r'\b' + re.escape(word) + r'\b'
                patterns[category].append(re.compile(pattern, re.IGNORECASE))
        
        return patterns
    
    def censor_text(self, text, category):
        """Censor offensive content in logs showing first two letters"""
        if category == 'banned_word':
            # Don't censor regular banned words
            return text
        
        censored = text
        for cat in ['child_safety', 'racial_slur', 'tos_violation']:
            for pattern in self.banned_patterns.get(cat, []):
                def replace_with_partial_stars(match):
                    word = match.group(0)
                    if len(word) <= 2:
                        return '*' * len(word)
                    return word[:2] + '*' * (len(word) - 2)
                censored = pattern.sub(replace_with_partial_stars, censored)
        
        return censored
    
    def get_offense_category(self, text):
        """Determine the category and severity of offense"""
        for category in ['child_safety', 'racial_slur', 'tos_violation', 'banned_word']:
            for pattern in self.banned_patterns[category]:
                if pattern.search(text):
                    return category
        return None
    
    def contains_banned_content(self, text):
        """Check if text contains any banned content"""
        for category in self.banned_patterns.values():
            for pattern in category:
                if pattern.search(text):
                    return True
        return False
    
    def has_elevated_permissions(self, member):
        """Check if member has elevated permissions"""
        return (member.guild_permissions.kick_members or 
                member.guild_permissions.ban_members or 
                member.guild_permissions.manage_messages or
                member.guild_permissions.moderate_members or
                member.guild_permissions.administrator)
    
    def format_category(self, category):
        """Format category name for display"""
        category_names = {
            'child_safety': 'Child Safety Violation',
            'racial_slur': 'Racial Slur',
            'tos_violation': 'Terms of Service Violation',
            'banned_word': 'Banned Content'
        }
        return category_names.get(category, category)
    
    async def log_mod_action(self, action_data):
        """Send moderation logs to bot-logs channel"""
        try:
            channel = None
            for guild in self.bot.guilds:
                channel = discord.utils.get(guild.text_channels, name="bot-logs")
                if channel:
                    break
            
            if not channel:
                return
            
            # Get user object for avatar
            user = action_data.get('user_obj')
            user_display = action_data['user']
            user_id = action_data['user_id']
            
            # Determine embed color and icon
            colors = {
                'ban': 0x992d22,
                'timeout': 0xf04747,
                'kick': 0xff9800,
                'warn': 0xfaa61a,
                'delete': 0x95a5a6,
                'purge': 0x5865f2,
                'clear_strikes': 0x57f287,
                'permission_failed': 0xfee75c
            }
            
            color = colors.get(action_data.get('action'), 0x95a5a6)
            
            embed = discord.Embed(color=color, timestamp=datetime.utcnow())
            
            # Set author with user avatar
            if user:
                embed.set_author(
                    name=f"{user_display}",
                    icon_url=user.display_avatar.url
                )
            else:
                embed.set_author(name=f"{user_display}")
            
            # Add action field with icon
            action_icons = {
                'ban': '🔨',
                'timeout': '⏰',
                'kick': '👢',
                'warn': '⚠️',
                'delete': '🗑️',
                'purge': '🧹',
                'clear_strikes': '✅',
                'permission_failed': '⚠️'
            }
            
            action_name = action_data['action'].replace('_', ' ').title()
            action_icon = action_icons.get(action_data['action'], '📋')
            
            # Build description
            description_parts = [f"{action_icon} **{action_name}** | {user_display}"]
            
            if action_data.get('action') == 'permission_failed':
                description_parts.append(f"\n⚠️ **Failed to execute action - Missing Permissions**")
            
            embed.description = '\n'.join(description_parts)
            
            # Add fields
            if 'moderator' in action_data:
                embed.add_field(name="Moderator", value=action_data['moderator'], inline=True)
            
            if 'reason' in action_data:
                reason = action_data['reason']
                if len(reason) > 1024:
                    reason = reason[:1021] + "..."
                embed.add_field(name="Reason", value=reason, inline=False)
            
            if 'duration' in action_data:
                embed.add_field(name="Duration", value=action_data['duration'], inline=True)
            
            if 'strikes' in action_data:
                embed.add_field(name="Strikes", value=action_data['strikes'], inline=True)
            
            if 'category' in action_data:
                category_display = {
                    'child_safety': '🔴 Child Safety',
                    'racial_slur': '🔴 Racial Slur',
                    'tos_violation': '🔴 TOS Violation',
                    'banned_word': '⚪ Banned Content'
                }
                embed.add_field(
                    name="Category", 
                    value=category_display.get(action_data['category'], action_data['category']),
                    inline=True
                )
            
            if 'message_content' in action_data:
                censored = self.censor_text(action_data['message_content'], action_data.get('category', 'banned_word'))
                if len(censored) > 1000:
                    censored = censored[:1000] + "..."
                embed.add_field(name="Message Content", value=f"```{censored}```", inline=False)
            
            if action_data.get('action') == 'purge':
                if 'messages_deleted' in action_data:
                    embed.add_field(name="Messages Deleted", value=str(action_data['messages_deleted']), inline=True)
                if 'channel' in action_data:
                    embed.add_field(name="Channel", value=action_data['channel'], inline=True)
                if 'target_user' in action_data:
                    embed.add_field(name="Target User", value=action_data['target_user'], inline=True)
            
            # Footer with user ID
            embed.set_footer(text=f"User ID: {user_id}")
            
            await channel.send(embed=embed)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send mod log", e)
    
    async def handle_auto_mod_violation(self, message):
        """Handle auto-mod violations with category-based enforcement"""
        if message.author.bot or not message.guild:
            return
        
        if not self.contains_banned_content(message.content):
            return
        
        offense_category = self.get_offense_category(message.content)
        
        if not offense_category:
            return
        
        self.bot.logger.log(MODULE_NAME, 
            f"Auto-mod violation ({offense_category}) from {message.author} ({message.author.id})")
        
        # Check if user has Moderator role and used racial slur
        moderator_role = discord.utils.get(message.guild.roles, name="Moderator")
        moderator_role_removed = False
        
        if moderator_role and moderator_role in message.author.roles and offense_category == 'racial_slur':
            try:
                await message.author.remove_roles(moderator_role, reason="Auto-mod: Racial slur violation by moderator")
                moderator_role_removed = True
                self.bot.logger.log(MODULE_NAME, f"Removed Moderator role from {message.author} for racial slur")
            except discord.Forbidden:
                self.bot.logger.error(MODULE_NAME, f"No permission to remove Moderator role from {message.author}")
        
        try:
            # Always delete the message
            await message.delete()
            
            # Handle based on severity
            if offense_category == 'banned_word':
                # Just delete, no strikes, no DM
                action_data = {
                    'action': 'delete',
                    'user': str(message.author),
                    'user_id': message.author.id,
                    'user_obj': message.author,
                    'moderator': 'Auto-Mod',
                    'reason': 'Use of banned content',
                    'category': offense_category,
                    'message_content': message.content
                }
                await self.log_mod_action(action_data)
                
                # NO DM for banned words - they're not serious violations
                return
            
            # For serious violations: child safety, racial slurs, TOS violations
            strike_count = self.strike_system.add_strike(message.author.id, f"Auto-mod: {offense_category}")
            
            if strike_count == 1:
                # First strike: 1 day timeout (24 hours)
                timeout_duration = timedelta(days=1)
                until = discord.utils.utcnow() + timeout_duration
                
                try:
                    await message.author.timeout(until, reason=f"Auto-mod: First strike - {offense_category}")
                    
                    # Send DM for serious violations only
                    try:
                        embed = discord.Embed(
                            title="⚠️ First Strike - Timeout",
                            description="You have been timed out for violating server rules.",
                            color=0xf04747,
                            timestamp=datetime.utcnow()
                        )
                        embed.add_field(name="Action", value="⏰ Timeout (1 day)", inline=True)
                        embed.add_field(name="Server", value=message.guild.name, inline=True)
                        
                        category_names = {
                            'child_safety': 'Child Safety Violation',
                            'racial_slur': 'Racial Slur',
                            'tos_violation': 'Terms of Service Violation'
                        }
                        embed.add_field(
                            name="Violation",
                            value=f"🔴 {category_names.get(offense_category, offense_category)}",
                            inline=False
                        )
                        
                        embed.add_field(name="Strike Count", value="⚡ 1/2", inline=True)
                        embed.add_field(name="Next Offense", value="❌ Permanent ban", inline=True)
                        
                        if moderator_role_removed:
                            embed.add_field(
                                name="🔴 Role Removed",
                                value="Your **Moderator** role has been removed due to using a racial slur. This is a severe breach of trust and conduct expectations.",
                                inline=False
                            )
                        
                        embed.set_footer(text="Automated Moderation System")
                        
                        await message.author.send(embed=embed)
                    except discord.Forbidden:
                        pass
                    
                    # Log action
                    action_data = {
                        'action': 'timeout',
                        'user': str(message.author),
                        'user_id': message.author.id,
                        'user_obj': message.author,
                        'moderator': 'Auto-Mod',
                        'reason': f'First strike: {self.format_category(offense_category)}' + 
                                 (' - Moderator role removed' if moderator_role_removed else ''),
                        'duration': '1 day',
                        'strikes': f'{strike_count}/2',
                        'category': offense_category,
                        'message_content': message.content
                    }
                    await self.log_mod_action(action_data)
                    
                except discord.Forbidden:
                    # Log permission failure
                    action_data = {
                        'action': 'permission_failed',
                        'user': str(message.author),
                        'user_id': message.author.id,
                        'user_obj': message.author,
                        'moderator': 'Auto-Mod',
                        'reason': f'Failed to timeout user - Missing permissions. First strike: {self.format_category(offense_category)}' + 
                                 (' - Moderator role removed' if moderator_role_removed else ''),
                        'strikes': f'{strike_count}/2',
                        'category': offense_category,
                        'message_content': message.content
                    }
                    await self.log_mod_action(action_data)
                    self.bot.logger.error(MODULE_NAME, f"No permission to timeout {message.author}")
            
            elif strike_count >= 2:
                # Second strike: permanent ban - NO MESSAGE HISTORY DELETION
                has_elevated_perms = self.has_elevated_permissions(message.author)
                
                try:
                    # Send final DM for serious violations
                    try:
                        embed = discord.Embed(
                            title="❌ Second Strike - Permanent Ban",
                            description="You have been permanently banned from the server.",
                            color=0x992d22,
                            timestamp=datetime.utcnow()
                        )
                        embed.add_field(name="Reason", value="Repeated violations of server rules", inline=False)
                        
                        category_names = {
                            'child_safety': 'Child Safety Violation',
                            'racial_slur': 'Racial Slur',
                            'tos_violation': 'Terms of Service Violation'
                        }
                        embed.add_field(
                            name="Final Violation",
                            value=f"🔴 {category_names.get(offense_category, offense_category)}",
                            inline=False
                        )
                        
                        embed.add_field(name="Strike Count", value="⚡ 2/2", inline=True)
                        embed.add_field(name="Server", value=message.guild.name, inline=True)
                        
                        if moderator_role_removed:
                            embed.add_field(
                                name="🔴 Role Removed",
                                value="Your **Moderator** role was removed during your first strike for using a racial slur.",
                                inline=False
                            )
                        
                        embed.set_footer(text="Automated Moderation System")
                        
                        await message.author.send(embed=embed)
                    except discord.Forbidden:
                        pass
                    
                    # Ban user WITHOUT deleting message history
                    await message.author.ban(
                        reason=f"Auto-mod: Second strike - {offense_category} violation",
                        delete_message_days=0  # No message history deletion for slur punishments
                    )
                    
                    # Log action
                    action_data = {
                        'action': 'ban',
                        'user': str(message.author),
                        'user_id': message.author.id,
                        'user_obj': message.author,
                        'moderator': 'Auto-Mod',
                        'reason': f'Second strike: Repeated {offense_category} violations' + 
                                 (' (Staff member)' if has_elevated_perms else '') +
                                 (' - Moderator role was removed on first strike' if moderator_role_removed else ''),
                        'strikes': f'{strike_count}/2',
                        'category': offense_category,
                        'message_content': message.content
                        # Removed message_deletion field since we're not deleting history
                    }
                    await self.log_mod_action(action_data)
                    
                except discord.Forbidden:
                    # Log permission failure
                    action_data = {
                        'action': 'permission_failed',
                        'user': str(message.author),
                        'user_id': message.author.id,
                        'user_obj': message.author,
                        'moderator': 'Auto-Mod',
                        'reason': f'Failed to ban user - Missing permissions. Second strike: {offense_category} violation' + 
                                 (' (Staff member)' if has_elevated_perms else '') +
                                 (' - Moderator role was removed on first strike' if moderator_role_removed else ''),
                        'strikes': f'{strike_count}/2',
                        'category': offense_category,
                        'message_content': message.content
                    }
                    await self.log_mod_action(action_data)
                    self.bot.logger.error(MODULE_NAME, f"No permission to ban {message.author}")
        
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error handling auto-mod violation", e)


def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up moderation module")
    
    moderation_manager = ModerationManager(bot)
    bot.moderation_manager = moderation_manager
    
    @bot.listen('on_message')
    async def moderation_on_message(message):
        """Handle auto-mod message filtering"""
        await moderation_manager.handle_auto_mod_violation(message)
    
    @bot.listen('on_member_remove')
    async def on_member_remove(member):
        """Save member roles when they leave"""
        moderation_manager.role_persistence.save_member_roles(member)
    
    @bot.listen('on_member_join')
    async def on_member_join(member):
        """Restore member roles when they rejoin"""
        # Small delay to ensure member is fully loaded
        await asyncio.sleep(1)
        await moderation_manager.role_persistence.restore_member_roles(member)
    
    @bot.tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(
        member="Member to kick",
        reason="Reason for kicking"
    )
    @app_commands.default_permissions(kick_members=True)
    async def kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
        """Kick command"""
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ You don't have permission to kick members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot kick yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot kick myself.", ephemeral=True)
            return
        
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You cannot kick members with equal or higher roles.", ephemeral=True)
            return
        
        try:
            # Save roles before kicking
            moderation_manager.role_persistence.save_member_roles(member)
            
            await member.kick(reason=reason)
            
            action_data = {
                'action': 'kick',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'reason': reason
            }
            
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Kicked",
                description=f"**{member}** has been kicked from the server.",
                color=0xff9800,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.set_footer(text="Roles will be restored if they rejoin")
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} kicked {member}")
            
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to kick that member.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to kick the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Kick command failed", e)
    
    @bot.tree.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="Member to ban",
        reason="Reason for banning",
        delete_message_days="Number of days of message history to delete (0-7)"
    )
    @app_commands.default_permissions(ban_members=True)
    async def ban(interaction: discord.Interaction, member: discord.Member, 
                 reason: Optional[str] = "No reason provided", 
                 delete_message_days: Optional[int] = 0):  # Changed default to 0
        """Ban command"""
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You don't have permission to ban members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot ban yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot ban myself.", ephemeral=True)
            return
        
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You cannot ban members with equal or higher roles.", ephemeral=True)
            return
        
        delete_days = max(0, min(7, delete_message_days or 0))  # Changed default to 0
        
        try:
            # Save roles before banning
            moderation_manager.role_persistence.save_member_roles(member)
            
            await member.ban(reason=reason, delete_message_days=delete_days)
            
            action_data = {
                'action': 'ban',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'reason': reason
            }
            
            # Only add message deletion field if actually deleting messages
            if delete_days > 0:
                action_data['message_deletion'] = f'{delete_days} days'
            
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Banned",
                description=f"**{member}** has been banned from the server.",
                color=0x992d22,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            if delete_days > 0:
                embed.add_field(name="Message Deletion", value=f"{delete_days} days", inline=True)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.set_footer(text="Roles will be restored if ban is lifted and they rejoin")
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} banned {member}")
            
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban that member.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to ban the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Ban command failed", e)
    
    @bot.tree.command(name="timeout", description="Timeout a member")
    @app_commands.describe(
        member="Member to timeout",
        duration_minutes="Duration in minutes (default: 60)",
        reason="Reason for timeout"
    )
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(interaction: discord.Interaction, member: discord.Member,
                     duration_minutes: Optional[int] = 60,
                     reason: Optional[str] = "No reason provided"):
        """Timeout command"""
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ You don't have permission to timeout members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot timeout yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot timeout myself.", ephemeral=True)
            return
        
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You cannot timeout members with equal or higher roles.", ephemeral=True)
            return
        
        duration_minutes = max(1, min(40320, duration_minutes or 60))
        duration = timedelta(minutes=duration_minutes)
        until = discord.utils.utcnow() + duration
        
        try:
            await member.timeout(until, reason=reason)
            
            action_data = {
                'action': 'timeout',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'reason': reason,
                'duration': f'{duration_minutes} minutes'
            }
            
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Timed Out",
                description=f"**{member}** has been timed out.",
                color=0xf04747,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Duration", value=f"{duration_minutes} minutes", inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} timed out {member} for {duration_minutes} minutes")
            
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to timeout that member.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to timeout the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Timeout command failed", e)
    
    @bot.tree.command(name="mute", description="Mute a member (alias for timeout)")
    @app_commands.describe(
        member="Member to mute",
        duration_minutes="Duration in minutes (default: 60)",
        reason="Reason for mute"
    )
    @app_commands.default_permissions(moderate_members=True)
    async def mute(interaction: discord.Interaction, member: discord.Member,
                   duration_minutes: Optional[int] = 60,
                   reason: Optional[str] = "No reason provided"):
        """Mute command (same as timeout)"""
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ You don't have permission to mute members.", ephemeral=True)
            return
        
        if member == interaction.user:
            await interaction.response.send_message("❌ You cannot mute yourself.", ephemeral=True)
            return
        
        if member == bot.user:
            await interaction.response.send_message("❌ I cannot mute myself.", ephemeral=True)
            return
        
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You cannot mute members with equal or higher roles.", ephemeral=True)
            return
        
        duration_minutes = max(1, min(40320, duration_minutes or 60))
        duration = timedelta(minutes=duration_minutes)
        until = discord.utils.utcnow() + duration
        
        try:
            await member.timeout(until, reason=reason)
            
            action_data = {
                'action': 'timeout',
                'user': str(member),
                'user_id': member.id,
                'user_obj': member,
                'moderator': str(interaction.user),
                'reason': reason,
                'duration': f'{duration_minutes} minutes'
            }
            
            await moderation_manager.log_mod_action(action_data)
            
            embed = discord.Embed(
                title="✅ Member Muted",
                description=f"**{member}** has been muted.",
                color=0xf04747,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Duration", value=f"{duration_minutes} minutes", inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
            await interaction.response.send_message(embed=embed)
            bot.logger.log(MODULE_NAME, f"{interaction.user} muted {member} for {duration_minutes} minutes")
            
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to mute that member.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("❌ An error occurred while trying to mute the member.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Mute command failed", e)
    
    @bot.tree.command(name="purge", description="Delete a number of messages")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        user="Only delete messages from this user (optional)"
    )
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
        """Purge messages command"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You don't have permission to manage messages.", ephemeral=True)
            return
        
        amount = max(1, min(100, amount))
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            def check(msg):
                return user is None or msg.author == user
            
            deleted = await interaction.channel.purge(limit=amount, check=check)
            
            action_data = {
                'action': 'purge',
                'channel': interaction.channel.mention,
                'moderator': str(interaction.user),
                'user': str(interaction.user),
                'user_id': interaction.user.id,
                'user_obj': interaction.user,
                'messages_deleted': len(deleted),
                'target_user': str(user) if user else 'All users',
                'reason': f"Purged {len(deleted)} messages" + (f" from {user}" if user else "")
            }
            
            await moderation_manager.log_mod_action(action_data)
            
            await interaction.followup.send(
                f"✅ Deleted {len(deleted)} messages" + 
                (f" from {user}" if user else ""),
                ephemeral=True
            )
            bot.logger.log(MODULE_NAME, f"{interaction.user} purged {len(deleted)} messages")
            
        except Exception as e:
            await interaction.followup.send("❌ An error occurred while trying to purge messages.", ephemeral=True)
            bot.logger.error(MODULE_NAME, "Purge command failed", e)
    
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
            # Send DM to member
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
        
        # Count saved roles
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
        
        embed.set_footer(text=f"Requested by {interaction.user} | Command prefix: ?")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.logger.log(MODULE_NAME, "Moderation module setup complete")