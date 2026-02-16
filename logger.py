# [file name]: logger.py
import discord
from discord import app_commands
from datetime import datetime
from typing import Optional
import json
import os

MODULE_NAME = "LOGGER"

class EventLogger:
    """Logs all Discord events to designated channels (#join-logs and #bot-logs)"""
    
    def __init__(self, bot):
        self.bot = bot
        from pathlib import Path
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.config_file = str(data_dir / "logger_config.json")
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
        """Save logger configuration atomically"""
        if config is None:
            config = self.config
        try:
            import tempfile
            # Write to temporary file first
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(self.config_file), suffix='.tmp')
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(config, f, indent=2)
                # Atomic replace
                os.replace(temp_path, self.config_file)
            except:
                # Clean up temp file if something fails
                try:
                    os.unlink(temp_path)
                except:
                    pass
                raise
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
    
    async def log_to_channel(self, channel, embed) -> Optional[int]:
        """Send log embed to channel, returns message ID or None"""
        if not channel:
            return None
        try:
            msg = await channel.send(embed=embed)
            return msg.id
        except discord.Forbidden:
            self.bot.logger.error(MODULE_NAME, f"No permission to send logs to {channel.name}")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send log to {channel.name}", e)
        return None
    
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
        """Log when a member is banned — handled by moderation.py for bot actions"""
        pass  # Ban logging is handled by moderation.py's explicit log_ban embeds

    async def log_member_unban(self, guild, user):
        """Log when a member is unbanned — handled by moderation.py for bot actions"""
        pass  # Unban logging is handled by moderation.py's explicit botlog embeds
    
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

    # ====================
    # MODERATION ACTION LOGGING
    # ====================

    async def log_ban(self, guild: discord.Guild, user: discord.User,
                      moderator: discord.Member, reason: str,
                      delete_days: int, action_channel: discord.TextChannel) -> Optional[int]:
        """Log a ban action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="User Banned",
            description=f"{user.mention} was banned from the server.",
            color=0x992d22,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} day{'s' if delete_days != 1 else ''}", inline=True)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_unban(self, guild: discord.Guild, user: discord.User,
                        moderator: discord.Member, reason: str) -> Optional[int]:
        """Log an unban action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="User Unbanned",
            description=f"{user.mention} was unbanned from the server.",
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        return await self.log_to_channel(channel, embed)

    async def log_kick(self, guild: discord.Guild, member: discord.Member,
                       moderator: discord.Member, reason: str,
                       action_channel: discord.TextChannel) -> Optional[int]:
        """Log a kick action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="Member Kicked",
            description=f"{member.mention} was kicked from the server.",
            color=0xe67e22,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_timeout(self, guild: discord.Guild, member: discord.Member,
                          moderator: discord.Member, reason: str,
                          duration_str: str, action_channel: discord.TextChannel) -> Optional[int]:
        """Log a timeout action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="Member Timed Out",
            description=f"{member.mention} was timed out.",
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_mute(self, guild: discord.Guild, member: discord.Member,
                       moderator: discord.Member, reason: str,
                       duration_str: str, action_channel: discord.TextChannel) -> Optional[int]:
        """Log a mute action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="Member Muted",
            description=f"{member.mention} was muted.",
            color=0xf39c12,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_softban(self, guild: discord.Guild, member: discord.Member,
                          moderator: discord.Member, reason: str,
                          delete_days: int, action_channel: discord.TextChannel) -> Optional[int]:
        """Log a softban action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="Member Softbanned",
            description=f"{member.mention} was softbanned (messages deleted, can rejoin).",
            color=0x992d22,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} day{'s' if delete_days != 1 else ''}", inline=True)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_purge(self, guild: discord.Guild, moderator: discord.Member,
                        count: int, action_channel: discord.TextChannel,
                        target_user: Optional[discord.Member] = None) -> Optional[int]:
        """Log a purge action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        desc = f"**{count}** message{'s' if count != 1 else ''} deleted"
        desc += f" from {target_user.mention}" if target_user else f" in {action_channel.mention}"
        embed = discord.Embed(
            title="Messages Purged",
            description=desc,
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        if target_user:
            embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        embed.add_field(name="Amount", value=str(count), inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_warn(self, guild: discord.Guild, member: discord.Member,
                       moderator: discord.Member, reason: str,
                       strike_count: int, action_channel: discord.TextChannel) -> Optional[int]:
        """Log a warn action to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="Member Warned",
            description=f"{member.mention} was warned.",
            color=0xf39c12,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
        embed.add_field(name="Channel", value=action_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_lock(self, guild: discord.Guild, moderator: discord.Member,
                       reason: str, locked_channel: discord.TextChannel) -> Optional[int]:
        """Log a channel lock to bot-logs. Returns message ID."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="Channel Locked",
            description=f"{locked_channel.mention} was locked.",
            color=0xe74c3c,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Moderator", value=moderator.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Channel", value=locked_channel.mention, inline=True)
        return await self.log_to_channel(channel, embed)

    async def log_autoban(self, guild: discord.Guild, user: discord.User,
                          reason: str, trigger_channel: discord.TextChannel) -> None:
        """Log an auto-mod ban to bot-logs."""
        channel = self.get_bot_logs_channel(guild)
        embed = discord.Embed(
            title="AUTO-BAN",
            description=f"{user.mention} was automatically banned.",
            color=0xdc143c,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Channel", value=trigger_channel.mention, inline=True)
        await self.log_to_channel(channel, embed)

    async def log_autoban_strike(self, guild: discord.Guild, user: discord.User,
                                 strike_count: int, reason: str,
                                 trigger_channel: discord.TextChannel) -> None:
        """Log an auto-mod strike (or strike-triggered ban) to bot-logs."""
        channel = self.get_bot_logs_channel(guild)
        if strike_count >= 2:
            embed = discord.Embed(
                title="AUTO-BAN: Repeated Violation",
                description=f"{user.mention} was automatically banned after {strike_count} strikes.",
                color=0xdc143c,
                timestamp=datetime.utcnow()
            )
        else:
            embed = discord.Embed(
                title=f"Auto-Mod Strike {strike_count}/2",
                description=f"{user.mention} received a strike.",
                color=0xf39c12,
                timestamp=datetime.utcnow()
            )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Channel", value=trigger_channel.mention, inline=True)
        await self.log_to_channel(channel, embed)


def setup(bot):
    """Setup function called by main.py"""
    
    event_logger = EventLogger(bot)
    
    # Register logger with bot so other modules can access it
    bot._logger_event_logger = event_logger
    
    # Register all event listeners
    @bot.listen()
    async def on_message_delete(message):
        """Called when a message is deleted"""
        if message.guild:
            await event_logger.log_message_delete(message)
    
    @bot.listen()
    async def on_message_edit(before, after):
        """Called when a message is edited"""
        if before.guild:
            await event_logger.log_message_edit(before, after)
    
    @bot.listen()
    async def on_bulk_message_delete(messages):
        """Called when messages are bulk deleted"""
        if messages and messages[0].guild:
            await event_logger.log_bulk_message_delete(messages)
    
    @bot.listen()
    async def on_member_join(member):
        """Called when a member joins"""
        await event_logger.log_member_join(member)
    
    @bot.listen()
    async def on_member_remove(member):
        """Called when a member leaves"""
        await event_logger.log_member_leave(member)
    
    @bot.listen()
    async def on_member_ban(guild, user):
        """Called when a member is banned"""
        await event_logger.log_member_ban(guild, user)
    
    @bot.listen()
    async def on_member_unban(guild, user):
        """Called when a member is unbanned"""
        await event_logger.log_member_unban(guild, user)
    
    @bot.listen()
    async def on_member_update(before, after):
        """Called when a member is updated"""
        await event_logger.log_member_update(before, after)
    
    @bot.listen()
    async def on_guild_role_create(role):
        """Called when a role is created"""
        await event_logger.log_role_create(role)
    
    @bot.listen()
    async def on_guild_role_delete(role):
        """Called when a role is deleted"""
        await event_logger.log_role_delete(role)
    
    @bot.listen()
    async def on_guild_role_update(before, after):
        """Called when a role is updated"""
        await event_logger.log_role_update(before, after)
    
    @bot.listen()
    async def on_guild_channel_create(channel):
        """Called when a channel is created"""
        await event_logger.log_channel_create(channel)
    
    @bot.listen()
    async def on_guild_channel_delete(channel):
        """Called when a channel is deleted"""
        await event_logger.log_channel_delete(channel)
    
    @bot.listen()
    async def on_guild_channel_update(before, after):
        """Called when a channel is updated"""
        await event_logger.log_channel_update(before, after)
    
    @bot.listen()
    async def on_voice_state_update(member, before, after):
        """Called when a voice state changes"""
        await event_logger.log_voice_state_update(member, before, after)
    
    @bot.listen()
    async def on_invite_create(invite):
        """Called when an invite is created"""
        await event_logger.log_invite_create(invite)
    
    @bot.listen()
    async def on_invite_delete(invite):
        """Called when an invite is deleted"""
        await event_logger.log_invite_delete(invite)
    
    # Commands to configure logger
    @bot.tree.command(name="setjoinlogs", description="Set the channel for join/leave logs")
    @app_commands.describe(channel="Channel to send join/leave logs to")
    @app_commands.default_permissions(administrator=True)
    async def set_join_logs(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the join logs channel"""
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