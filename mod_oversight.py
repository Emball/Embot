# [file name]: mod_oversight.py
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import json
import asyncio
import os
from typing import Optional, Dict, List
import io
from PIL import Image, ImageDraw, ImageFont
import pytz
from pathlib import Path

MODULE_NAME = "MOD_OVERSIGHT"

# ==================== HARDCODED CONFIGURATION ====================
# Edit these values directly — bot_config.json is no longer used.

OWNER_ID = 1328822521084117033

CONFIG = {
    "owner_id": OWNER_ID,
    "channels": {
        "join_logs_channel_id": 1229868495307669608,
        "bot_logs_channel_id": 1229871835978666115
    },
    "elevated_roles": ["Moderator", "Admin", "Owner"],
    "moderation": {
        "min_reason_length": 10,
        "muted_role_name": "Muted"
    },
    "oversight": {
        "report_time_cst": "00:00",
        "context_message_count": 30,
        "invite_cleanup_days": 7
    }
}

# Minimum reason length (in characters)
MIN_REASON_LENGTH = CONFIG["moderation"]["min_reason_length"]

# Context message count
CONTEXT_MESSAGE_COUNT = CONFIG["oversight"]["context_message_count"]

class ModOversightSystem:
    """
    Comprehensive moderation oversight system that tracks all mod actions,
    logs context, handles appeals, and provides daily reports to the owner.
    """
    
    def __init__(self, bot):
        self.bot = bot
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        self.data_file = str(data_dir / "mod_oversight_data.json")
        self.appeals_file = str(data_dir / "ban_appeals.json")
        self.invites_file = str(data_dir / "ban_reversal_invites.json")
        
        # Load data
        self.pending_actions = self.load_data(self.data_file, {})
        self.appeals = self.load_data(self.appeals_file, {})
        self.invites = self.load_data(self.invites_file, {})
        
        # Message cache for context logging (guild_id -> channel_id -> [messages])
        self.message_cache = {}
        
        # Track embed message IDs for deletion monitoring
        # Format: {message_id: {'action_id': str, 'type': 'inchat' or 'botlog'}}
        self.tracked_embeds = {}
        
        # Start background tasks
        self.cleanup_invites.start()
        self.send_daily_report.start()
        
        self.bot.logger.log(MODULE_NAME, "Mod oversight system initialized")
    
    def load_data(self, filename: str, default):
        """Load data from JSON file"""
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to load {filename}", e)
            return default
    
    def save_data(self, filename: str, data):
        """Save data to JSON file atomically"""
        try:
            import tempfile
            # Write to temporary file first
            temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(filename), suffix='.tmp')
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(data, f, indent=2)
                # Atomic replace
                os.replace(temp_path, filename)
            except:
                # Clean up temp file if something fails
                try:
                    os.unlink(temp_path)
                except:
                    pass
                raise
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to save {filename}", e)
    
    def save_pending_actions(self):
        """Save pending actions to file"""
        self.save_data(self.data_file, self.pending_actions)
    
    def save_appeals(self):
        """Save appeals to file"""
        self.save_data(self.appeals_file, self.appeals)
    
    def save_invites(self):
        """Save invites to file"""
        self.save_data(self.invites_file, self.invites)
    
    async def cache_message(self, message: discord.Message):
        """Cache a message for context logging"""
        if message.guild is None or message.author.bot:
            return
        
        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        
        if guild_id not in self.message_cache:
            self.message_cache[guild_id] = {}
        
        if channel_id not in self.message_cache[guild_id]:
            self.message_cache[guild_id][channel_id] = []
        
        # Store message data
        msg_data = {
            'id': message.id,
            'author': str(message.author),
            'author_id': message.author.id,
            'content': message.content,
            'timestamp': message.created_at.isoformat(),
            'attachments': [att.url for att in message.attachments],
            'embeds': len(message.embeds)
        }
        
        self.message_cache[guild_id][channel_id].append(msg_data)
        
        # Keep only last 100 messages per channel
        if len(self.message_cache[guild_id][channel_id]) > 100:
            self.message_cache[guild_id][channel_id].pop(0)
    
    def get_context_messages(self, guild_id: int, channel_id: int, around_message_id: int, count: int = None) -> List[Dict]:
        """Get messages around a specific message ID"""
        if count is None:
            count = CONTEXT_MESSAGE_COUNT
        
        guild_key = str(guild_id)
        channel_key = str(channel_id)
        
        if guild_key not in self.message_cache or channel_key not in self.message_cache[guild_key]:
            return []
        
        messages = self.message_cache[guild_key][channel_key]
        
        # Find the index of the target message
        target_idx = None
        for i, msg in enumerate(messages):
            if msg['id'] == around_message_id:
                target_idx = i
                break
        
        if target_idx is None:
            # If message not found, return last N messages
            return messages[-count:]
        
        # Get messages before and after
        half_count = count // 2
        start = max(0, target_idx - half_count)
        end = min(len(messages), target_idx + half_count + 1)
        
        return messages[start:end]
    
    def generate_context_screenshot(self, messages: List[Dict], highlighted_msg_id: Optional[int] = None) -> io.BytesIO:
        """Generate a synthetic screenshot of the message context"""
        # Image settings
        width = 800
        line_height = 60
        padding = 20
        height = len(messages) * line_height + padding * 2
        
        # Create image
        img = Image.new('RGB', (width, height), color='#36393f')
        draw = ImageDraw.Draw(img)
        
        # Try to use a font, fallback to default
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
            font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except:
            font = ImageFont.load_default()
            font_bold = font
        
        y = padding
        for msg in messages:
            # Highlight if this is the target message
            if highlighted_msg_id and msg['id'] == highlighted_msg_id:
                draw.rectangle([0, y - 5, width, y + line_height - 5], fill='#4a4d52')
            
            # Draw author name
            timestamp = datetime.fromisoformat(msg['timestamp']).strftime("%H:%M")
            author_text = f"{msg['author']} - {timestamp}"
            draw.text((padding, y), author_text, fill='#7289da', font=font_bold)
            
            # Draw message content
            content = msg['content'][:100]  # Truncate long messages
            if msg['content'] and len(msg['content']) > 100:
                content += "..."
            if not content and msg['attachments']:
                content = "[Attachment]"
            if not content and msg['embeds'] > 0:
                content = "[Embed]"
            if not content:
                content = "[Empty message]"
            
            draw.text((padding, y + 20), content, fill='#dcddde', font=font)
            
            y += line_height
        
        # Save to bytes
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        return buffer
    
    async def log_mod_action(self, action_data: Dict) -> str:
        """
        Log a moderation action and return the action ID.
        
        action_data should contain:
        - action: str (ban, mute, kick, warn, purge, etc.)
        - moderator_id: int
        - moderator: str
        - user_id: int (or None for purge)
        - user: str (or None for purge)
        - reason: str
        - guild_id: int
        - channel_id: int
        - message_id: int (the command message that triggered this)
        - duration: str (for temporary actions)
        - additional: dict (any additional data)
        """
        
        # EXCLUSION: Ignore mutes, warns, and timeouts for the oversight report/queue
        if action_data['action'] in ['mute', 'warn', 'timeout']:
            return None

        # Generate unique action ID
        action_id = f"{action_data['guild_id']}_{action_data['action']}_{int(datetime.utcnow().timestamp())}"
        
        # Get message context
        context_messages = []
        if 'message_id' in action_data and 'channel_id' in action_data:
            context_messages = self.get_context_messages(
                action_data['guild_id'],
                action_data['channel_id'],
                action_data['message_id'],
                count=CONTEXT_MESSAGE_COUNT
            )
        
        # Store the action
        action_record = {
            'id': action_id,
            'action': action_data['action'],
            'moderator_id': action_data['moderator_id'],
            'moderator': action_data['moderator'],
            'user_id': action_data.get('user_id'),
            'user': action_data.get('user'),
            'reason': action_data['reason'],
            'guild_id': action_data['guild_id'],
            'channel_id': action_data.get('channel_id'),
            'message_id': action_data.get('message_id'),
            'timestamp': datetime.utcnow().isoformat(),
            'context_messages': context_messages,
            'duration': action_data.get('duration'),
            'additional': action_data.get('additional', {}),
            'flags': [],
            'embed_ids': {
                'inchat': None,
                'botlog': None
            },
            'status': 'pending'  # pending, approved, reverted
        }
        
        self.pending_actions[action_id] = action_record
        self.save_pending_actions()
        
        self.bot.logger.log(MODULE_NAME, f"Logged mod action: {action_id} by {action_data['moderator']}")
        
        return action_id
    
    def resolve_pending_action(self, user_id: int, action_type: str):
        """
        Manually resolve/remove a pending action from the queue.
        Used when a moderator manually reverses an action (e.g., manual /unban).
        """
        to_delete = []
        for action_id, action in self.pending_actions.items():
            # Check if action matches user and type
            if (action.get('user_id') == user_id and 
                action.get('action') == action_type and 
                action.get('status') == 'pending'):
                
                to_delete.append(action_id)
        
        if to_delete:
            for aid in to_delete:
                del self.pending_actions[aid]
            
            self.save_pending_actions()
            self.bot.logger.log(MODULE_NAME, f"Auto-resolved {len(to_delete)} pending {action_type} action(s) for user {user_id}")
            return True
        return False

    def track_embed(self, message_id: int, action_id: str, embed_type: str):
        """Track an embed message for deletion monitoring"""
        self.tracked_embeds[message_id] = {
            'action_id': action_id,
            'type': embed_type  # 'inchat' or 'botlog'
        }
        
        # Update the action record
        if action_id in self.pending_actions:
            self.pending_actions[action_id]['embed_ids'][embed_type] = message_id
            self.save_pending_actions()
    
    async def handle_embed_deletion(self, message_id: int):
        """Handle when a tracked embed is deleted"""
        if message_id not in self.tracked_embeds:
            return
        
        embed_info = self.tracked_embeds[message_id]
        action_id = embed_info['action_id']
        embed_type = embed_info['type']
        
        if action_id not in self.pending_actions:
            return
        
        action = self.pending_actions[action_id]
        
        # Check what's been deleted
        inchat_deleted = action['embed_ids']['inchat'] is not None and \
                        (embed_type == 'inchat' or 'inchat_deleted' in action['flags'])
        botlog_deleted = action['embed_ids']['botlog'] is not None and \
                        (embed_type == 'botlog' or 'botlog_deleted' in action['flags'])
        
        if embed_type == 'inchat':
            if 'inchat_deleted' not in action['flags']:
                action['flags'].append('inchat_deleted')
        elif embed_type == 'botlog':
            if 'botlog_deleted' not in action['flags']:
                action['flags'].append('botlog_deleted')
        
        # Determine flag color
        if inchat_deleted and botlog_deleted:
            if 'red_flag' not in action['flags']:
                action['flags'].append('red_flag')
                self.bot.logger.log(MODULE_NAME, f"🚩 RED FLAG: Both embeds deleted for action {action_id}", "WARNING")
        elif inchat_deleted or botlog_deleted:
            if 'yellow_flag' not in action['flags']:
                action['flags'].append('yellow_flag')
                self.bot.logger.log(MODULE_NAME, f"⚠️ YELLOW FLAG: Embed deleted for action {action_id}", "WARNING")
        
        self.save_pending_actions()
        
        # Remove from tracking
        del self.tracked_embeds[message_id]
    
    async def approve_action(self, action_id: str) -> bool:
        """Approve a moderation action and remove it from pending"""
        if action_id not in self.pending_actions:
            return False
        
        action = self.pending_actions[action_id]
        action['status'] = 'approved'
        action['reviewed_at'] = datetime.utcnow().isoformat()
        
        # Remove from pending (move to archive if needed in future)
        del self.pending_actions[action_id]
        self.save_pending_actions()
        
        self.bot.logger.log(MODULE_NAME, f"Action {action_id} approved and removed from pending")
        return True
    
    async def revert_action(self, action_id: str, guild: discord.Guild) -> bool:
        """Revert a moderation action"""
        if action_id not in self.pending_actions:
            return False
        
        action = self.pending_actions[action_id]
        
        # Handle different action types
        if action['action'] == 'ban':
            return await self.revert_ban(action, guild)
        elif action['action'] == 'mute':
            return await self.revert_mute(action, guild)
        elif action['action'] == 'kick':
            # Can't undo a kick, but we log it
            action['status'] = 'reverted'
            action['reviewed_at'] = datetime.utcnow().isoformat()
            self.save_pending_actions()
            self.bot.logger.log(MODULE_NAME, f"Kick action {action_id} marked as reverted (cannot undo)")
            return True
        
        # For other actions, just mark as reverted
        action['status'] = 'reverted'
        action['reviewed_at'] = datetime.utcnow().isoformat()
        self.save_pending_actions()
        return True
    
    async def revert_ban(self, action: Dict, guild: discord.Guild) -> bool:
        """Revert a ban"""
        try:
            user_id = action['user_id']
            
            # Unban the user
            user = await self.bot.fetch_user(user_id)
            await guild.unban(user, reason="Ban reverted after review")
            
            # Create invite
            invite_link = await self.create_ban_reversal_invite(guild, user_id)
            
            # Send DM
            try:
                embed = discord.Embed(
                    title="Ban Reverted",
                    description=f"After reviewing your case, we've decided to revert your ban from **{guild.name}**.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(
                    name="Rejoin Server",
                    value=f"You can rejoin using this invite:\n{invite_link}",
                    inline=False
                )
                embed.set_footer(text="This invite is for you only and will not expire")
                
                await user.send(embed=embed)
            except discord.Forbidden:
                self.bot.logger.log(MODULE_NAME, f"Could not DM user {user} about ban reversal")
            
            # Log the reversal in bot logs
            bot_logs_channel = self.get_bot_logs_channel(guild)
            if bot_logs_channel:
                log_embed = discord.Embed(
                    title="Ban Reverted (Review System)",
                    description=f"**{user}** ({user_id}) has been unbanned after review.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
                log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
                await bot_logs_channel.send(embed=log_embed)
            
            # Mark as reverted
            action['status'] = 'reverted'
            action['reviewed_at'] = datetime.utcnow().isoformat()
            del self.pending_actions[action['id']]
            self.save_pending_actions()
            
            self.bot.logger.log(MODULE_NAME, f"Ban reverted for user {user_id} in guild {guild.id}")
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to revert ban for action {action['id']}", e)
            return False
    
    async def revert_mute(self, action: Dict, guild: discord.Guild) -> bool:
        """Revert a mute"""
        try:
            user_id = action['user_id']
            member = guild.get_member(user_id)
            
            if not member:
                self.bot.logger.log(MODULE_NAME, f"Cannot revert mute - user {user_id} not in guild")
                return False
            
            # Remove mute role
            muted_role = discord.utils.get(guild.roles, name="Muted")
            if muted_role and muted_role in member.roles:
                await member.remove_roles(muted_role, reason="Mute reverted after review")
            
            # Send DM
            try:
                embed = discord.Embed(
                    title="Mute Reverted",
                    description=f"After reviewing your case, your mute in **{guild.name}** has been reverted.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            
            # Log the reversal
            bot_logs_channel = self.get_bot_logs_channel(guild)
            if bot_logs_channel:
                log_embed = discord.Embed(
                    title="Mute Reverted (Review System)",
                    description=f"**{member}** has been unmuted after review.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
                log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
                await bot_logs_channel.send(embed=log_embed)
            
            # Mark as reverted
            action['status'] = 'reverted'
            action['reviewed_at'] = datetime.utcnow().isoformat()
            del self.pending_actions[action['id']]
            self.save_pending_actions()
            
            self.bot.logger.log(MODULE_NAME, f"Mute reverted for user {user_id} in guild {guild.id}")
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to revert mute for action {action['id']}", e)
            return False
    
    async def create_ban_reversal_invite(self, guild: discord.Guild, user_id: int) -> str:
        """Create a single-use invite for a banned user"""
        try:
            # Find the first available text channel
            channel = None
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).create_instant_invite:
                    channel = ch
                    break
            
            if not channel:
                self.bot.logger.error(MODULE_NAME, "No channel available to create invite")
                return "Could not create invite - no suitable channel"
            
            # Create invite
            invite = await channel.create_invite(
                max_uses=1,
                max_age=0,  # Never expires
                unique=True,
                reason=f"Ban reversal for user {user_id}"
            )
            
            # Store invite info
            invite_key = f"{guild.id}_{user_id}"
            self.invites[invite_key] = {
                'code': invite.code,
                'user_id': user_id,
                'guild_id': guild.id,
                'created_at': datetime.utcnow().isoformat()
            }
            self.save_invites()
            
            return invite.url
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to create ban reversal invite", e)
            return "Error creating invite"
    
    async def cleanup_old_invites(self):
        """Clean up invites that haven't been used after configured days"""
        try:
            cleanup_days = CONFIG["oversight"]["invite_cleanup_days"]
            cutoff = datetime.utcnow() - timedelta(days=cleanup_days)
            to_delete = []
            
            for invite_key, data in self.invites.items():
                created = datetime.fromisoformat(data['created_at'])
                if created < cutoff:
                    # Try to delete the invite
                    try:
                        guild = self.bot.get_guild(data['guild_id'])
                        if guild:
                            invites = await guild.invites()
                            for inv in invites:
                                if inv.code == data['code']:
                                    await inv.delete(reason="Unused ban reversal invite cleanup")
                                    break
                    except:
                        pass
                    
                    to_delete.append(invite_key)
            
            for key in to_delete:
                del self.invites[key]
            
            if to_delete:
                self.save_invites()
                self.bot.logger.log(MODULE_NAME, f"Cleaned up {len(to_delete)} old ban reversal invites")
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to cleanup old invites", e)
    
    def get_bot_logs_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Get the bot logs channel from config"""
        bot_logs_id = CONFIG["channels"]["bot_logs_channel_id"]
        if bot_logs_id:
            return guild.get_channel(bot_logs_id)
        
        # Fallback to logger module if available
        if hasattr(self.bot, '_logger_event_logger'):
            return self.bot._logger_event_logger.get_bot_logs_channel(guild)
        return None
    
    async def submit_appeal(self, user_id: int, guild_id: int, appeal_text: str) -> str:
        """Submit a ban appeal"""
        appeal_id = f"{guild_id}_{user_id}_{int(datetime.utcnow().timestamp())}"
        
        appeal_data = {
            'id': appeal_id,
            'user_id': user_id,
            'guild_id': guild_id,
            'appeal_text': appeal_text,
            'submitted_at': datetime.utcnow().isoformat(),
            'status': 'pending'  # pending, approved, denied
        }
        
        self.appeals[appeal_id] = appeal_data
        self.save_appeals()
        
        self.bot.logger.log(MODULE_NAME, f"Ban appeal submitted: {appeal_id}")
        return appeal_id
    
    async def approve_appeal(self, appeal_id: str) -> bool:
        """Approve a ban appeal and unban the user"""
        if appeal_id not in self.appeals:
            return False
        
        appeal = self.appeals[appeal_id]
        
        try:
            guild = self.bot.get_guild(appeal['guild_id'])
            if not guild:
                return False
            
            user = await self.bot.fetch_user(appeal['user_id'])
            
            # Unban
            await guild.unban(user, reason="Appeal approved")
            
            # Create invite
            invite_link = await self.create_ban_reversal_invite(guild, appeal['user_id'])
            
            # Send DM
            try:
                embed = discord.Embed(
                    title="Ban Appeal Approved",
                    description=f"Your appeal for **{guild.name}** has been approved!",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(
                    name="Rejoin Server",
                    value=f"You can rejoin using this invite:\n{invite_link}",
                    inline=False
                )
                embed.set_footer(text="Welcome back!")
                
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            
            # Log
            bot_logs_channel = self.get_bot_logs_channel(guild)
            if bot_logs_channel:
                log_embed = discord.Embed(
                    title="Ban Appeal Approved",
                    description=f"**{user}** has been unbanned after appeal approval.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Appeal Text", value=appeal['appeal_text'][:1024], inline=False)
                await bot_logs_channel.send(embed=log_embed)
            
            # Update appeal status
            appeal['status'] = 'approved'
            appeal['reviewed_at'] = datetime.utcnow().isoformat()
            del self.appeals[appeal_id]
            self.save_appeals()
            
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to approve appeal {appeal_id}", e)
            return False
    
    async def deny_appeal(self, appeal_id: str) -> bool:
        """Deny a ban appeal"""
        if appeal_id not in self.appeals:
            return False
        
        appeal = self.appeals[appeal_id]
        
        try:
            guild = self.bot.get_guild(appeal['guild_id'])
            user = await self.bot.fetch_user(appeal['user_id'])
            
            # Send DM
            try:
                embed = discord.Embed(
                    title="Ban Appeal Denied",
                    description=f"Your appeal for **{guild.name}** has been reviewed and denied.",
                    color=0xe74c3c,
                    timestamp=datetime.utcnow()
                )
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            
            # Update appeal status
            appeal['status'] = 'denied'
            appeal['reviewed_at'] = datetime.utcnow().isoformat()
            del self.appeals[appeal_id]
            self.save_appeals()
            
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to deny appeal {appeal_id}", e)
            return False
    
    @tasks.loop(hours=24)
    async def cleanup_invites(self):
        """Periodic task to cleanup old invites"""
        await self.cleanup_old_invites()
    
    @cleanup_invites.before_loop
    async def before_cleanup_invites(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=24)
    async def send_daily_report(self):
        """Send daily moderation report to owner at 12:00 AM CST"""
        try:
            # Wait until 12:00 AM CST
            cst = pytz.timezone('America/Chicago')
            now = datetime.now(cst)
            
            # Calculate next midnight
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (next_midnight - now).total_seconds()
            
            await asyncio.sleep(wait_seconds)
            
            # Generate and send report
            await self.generate_daily_report()
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send daily report", e)
    
    @send_daily_report.before_loop
    async def before_send_daily_report(self):
        await self.bot.wait_until_ready()
    
    async def generate_daily_report(self):
        """Generate and send the daily moderation report"""
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            if not owner:
                self.bot.logger.error(MODULE_NAME, "Could not find owner user")
                return
            
            # Get all pending actions
            if not self.pending_actions and not self.appeals:
                # No pending items
                embed = discord.Embed(
                    title="📊 Daily Moderation Report",
                    description="No pending moderation actions or appeals to review.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                await owner.send(embed=embed)
                return
            
            # Create report embed
            embed = discord.Embed(
                title="📊 Daily Moderation Report",
                description=f"**{len(self.pending_actions)}** pending action(s) | **{len(self.appeals)}** appeal(s)",
                color=0x5865f2,
                timestamp=datetime.utcnow()
            )
            
            await owner.send(embed=embed)
            
            # Send each pending action with review buttons
            for action_id, action in list(self.pending_actions.items())[:10]:  # Limit to 10 per report
                await self.send_action_review(owner, action_id, action)
            
            # Send each appeal with review buttons
            for appeal_id, appeal in list(self.appeals.items())[:10]:
                await self.send_appeal_review(owner, appeal_id, appeal)
            
            self.bot.logger.log(MODULE_NAME, "Daily report sent to owner")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to generate daily report", e)
    
    async def send_action_review(self, owner: discord.User, action_id: str, action: Dict):
        """Send an action for review with buttons"""
        try:
            # Build embed
            embed = discord.Embed(
                title=f"🔍 {action['action'].upper()} Action Review",
                color=0xe74c3c if 'red_flag' in action['flags'] else 
                      0xf39c12 if 'yellow_flag' in action['flags'] else 0x5865f2,
                timestamp=datetime.fromisoformat(action['timestamp'])
            )
            
            # Add flags if any
            if action['flags']:
                flags_text = []
                if 'red_flag' in action['flags']:
                    flags_text.append("🚩 **RED FLAG** - Both embeds deleted")
                elif 'yellow_flag' in action['flags']:
                    flags_text.append("⚠️ **YELLOW FLAG** - Embed deleted")
                
                if 'inchat_deleted' in action['flags']:
                    flags_text.append("❌ In-chat embed deleted")
                if 'botlog_deleted' in action['flags']:
                    flags_text.append("❌ Bot-log embed deleted")
                
                embed.add_field(name="⚠️ Flags", value="\n".join(flags_text), inline=False)
            
            embed.add_field(name="Moderator", value=f"{action['moderator']} (ID: {action['moderator_id']})", inline=True)
            if action.get('user'):
                embed.add_field(name="User", value=f"{action['user']} (ID: {action['user_id']})", inline=True)
            embed.add_field(name="Reason", value=action['reason'], inline=False)
            
            if action.get('duration'):
                embed.add_field(name="Duration", value=action['duration'], inline=True)
            
            # Add context info
            if action['context_messages']:
                embed.add_field(
                    name="Context",
                    value=f"{len(action['context_messages'])} messages logged",
                    inline=True
                )
            
            # Create view with buttons
            view = ActionReviewView(self, action_id, action)
            
            await owner.send(embed=embed, view=view)
            
            # Send context screenshot if available
            if action['context_messages']:
                context_image = self.generate_context_screenshot(
                    action['context_messages'],
                    action.get('message_id')
                )
                file = discord.File(context_image, filename="context.png")
                await owner.send(file=file)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send action review for {action_id}", e)
    
    async def send_appeal_review(self, owner: discord.User, appeal_id: str, appeal: Dict):
        """Send an appeal for review with buttons"""
        try:
            embed = discord.Embed(
                title="📝 Ban Appeal Review",
                color=0x9b59b6,
                timestamp=datetime.fromisoformat(appeal['submitted_at'])
            )
            
            embed.add_field(name="User ID", value=str(appeal['user_id']), inline=True)
            embed.add_field(name="Guild ID", value=str(appeal['guild_id']), inline=True)
            embed.add_field(name="Appeal Text", value=appeal['appeal_text'][:1024], inline=False)
            
            view = AppealReviewView(self, appeal_id)
            
            await owner.send(embed=embed, view=view)
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send appeal review for {appeal_id}", e)


class ActionReviewView(ui.View):
    """View with buttons for reviewing mod actions"""
    
    def __init__(self, oversight: ModOversightSystem, action_id: str, action: Dict):
        super().__init__(timeout=None)
        self.oversight = oversight
        self.action_id = action_id
        self.action = action
    
    @ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.oversight.approve_action(self.action_id)
        
        if success:
            await interaction.response.send_message("✅ Action approved and removed from pending.", ephemeral=True)
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to approve action.", ephemeral=True)
    
    @ui.button(label="Revert", style=discord.ButtonStyle.red, emoji="↩️")
    async def revert_button(self, interaction: discord.Interaction, button: ui.Button):
        guild = self.oversight.bot.get_guild(self.action['guild_id'])
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
            return
        
        success = await self.oversight.revert_action(self.action_id, guild)
        
        if success:
            await interaction.response.send_message("↩️ Action reverted successfully.", ephemeral=True)
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to revert action.", ephemeral=True)
    
    @ui.button(label="View Chat", style=discord.ButtonStyle.gray, emoji="💬")
    async def view_chat_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.action.get('channel_id') or not self.action.get('message_id'):
            await interaction.response.send_message("❌ No chat link available.", ephemeral=True)
            return
        
        guild_id = self.action['guild_id']
        channel_id = self.action['channel_id']
        message_id = self.action['message_id']
        
        jump_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        
        await interaction.response.send_message(f"📍 [Jump to message]({jump_link})", ephemeral=True)


class AppealReviewView(ui.View):
    """View with buttons for reviewing ban appeals"""
    
    def __init__(self, oversight: ModOversightSystem, appeal_id: str):
        super().__init__(timeout=None)
        self.oversight = oversight
        self.appeal_id = appeal_id
    
    @ui.button(label="Accept Appeal", style=discord.ButtonStyle.green, emoji="✅")
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.oversight.approve_appeal(self.appeal_id)
        
        if success:
            await interaction.response.send_message("✅ Appeal accepted and user unbanned.", ephemeral=True)
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to accept appeal.", ephemeral=True)
    
    @ui.button(label="Deny Appeal", style=discord.ButtonStyle.red, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.oversight.deny_appeal(self.appeal_id)
        
        if success:
            await interaction.response.send_message("❌ Appeal denied.", ephemeral=True)
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to deny appeal.", ephemeral=True)


class BanAppealModal(ui.Modal, title="Ban Appeal"):
    """Modal for submitting a ban appeal"""
    
    appeal_text = ui.TextInput(
        label="Why should you be unbanned?",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why you believe the ban should be lifted...",
        required=True,
        max_length=1000
    )
    
    def __init__(self, oversight: ModOversightSystem, guild_id: int):
        super().__init__()
        self.oversight = oversight
        self.guild_id = guild_id
    
    async def on_submit(self, interaction: discord.Interaction):
        # Submit the appeal
        appeal_id = await self.oversight.submit_appeal(
            interaction.user.id,
            self.guild_id,
            self.appeal_text.value
        )
        
        embed = discord.Embed(
            title="✅ Appeal Submitted",
            description="Your ban appeal has been submitted and will be reviewed.",
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="What happens next?",
            value="The server owner will review your appeal and you'll be notified of the decision.",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


def setup(bot):
    """Setup function called by main.py"""
    
    # Initialize the oversight system
    oversight = ModOversightSystem(bot)
    
    # Make it accessible to other modules
    bot.mod_oversight = oversight
    
    # Register message cache event
    @bot.listen()
    async def on_message(message):
        """Cache messages for context logging"""
        await oversight.cache_message(message)
    
    # Register embed deletion tracking
    @bot.listen()
    async def on_message_delete(message):
        """Track when moderation embeds are deleted"""
        await oversight.handle_embed_deletion(message.id)
    
    # /report command — owner can DM the bot to trigger the report early
    @bot.tree.command(name="report", description="[Owner only] Trigger the moderation report immediately")
    async def report_command(interaction: discord.Interaction):
        """Manually trigger the daily moderation report (owner only, usable in DMs)"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ This command is restricted to the bot owner.", ephemeral=True)
            return
        
        await interaction.response.send_message("📊 Generating report...", ephemeral=True)
        
        try:
            await oversight.generate_daily_report()
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Manual report generation failed", e)
    
    bot.logger.log(MODULE_NAME, "Mod oversight module setup complete")