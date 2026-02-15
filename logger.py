# [file name]: logger.py
import discord
from discord import app_commands
from datetime import datetime
from typing import Optional
import json

MODULE_NAME = "LOGGER"

class EventLogger:
    """Logs all Discord events to designated channels (#join-logs and #bot-logs)"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config_file = "logger_config.json"
        self.config = self.load_config()
        
    def load_config(self):
        """Load logger configuration"""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            # Default configuration
            default_config = {
                "join_logs_channel_id": None,  # Set via command
                "bot_logs_channel_id": None,   # Set via command
                "log_message_edits": True,
                "log_message_deletes": True,
                "log_member_joins": True,
                "log_member_leaves": True,
                "log_bans": True,
                "log_unbans": True,
                "log_role_changes": True,
                "log_channel_changes": True,
                "log_server_changes": True,
                "log_invite_changes": True,
                "log_voice_changes": True,
                "log_nickname_changes": True
            }
            self.save_config(default_config)
            return default_config
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load logger config", e)
            return {}
    
    def save_config(self, config=None):
        """Save logger configuration"""
        if config is None:
            config = self.config
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save logger config", e)
    
    def get_join_logs_channel(self, guild):
        """Get the join logs channel"""
        if not self.config.get("join_logs_channel_id"):
            return None
        return guild.get_channel(self.config["join_logs_channel_id"])
    
    def get_bot_logs_channel(self, guild):
        """Get the bot logs channel"""
        if not self.config.get("bot_logs_channel_id"):
            return None
        return guild.get_channel(self.config["bot_logs_channel_id"])
    
    async def log_to_channel(self, channel, embed):
        """Send log embed to channel"""
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            self.bot.logger.error(MODULE_NAME, f"No permission to send logs to {channel.name}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send log to {channel.name}", e)
    
    # ====================
    # MESSAGE EVENTS
    # ====================
    
    async def log_message_delete(self, message):
        """Log when a message is deleted"""
        if not self.config.get("log_message_deletes"):
            return
        if message.author.bot:
            return
        
        channel = self.get_bot_logs_channel(message.guild)
        if not channel:
            return
        
        # Build description
        description = f"**Message sent by {message.author.mention} deleted in {message.channel.mention}**"
        if message.content:
            description += f"\n{message.content}"
        
        embed = discord.Embed(
            description=description,
            color=0xff4500,
            timestamp=datetime.utcnow()
        )
        
        # Set author (user who sent the message)
        embed.set_author(
            name=str(message.author),
            icon_url=message.author.display_avatar.url
        )
        
        # Footer with IDs
        embed.set_footer(text=f"Author: {message.author.id} | Message ID: {message.id}")
        
        # If there's an image attachment, show it
        if message.attachments:
            image_exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
            for att in message.attachments:
                if att.filename.lower().endswith(image_exts):
                    embed.set_image(url=att.url)
                    break
        
        await self.log_to_channel(channel, embed)
    
    async def log_message_edit(self, before, after):
        """Log when a message is edited"""
        if not self.config.get("log_message_edits"):
            return
        if before.author.bot:
            return
        if before.content == after.content:
            return  # No actual content change
        
        channel = self.get_bot_logs_channel(before.guild)
        if not channel:
            return
        
        # Before content
        before_content = before.content if before.content else "*[No text content]*"
        if len(before_content) > 800:
            before_content = before_content[:797] + "..."
        
        # After content
        after_content = after.content if after.content else "*[No text content]*"
        if len(after_content) > 800:
            after_content = after_content[:797] + "..."
        
        description = (
            f"**Message sent by {after.author.mention} edited in {after.channel.mention}**\n"
            f"**Before**\n{before_content}\n"
            f"**After**\n{after_content}\n"
            f"[Jump to Message]({after.jump_url})"
        )
        
        embed = discord.Embed(
            description=description,
            color=0x3498db,
            timestamp=datetime.utcnow()
        )
        
        embed.set_author(
            name=str(after.author),
            icon_url=after.author.display_avatar.url
        )
        
        embed.set_footer(text=f"User ID: {after.author.id} | Message ID: {after.id}")
        
        await self.log_to_channel(channel, embed)
    
    async def log_bulk_message_delete(self, messages):
        """Log when messages are bulk deleted"""
        if not self.config.get("log_message_deletes"):
            return
        if not messages:
            return
        
        channel = self.get_bot_logs_channel(messages[0].guild)
        if not channel:
            return
        
        description = (
            f"**{len(messages)} messages were deleted in {messages[0].channel.mention}**"
        )
        
        embed = discord.Embed(
            description=description,
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        
        await self.log_to_channel(channel, embed)
    
    # ====================
    # MEMBER EVENTS
    # ====================
    
    async def log_member_join(self, member):
        """Log when a member joins"""
        if not self.config.get("log_member_joins"):
            return
        
        channel = self.get_join_logs_channel(member.guild)
        if not channel:
            return
        
        # Calculate account age
        account_age = datetime.utcnow() - member.created_at
        years = account_age.days // 365
        months = (account_age.days % 365) // 30
        days = (account_age.days % 365) % 30
        
        age_parts = []
        if years > 0:
            age_parts.append(f"{years} year{'s' if years != 1 else ''}")
        if months > 0:
            age_parts.append(f"{months} month{'s' if months != 1 else ''}")
        if days > 0 or not age_parts:
            age_parts.append(f"{days} day{'s' if days != 1 else ''}")
        
        account_age_str = ", ".join(age_parts)
        
        embed = discord.Embed(
            description=f"{member.mention} {member.name}",
            color=0x43b581,
            timestamp=datetime.utcnow()
        )
        
        # Set author
        embed.set_author(
            name="Member Joined",
            icon_url=member.display_avatar.url
        )
        
        # Set thumbnail
        embed.set_thumbnail(url=member.display_avatar.url)
        
        # Add account age field
        embed.add_field(name="Account Age", value=account_age_str, inline=False)
        
        # Footer with ID
        embed.set_footer(text=f"ID: {member.id}")
        
        await self.log_to_channel(channel, embed)
    
    async def log_member_leave(self, member):
        """Log when a member leaves"""
        if not self.config.get("log_member_leaves"):
            return
        
        channel = self.get_join_logs_channel(member.guild)
        if not channel:
            return
        
        embed = discord.Embed(
            description=f"{member.mention} {member.name}",
            color=0xe67e22,
            timestamp=datetime.utcnow()
        )
        
        # Set author
        embed.set_author(
            name="Member Left",
            icon_url=member.display_avatar.url
        )
        
        # Set thumbnail
        embed.set_thumbnail(url=member.display_avatar.url)
        
        # Footer with ID
        embed.set_footer(text=f"ID: {member.id}")
        
        await self.log_to_channel(channel, embed)
    
    async def log_member_ban(self, guild, user):
        """Log when a member is banned"""
        if not self.config.get("log_bans"):
            return
        
        channel = self.get_bot_logs_channel(guild)
        if not channel:
            return
        
        # Try to get ban reason
        try:
            ban_entry = await guild.fetch_ban(user)
            reason = ban_entry.reason or "No reason provided"
        except:
            reason = "Could not fetch ban reason"
        
        description = (
            f"**{user.mention} was banned from the server**\n\n"
            f"**User:** {user.name}\n"
            f"**ID:** {user.id}\n"
            f"**Reason:** {reason}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0x992d22,
            timestamp=datetime.utcnow()
        )
        
        embed.set_author(
            name=str(user),
            icon_url=user.display_avatar.url
        )
        
        embed.set_footer(text=f"User ID: {user.id}")
        
        await self.log_to_channel(channel, embed)
    
    async def log_member_unban(self, guild, user):
        """Log when a member is unbanned"""
        if not self.config.get("log_unbans"):
            return
        
        channel = self.get_bot_logs_channel(guild)
        if not channel:
            return
        
        description = f"**{user.mention} was unbanned from the server**\n\n**User:** {user.name}\n**ID:** {user.id}"
        
        embed = discord.Embed(
            description=description,
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        
        embed.set_author(
            name=str(user),
            icon_url=user.display_avatar.url
        )
        
        embed.set_footer(text=f"User ID: {user.id}")
        
        await self.log_to_channel(channel, embed)
    
    async def log_member_update(self, before, after):
        """Log when a member is updated (roles, nickname)"""
        guild = after.guild
        channel = self.get_bot_logs_channel(guild)
        if not channel:
            return
        
        # Check for role changes
        if self.config.get("log_role_changes") and before.roles != after.roles:
            added_roles = [role for role in after.roles if role not in before.roles]
            removed_roles = [role for role in before.roles if role not in after.roles]
            
            if added_roles or removed_roles:
                description = f"**{after.mention}'s roles were updated**\n\n"
                
                if added_roles:
                    description += f"**Roles Added:** {', '.join([role.mention for role in added_roles])}\n"
                
                if removed_roles:
                    description += f"**Roles Removed:** {', '.join([role.mention for role in removed_roles])}"
                
                embed = discord.Embed(
                    description=description,
                    color=0x9b59b6,
                    timestamp=datetime.utcnow()
                )
                
                embed.set_author(
                    name=str(after),
                    icon_url=after.display_avatar.url
                )
                
                embed.set_footer(text=f"User ID: {after.id}")
                await self.log_to_channel(channel, embed)
        
        # Check for nickname changes
        if self.config.get("log_nickname_changes") and before.nick != after.nick:
            before_nick = before.nick or "*No nickname*"
            after_nick = after.nick or "*No nickname*"
            
            description = (
                f"**{after.mention}'s nickname was changed**\n\n"
                f"**Before:** {before_nick}\n"
                f"**After:** {after_nick}"
            )
            
            embed = discord.Embed(
                description=description,
                color=0x3498db,
                timestamp=datetime.utcnow()
            )
            
            embed.set_author(
                name=str(after),
                icon_url=after.display_avatar.url
            )
            
            embed.set_footer(text=f"User ID: {after.id}")
            await self.log_to_channel(channel, embed)
    
    # ====================
    # ROLE EVENTS
    # ====================
    
    async def log_role_create(self, role):
        """Log when a role is created"""
        if not self.config.get("log_role_changes"):
            return
        
        channel = self.get_bot_logs_channel(role.guild)
        if not channel:
            return
        
        hoisted = "Yes" if role.hoist else "No"
        mentionable = "Yes" if role.mentionable else "No"
        
        description = (
            f"**Role {role.mention} was created**\n\n"
            f"**Name:** {role.name}\n"
            f"**ID:** {role.id}\n"
            f"**Color:** {role.color}\n"
            f"**Hoisted:** {hoisted}\n"
            f"**Mentionable:** {mentionable}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        
        await self.log_to_channel(channel, embed)
    
    async def log_role_delete(self, role):
        """Log when a role is deleted"""
        if not self.config.get("log_role_changes"):
            return
        
        channel = self.get_bot_logs_channel(role.guild)
        if not channel:
            return
        
        description = (
            f"**Role {role.name} was deleted**\n\n"
            f"**Name:** {role.name}\n"
            f"**ID:** {role.id}\n"
            f"**Color:** {role.color}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        
        await self.log_to_channel(channel, embed)
    
    async def log_role_update(self, before, after):
        """Log when a role is updated"""
        if not self.config.get("log_role_changes"):
            return
        
        channel = self.get_bot_logs_channel(after.guild)
        if not channel:
            return
        
        changes = []
        
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        
        if before.color != after.color:
            changes.append(f"**Color:** {before.color} → {after.color}")
        
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
        
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
        
        if before.permissions != after.permissions:
            changes.append("**Permissions:** Updated")
        
        if not changes:
            return
        
        description = f"**Role {after.mention} was updated**\n\n" + "\n".join(changes)
        
        embed = discord.Embed(
            description=description,
            color=0x3498db,
            timestamp=datetime.utcnow()
        )
        
        embed.set_footer(text=f"Role ID: {after.id}")
        
        await self.log_to_channel(channel, embed)
    
    # ====================
    # CHANNEL EVENTS
    # ====================
    
    async def log_channel_create(self, channel):
        """Log when a channel is created"""
        if not self.config.get("log_channel_changes"):
            return
        
        bot_logs = self.get_bot_logs_channel(channel.guild)
        if not bot_logs:
            return
        
        description = (
            f"**Channel {channel.mention} was created**\n\n"
            f"**Name:** {channel.name}\n"
            f"**ID:** {channel.id}\n"
            f"**Type:** {channel.type}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        
        await self.log_to_channel(bot_logs, embed)
    
    async def log_channel_delete(self, channel):
        """Log when a channel is deleted"""
        if not self.config.get("log_channel_changes"):
            return
        
        bot_logs = self.get_bot_logs_channel(channel.guild)
        if not bot_logs:
            return
        
        description = (
            f"**Channel {channel.name} was deleted**\n\n"
            f"**Name:** {channel.name}\n"
            f"**ID:** {channel.id}\n"
            f"**Type:** {channel.type}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        
        await self.log_to_channel(bot_logs, embed)
    
    async def log_channel_update(self, before, after):
        """Log when a channel is updated"""
        if not self.config.get("log_channel_changes"):
            return
        
        channel = self.get_bot_logs_channel(after.guild)
        if not channel:
            return
        
        changes = []
        
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        
        if hasattr(before, 'topic') and hasattr(after, 'topic') and before.topic != after.topic:
            changes.append(f"**Topic:** {before.topic or 'None'} → {after.topic or 'None'}")
        
        if hasattr(before, 'slowmode_delay') and hasattr(after, 'slowmode_delay') and before.slowmode_delay != after.slowmode_delay:
            changes.append(f"**Slowmode:** {before.slowmode_delay}s → {after.slowmode_delay}s")
        
        if not changes:
            return
        
        description = f"**Channel {after.mention} was updated**\n\n" + "\n".join(changes)
        
        embed = discord.Embed(
            description=description,
            color=0x3498db,
            timestamp=datetime.utcnow()
        )
        
        embed.set_footer(text=f"Channel ID: {after.id}")
        
        await self.log_to_channel(channel, embed)
    
    # ====================
    # VOICE EVENTS
    # ====================
    
    async def log_voice_state_update(self, member, before, after):
        """Log voice channel joins/leaves/moves"""
        if not self.config.get("log_voice_changes"):
            return
        
        channel = self.get_bot_logs_channel(member.guild)
        if not channel:
            return
        
        # User joined a voice channel
        if before.channel is None and after.channel is not None:
            description = f"**{member.mention} joined {after.channel.mention}**"
            
            embed = discord.Embed(
                description=description,
                color=0x2ecc71,
                timestamp=datetime.utcnow()
            )
            
            embed.set_author(
                name=str(member),
                icon_url=member.display_avatar.url
            )
            
            embed.set_footer(text=f"User ID: {member.id}")
            await self.log_to_channel(channel, embed)
        
        # User left a voice channel
        elif before.channel is not None and after.channel is None:
            description = f"**{member.mention} left {before.channel.mention}**"
            
            embed = discord.Embed(
                description=description,
                color=0xe74c3c,
                timestamp=datetime.utcnow()
            )
            
            embed.set_author(
                name=str(member),
                icon_url=member.display_avatar.url
            )
            
            embed.set_footer(text=f"User ID: {member.id}")
            await self.log_to_channel(channel, embed)
        
        # User moved between voice channels
        elif before.channel != after.channel:
            description = f"**{member.mention} moved from {before.channel.mention} to {after.channel.mention}**"
            
            embed = discord.Embed(
                description=description,
                color=0x3498db,
                timestamp=datetime.utcnow()
            )
            
            embed.set_author(
                name=str(member),
                icon_url=member.display_avatar.url
            )
            
            embed.set_footer(text=f"User ID: {member.id}")
            await self.log_to_channel(channel, embed)
    
    # ====================
    # MODERATION ACTIONS (from moderation.py)
    # ====================
    
    async def log_moderation_action(self, guild, action_type, moderator, target_user, reason, **kwargs):
        """Log moderation actions to bot logs channel"""
        channel = self.get_bot_logs_channel(guild)
        if not channel:
            return
        
        # Action type mapping
        action_names = {
            'ban': 'Member Banned',
            'unban': 'Member Unbanned',
            'kick': 'Member Kicked',
            'mute': 'Member Muted',
            'unmute': 'Member Unmuted',
            'warn': 'Member Warned',
            'timeout': 'Member Timed Out',
            'untimeout': 'Timeout Removed',
            'softban': 'Member Softbanned',
            'lock': 'Channel Locked',
            'unlock': 'Channel Unlocked',
            'slowmode': 'Slowmode Set',
            'purge': 'Messages Purged',
            'clear_strikes': 'Strikes Cleared'
        }
        
        action_colors = {
            'ban': 0x992d22,
            'unban': 0x2ecc71,
            'kick': 0xe67e22,
            'mute': 0xf39c12,
            'unmute': 0x2ecc71,
            'warn': 0xfaa61a,
            'timeout': 0xe74c3c,
            'untimeout': 0x2ecc71,
            'softban': 0x992d22,
            'lock': 0x95a5a6,
            'unlock': 0x2ecc71,
            'slowmode': 0x3498db,
            'purge': 0xe74c3c,
            'clear_strikes': 0x3498db
        }
        
        # Build description with target user
        description = f"**User:** {target_user.mention}\n**Moderator:** {moderator.mention}\n**Reason:** {reason}"
        
        # Add extra info
        if 'duration' in kwargs:
            description += f"\n**Duration:** {kwargs['duration']}"
        if 'channel' in kwargs:
            description += f"\n**Channel:** {kwargs['channel']}"
        if 'amount' in kwargs:
            description += f"\n**Amount:** {kwargs['amount']}"
        
        embed = discord.Embed(
            description=description,
            color=action_colors.get(action_type, 0x5865f2),
            timestamp=datetime.utcnow()
        )
        
        # Set author to show action type
        embed.set_author(name=action_names.get(action_type, 'Moderation Action'))
        
        if target_user:
            embed.set_footer(text=f"User ID: {target_user.id} | Moderator ID: {moderator.id}")
        else:
            embed.set_footer(text=f"Moderator ID: {moderator.id}")
        
        await self.log_to_channel(channel, embed)
    
    # ====================
    # INVITE EVENTS
    # ====================
    
    async def log_invite_create(self, invite):
        """Log when an invite is created"""
        if not self.config.get("log_invite_changes"):
            return
        
        channel = self.get_bot_logs_channel(invite.guild)
        if not channel:
            return
        
        # Build description
        max_uses = "Unlimited" if not invite.max_uses else str(invite.max_uses)
        
        if invite.max_age:
            hours = invite.max_age // 3600
            expires = f"{hours} hours"
        else:
            expires = "Never"
        
        description = (
            f"**Invite Code:** {invite.code}\n"
            f"**Channel:** {invite.channel.mention}\n"
            f"**Max Uses:** {max_uses}\n"
            f"**Expires:** {expires}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        
        # Set author (user who created invite)
        if invite.inviter:
            embed.set_author(
                name=str(invite.inviter),
                icon_url=invite.inviter.display_avatar.url
            )
            embed.set_footer(text=f"Inviter ID: {invite.inviter.id} | Code: {invite.code}")
        else:
            embed.set_author(name="Invite Created")
            embed.set_footer(text=f"Code: {invite.code}")
        
        await self.log_to_channel(channel, embed)
        await self.log_to_channel(channel, embed)
    
    async def log_invite_delete(self, invite):
        """Log when an invite is deleted"""
        if not self.config.get("log_invite_changes"):
            return
        
        channel = self.get_bot_logs_channel(invite.guild)
        if not channel:
            return
        
        channel_name = invite.channel.mention if invite.channel else "Unknown"
        
        description = (
            f"**Invite {invite.code} was deleted**\n\n"
            f"**Code:** {invite.code}\n"
            f"**Channel:** {channel_name}"
        )
        
        embed = discord.Embed(
            description=description,
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        
        await self.log_to_channel(channel, embed)


def setup(bot):
    """Setup function called by main.py"""
    
    event_logger = EventLogger(bot)
    
    # Register logger with bot so other modules can access it
    bot._logger_event_logger = event_logger
    
    # Register all event listeners
    @bot.event
    async def on_message_delete(message):
        """Called when a message is deleted"""
        if message.guild:
            await event_logger.log_message_delete(message)
    
    @bot.event
    async def on_message_edit(before, after):
        """Called when a message is edited"""
        if before.guild:
            await event_logger.log_message_edit(before, after)
    
    @bot.event
    async def on_bulk_message_delete(messages):
        """Called when messages are bulk deleted"""
        if messages and messages[0].guild:
            await event_logger.log_bulk_message_delete(messages)
    
    @bot.event
    async def on_member_join(member):
        """Called when a member joins"""
        await event_logger.log_member_join(member)
    
    @bot.event
    async def on_member_remove(member):
        """Called when a member leaves"""
        await event_logger.log_member_leave(member)
    
    @bot.event
    async def on_member_ban(guild, user):
        """Called when a member is banned"""
        await event_logger.log_member_ban(guild, user)
    
    @bot.event
    async def on_member_unban(guild, user):
        """Called when a member is unbanned"""
        await event_logger.log_member_unban(guild, user)
    
    @bot.event
    async def on_member_update(before, after):
        """Called when a member is updated"""
        await event_logger.log_member_update(before, after)
    
    @bot.event
    async def on_guild_role_create(role):
        """Called when a role is created"""
        await event_logger.log_role_create(role)
    
    @bot.event
    async def on_guild_role_delete(role):
        """Called when a role is deleted"""
        await event_logger.log_role_delete(role)
    
    @bot.event
    async def on_guild_role_update(before, after):
        """Called when a role is updated"""
        await event_logger.log_role_update(before, after)
    
    @bot.event
    async def on_guild_channel_create(channel):
        """Called when a channel is created"""
        await event_logger.log_channel_create(channel)
    
    @bot.event
    async def on_guild_channel_delete(channel):
        """Called when a channel is deleted"""
        await event_logger.log_channel_delete(channel)
    
    @bot.event
    async def on_guild_channel_update(before, after):
        """Called when a channel is updated"""
        await event_logger.log_channel_update(before, after)
    
    @bot.event
    async def on_voice_state_update(member, before, after):
        """Called when a voice state changes"""
        await event_logger.log_voice_state_update(member, before, after)
    
    @bot.event
    async def on_invite_create(invite):
        """Called when an invite is created"""
        await event_logger.log_invite_create(invite)
    
    @bot.event
    async def on_invite_delete(invite):
        """Called when an invite is deleted"""
        await event_logger.log_invite_delete(invite)
    
    # Commands to configure logger
    @bot.tree.command(name="setjoinlogs", description="Set the channel for join/leave logs")
    @app_commands.describe(channel="Channel to send join/leave logs to")
    @app_commands.default_permissions(administrator=True)
    async def set_join_logs(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the join logs channel"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission to use this command.", ephemeral=True)
            return
        
        event_logger.config["join_logs_channel_id"] = channel.id
        event_logger.save_config()
        
        embed = discord.Embed(
            title="✅ Join Logs Channel Set",
            description=f"Join/leave logs will now be sent to {channel.mention}",
            color=0x2ecc71
        )
        
        await interaction.response.send_message(embed=embed)
        bot.logger.log(MODULE_NAME, f"Join logs channel set to {channel.name} by {interaction.user}")
    
    @bot.tree.command(name="setbotlogs", description="Set the channel for bot/moderation logs")
    @app_commands.describe(channel="Channel to send bot/moderation logs to")
    @app_commands.default_permissions(administrator=True)
    async def set_bot_logs(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the bot logs channel"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission to use this command.", ephemeral=True)
            return
        
        event_logger.config["bot_logs_channel_id"] = channel.id
        event_logger.save_config()
        
        embed = discord.Embed(
            title="✅ Bot Logs Channel Set",
            description=f"Bot/moderation logs will now be sent to {channel.mention}",
            color=0x2ecc71
        )
        
        await interaction.response.send_message(embed=embed)
        bot.logger.log(MODULE_NAME, f"Bot logs channel set to {channel.name} by {interaction.user}")
    
    @bot.tree.command(name="logconfig", description="View or toggle logging settings")
    @app_commands.default_permissions(administrator=True)
    async def log_config(interaction: discord.Interaction):
        """View logging configuration"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permission to use this command.", ephemeral=True)
            return
        
        join_channel = event_logger.get_join_logs_channel(interaction.guild)
        bot_channel = event_logger.get_bot_logs_channel(interaction.guild)
        
        embed = discord.Embed(
            title="📋 Logging Configuration",
            color=0x5865f2,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(
            name="Channels",
            value=f"**Join Logs:** {join_channel.mention if join_channel else 'Not set'}\n"
                  f"**Bot Logs:** {bot_channel.mention if bot_channel else 'Not set'}",
            inline=False
        )
        
        settings = [
            f"{'✅' if event_logger.config.get('log_message_edits') else '❌'} Message Edits",
            f"{'✅' if event_logger.config.get('log_message_deletes') else '❌'} Message Deletes",
            f"{'✅' if event_logger.config.get('log_member_joins') else '❌'} Member Joins",
            f"{'✅' if event_logger.config.get('log_member_leaves') else '❌'} Member Leaves",
            f"{'✅' if event_logger.config.get('log_bans') else '❌'} Bans/Unbans",
            f"{'✅' if event_logger.config.get('log_role_changes') else '❌'} Role Changes",
            f"{'✅' if event_logger.config.get('log_channel_changes') else '❌'} Channel Changes",
            f"{'✅' if event_logger.config.get('log_voice_changes') else '❌'} Voice Changes",
            f"{'✅' if event_logger.config.get('log_invite_changes') else '❌'} Invite Changes",
            f"{'✅' if event_logger.config.get('log_nickname_changes') else '❌'} Nickname Changes"
        ]
        
        embed.add_field(name="Enabled Features", value="\n".join(settings), inline=False)
        embed.set_footer(text="Use /setjoinlogs and /setbotlogs to configure channels")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    bot.logger.log(MODULE_NAME, "Logger module setup complete")