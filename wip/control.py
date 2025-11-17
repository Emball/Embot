import discord
from discord.ext import commands
import asyncio

import aiohttp
import aiohttp.web
import json
import threading
from datetime import datetime
from typing import Optional
import os
import webbrowser
import time
from collections import defaultdict
import base64
import io

MODULE_NAME = "CONTROL"

class DiscordWebClient:
    """Advanced Web-based Discord client with full feature set"""
    
    def __init__(self, bot):
        self.bot = bot
        self.app = None
        self.runner = None
        self.site = None
        self.websockets = set()
        self.running = False
        self.port = 8080
        
        # State management
        self.current_guild = None
        self.current_channel = None
        
        # Enhanced caching
        self.message_cache = defaultdict(list)
        self.cache_timestamps = defaultdict(float)
        self.last_api_call = defaultdict(float)
        self.member_cache = defaultdict(list)
        self.role_cache = defaultdict(list)
        self.emoji_cache = defaultdict(list)
        
        # Cache TTLs
        self.MESSAGE_CACHE_TTL = 120
        self.MEMBER_CACHE_TTL = 180
        self.ROLE_CACHE_TTL = 300
        self.EMOJI_CACHE_TTL = 300
        
        # Rate limiting
        self.API_COOLDOWN = 1.0
        self.active_loads = set()
        
        # Real-time updates
        self.last_updates = defaultdict(dict)
        
    async def start_server(self):
        """Start the web server"""
        if self.running:
            self.bot.logger.log(MODULE_NAME, "Web server already running", "WARNING")
            return
            
        self.app = aiohttp.web.Application()
        self._setup_routes()
        
        self.runner = aiohttp.web.AppRunner(self.app)
        await self.runner.setup()
        
        self.site = aiohttp.web.TCPSite(self.runner, 'localhost', self.port)
        await self.site.start()
        
        self.running = True
        self.bot.logger.log(MODULE_NAME, f"Advanced web client started on http://localhost:{self.port}")
        
        # Open browser automatically
        webbrowser.open(f'http://localhost:{self.port}')
        
    def _setup_routes(self):
        """Setup HTTP routes"""
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/ws', self.handle_websocket)
        self.app.router.add_post('/upload', self.handle_file_upload)
        
    async def handle_index(self, request):
        """Serve the main HTML page"""
        html_content = self._generate_html()
        return aiohttp.web.Response(text=html_content, content_type='text/html')
    
    async def handle_file_upload(self, request):
        """Handle file uploads"""
        try:
            data = await request.post()
            file_data = data['file'].file.read()
            filename = data['file'].filename
            channel_id = data['channel_id']
            
            # Forward to Discord
            channel = self.bot.get_channel(int(channel_id))
            if channel:
                file = discord.File(io.BytesIO(file_data), filename=filename)
                await channel.send(file=file)
                return aiohttp.web.Response(text=json.dumps({'success': True}))
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "File upload error", e)
        
        return aiohttp.web.Response(text=json.dumps({'success': False}))
    
    async def handle_websocket(self, request):
        """Handle WebSocket connections"""
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        
        self.websockets.add(ws)
        self.bot.logger.log(MODULE_NAME, "WebSocket client connected")
        
        try:
            # Send initial state
            await self.send_initial_state(ws)
            
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self.handle_websocket_message(msg.data, ws)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.bot.logger.log(MODULE_NAME, "WebSocket error", "ERROR")
                    
        finally:
            self.websockets.remove(ws)
            self.bot.logger.log(MODULE_NAME, "WebSocket client disconnected")
            
        return ws
    
    async def send_initial_state(self, ws):
        """Send initial state to new WebSocket client"""
        state = {
            'type': 'initial_state',
            'data': {
                'user': self._serialize_user(self.bot.user),
                'guilds': [self._serialize_guild(guild) for guild in self.bot.guilds],
                'current_guild': self._serialize_guild(self.current_guild) if self.current_guild else None,
                'current_channel': self._serialize_channel(self.current_channel) if self.current_channel else None
            }
        }
        await ws.send_json(state)
    
    async def handle_websocket_message(self, message_data, ws):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message_data)
            message_type = data.get('type')
            
            if message_type == 'select_guild':
                await self.handle_select_guild(data['guild_id'], ws)
            elif message_type == 'select_channel':
                await self.handle_select_channel(data['channel_id'], ws)
            elif message_type == 'send_message':
                await self.handle_send_message(data['content'], data.get('channel_id'), data.get('reply_to'))
            elif message_type == 'typing_start':
                await self.handle_typing_start(data.get('channel_id'))
            elif message_type == 'load_messages':
                await self.handle_load_messages(data.get('channel_id'), data.get('limit', 20), ws)
            elif message_type == 'create_dm':
                await self.handle_create_dm(data['user_id'], ws)
            elif message_type == 'load_dm_channels':
                await self.handle_load_dm_channels(ws)
            elif message_type == 'add_reaction':
                await self.handle_add_reaction(data['message_id'], data['emoji'], data.get('channel_id'))
            elif message_type == 'remove_reaction':
                await self.handle_remove_reaction(data['message_id'], data['emoji'], data.get('channel_id'))
            elif message_type == 'edit_message':
                await self.handle_edit_message(data['message_id'], data['content'], data.get('channel_id'))
            elif message_type == 'delete_message':
                await self.handle_delete_message(data['message_id'], data.get('channel_id'))
            elif message_type == 'get_user_profile':
                await self.handle_get_user_profile(data['user_id'], ws)
            elif message_type == 'moderate_user':
                await self.handle_moderate_user(data['user_id'], data['action'], data.get('reason'), data.get('duration'))
            elif message_type == 'update_bot_status':
                await self.handle_update_bot_status(data['status'], data.get('activity_type'), data.get('activity_text'))
            elif message_type == 'update_bot_profile':
                await self.handle_update_bot_profile(data.get('username'), data.get('avatar_data'))
            elif message_type == 'record_voice_message':
                await self.handle_record_voice_message(data['audio_data'], data.get('channel_id'))
            elif message_type == 'load_roles':
                await self.handle_load_roles(data.get('guild_id'), ws)
            elif message_type == 'load_emojis':
                await self.handle_load_emojis(data.get('guild_id'), ws)
                
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "WebSocket message handling error", e)
    
    async def handle_select_guild(self, guild_id, ws):
        """Handle guild selection with full data"""
        guild = self.bot.get_guild(int(guild_id))
        if guild:
            self.current_guild = guild
            self.current_channel = None
            
            # Send guild channels
            channels_data = {
                'text_channels': [self._serialize_channel(channel) for channel in guild.text_channels],
                'voice_channels': [self._serialize_channel(channel) for channel in guild.voice_channels],
                'categories': [self._serialize_channel(channel) for channel in guild.categories]
            }
            
            await ws.send_json({
                'type': 'guild_channels',
                'data': channels_data
            })
            
            # Load members with roles
            await self.handle_load_members(guild_id, ws)
            
            # Load roles
            await self.handle_load_roles(guild_id, ws)
            
            # Load emojis
            await self.handle_load_emojis(guild_id, ws)
    
    async def handle_load_members(self, guild_id, ws):
        """Load guild members with roles"""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return
            
        cache_key = f"members_{guild_id}"
        current_time = time.time()
        
        if (cache_key in self.member_cache and 
            current_time - self.cache_timestamps.get(cache_key, 0) < self.MEMBER_CACHE_TTL):
            members_data = self.member_cache[cache_key]
        else:
            # Load all members with their roles
            members_data = []
            for member in guild.members:
                member_data = self._serialize_member(member)
                member_data['roles'] = [self._serialize_role(role) for role in member.roles if role.name != "@everyone"]
                members_data.append(member_data)
            
            self.member_cache[cache_key] = members_data
            self.cache_timestamps[cache_key] = current_time
        
        await ws.send_json({
            'type': 'guild_members',
            'data': members_data
        })
    
    async def handle_load_roles(self, guild_id, ws):
        """Load guild roles"""
        guild = self.bot.get_guild(int(guild_id)) if guild_id else self.current_guild
        if not guild:
            return
            
        cache_key = f"roles_{guild.id}"
        current_time = time.time()
        
        if (cache_key in self.role_cache and 
            current_time - self.cache_timestamps.get(cache_key, 0) < self.ROLE_CACHE_TTL):
            roles_data = self.role_cache[cache_key]
        else:
            roles_data = [self._serialize_role(role) for role in guild.roles if role.name != "@everyone"]
            self.role_cache[cache_key] = roles_data
            self.cache_timestamps[cache_key] = current_time
        
        await ws.send_json({
            'type': 'guild_roles',
            'data': roles_data
        })
    
    async def handle_load_emojis(self, guild_id, ws):
        """Load guild emojis"""
        guild = self.bot.get_guild(int(guild_id)) if guild_id else self.current_guild
        if not guild:
            return
            
        cache_key = f"emojis_{guild.id}"
        current_time = time.time()
        
        if (cache_key in self.emoji_cache and 
            current_time - self.cache_timestamps.get(cache_key, 0) < self.EMOJI_CACHE_TTL):
            emojis_data = self.emoji_cache[cache_key]
        else:
            emojis_data = [self._serialize_emoji(emoji) for emoji in guild.emojis]
            self.emoji_cache[cache_key] = emojis_data
            self.cache_timestamps[cache_key] = current_time
        
        await ws.send_json({
            'type': 'guild_emojis',
            'data': emojis_data
        })
    
    async def handle_select_channel(self, channel_id, ws):
        """Handle channel selection"""
        channel = self.bot.get_channel(int(channel_id))
        if channel:
            self.current_channel = channel
            
            await ws.send_json({
                'type': 'channel_selected',
                'data': self._serialize_channel(channel)
            })
            
            # Load messages
            await self.handle_load_messages(channel_id, 20, ws)
            
            # Start real-time updates for this channel
            asyncio.create_task(self.start_realtime_updates(channel_id, ws))

    async def _fetch_messages(self, channel, limit):
        """Helper method to fetch messages in the bot's event loop"""
        messages = []
        try:
            async for message in channel.history(limit=limit):
                messages.append(self._serialize_message(message))
            messages.reverse()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error fetching messages", e)
        return messages            
    
    async def start_realtime_updates(self, channel_id, ws):
        """Start real-time message updates for a channel - FIXED VERSION"""
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            return
            
        while ws in self.websockets and self.current_channel and self.current_channel.id == channel.id:
            try:
                # Check for new messages every 3 seconds
                await asyncio.sleep(3)
                
                # Get latest messages using the bot's event loop
                def get_latest_messages_sync():
                    future = asyncio.run_coroutine_threadsafe(
                        self._fetch_messages(channel, 5), 
                        self.bot.loop
                    )
                    return future.result(timeout=30)
                
                messages = await asyncio.get_event_loop().run_in_executor(None, get_latest_messages_sync)
                
                # Check if we have new messages
                if messages and messages[0]['id'] != self.last_updates.get(channel_id, {}).get('last_message_id'):
                    latest_message = messages[0]
                    self.last_updates[channel_id] = {'last_message_id': latest_message['id']}
                    
                    # Send new message to client
                    await ws.send_json({
                        'type': 'message_create',
                        'data': latest_message
                    })
                    
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Real-time update error", e)
                break
    
    async def handle_send_message(self, content, channel_id=None, reply_to=None):
        """Handle sending a message - FIXED VERSION"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        
        if channel and isinstance(channel, (discord.TextChannel, discord.DMChannel)):
            try:
                # Handle replies
                reference = None
                if reply_to:
                    try:
                        # Use bot's event loop to fetch message
                        def fetch_reply_sync():
                            future = asyncio.run_coroutine_threadsafe(
                                channel.fetch_message(int(reply_to)),
                                self.bot.loop
                            )
                            return future.result(timeout=10)
                        
                        reference = await asyncio.get_event_loop().run_in_executor(None, fetch_reply_sync)
                    except:
                        pass
                
                # Use bot's event loop to send message
                def send_message_sync():
                    future = asyncio.run_coroutine_threadsafe(
                        self._send_channel_message(channel, content, reference),
                        self.bot.loop
                    )
                    return future.result(timeout=10)
                
                await asyncio.get_event_loop().run_in_executor(None, send_message_sync)
                
                self.bot.logger.log(MODULE_NAME, f"Sent message to {channel}")
                
                # Invalidate cache
                if str(channel.id) in self.message_cache:
                    del self.message_cache[str(channel.id)]
                
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to send message", e)

    async def handle_typing_start(self, channel_id=None):
        """Handle typing indicator - FIXED VERSION"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        if channel and isinstance(channel, (discord.TextChannel, discord.DMChannel)):
            try:
                # Use bot's event loop for typing operation
                def start_typing_sync():
                    future = asyncio.run_coroutine_threadsafe(
                        self._send_typing(channel),
                        self.bot.loop
                    )
                    return future.result(timeout=10)
                
                await asyncio.get_event_loop().run_in_executor(None, start_typing_sync)
                
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to start typing", e)

    async def _send_typing(self, channel):
        """Helper method to send typing indicator in bot's event loop"""
        async with channel.typing():
            await asyncio.sleep(5)  # Keep typing indicator for 5 seconds
    
    async def handle_load_messages(self, channel_id, limit, ws):
        """Load messages from a channel with enhanced caching - FIXED VERSION"""
        if not channel_id:
            return
            
        if channel_id in self.active_loads:
            return
            
        self.active_loads.add(channel_id)
        
        try:
            # Use the bot's event loop to run Discord operations
            def get_messages_sync():
                # This will be run in the bot's main thread
                channel = self.bot.get_channel(int(channel_id))
                if not channel or not isinstance(channel, (discord.TextChannel, discord.DMChannel)):
                    return None
                
                # Use asyncio.run_coroutine_threadsafe to run async code from sync context
                future = asyncio.run_coroutine_threadsafe(
                    self._fetch_messages(channel, min(limit, 50)), 
                    self.bot.loop
                )
                return future.result(timeout=30)
            
            # Run in thread to avoid blocking
            messages = await asyncio.get_event_loop().run_in_executor(None, get_messages_sync)
            
            if messages is None:
                return
            
            # Cache the results
            current_time = time.time()
            self.message_cache[channel_id] = messages
            self.cache_timestamps[channel_id] = current_time
            
            await ws.send_json({
                'type': 'messages_loaded',
                'data': {
                    'channel_id': channel_id,
                    'messages': messages
                }
            })
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load messages", e)
        finally:
            self.active_loads.discard(channel_id)

    async def handle_create_dm(self, user_id, ws):
        """Create or get DM channel"""
        user = self.bot.get_user(int(user_id))
        if user:
            try:
                channel = user.dm_channel or await user.create_dm()
                self.current_channel = channel
                
                await ws.send_json({
                    'type': 'dm_channel_created',
                    'data': self._serialize_channel(channel)
                })
                
                # Load messages from DM
                await self.handle_load_messages(str(channel.id), 20, ws)
                
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to create DM", e)
    
    async def handle_load_dm_channels(self, ws):
        """Load all DM channels"""
        dm_channels = []
        for channel in self.bot.private_channels:
            if isinstance(channel, discord.DMChannel):
                dm_data = self._serialize_channel(channel)
                # Get recipient info
                if channel.recipient:
                    dm_data['recipient'] = self._serialize_user(channel.recipient)
                dm_channels.append(dm_data)
        
        await ws.send_json({
            'type': 'dm_channels_loaded',
            'data': dm_channels
        })
    
    async def handle_add_reaction(self, message_id, emoji, channel_id=None):
        """Add reaction to a message"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        if channel:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.add_reaction(emoji)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to add reaction", e)
    
    async def handle_remove_reaction(self, message_id, emoji, channel_id=None):
        """Remove reaction from a message"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        if channel:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.remove_reaction(emoji, self.bot.user)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to remove reaction", e)
    
    async def handle_edit_message(self, message_id, content, channel_id=None):
        """Edit a message"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        if channel:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(content=content)
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to edit message", e)
    
    async def handle_delete_message(self, message_id, channel_id=None):
        """Delete a message"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        if channel:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.delete()
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to delete message", e)
    
    async def handle_get_user_profile(self, user_id, ws):
        """Get detailed user profile"""
        user = self.bot.get_user(int(user_id))
        if user:
            profile_data = {
                'user': self._serialize_user(user),
                'mutual_guilds': [],
                'relationship': 'none'
            }
            
            # Get mutual guilds
            for guild in self.bot.guilds:
                if guild.get_member(user.id):
                    profile_data['mutual_guilds'].append(self._serialize_guild(guild))
            
            await ws.send_json({
                'type': 'user_profile',
                'data': profile_data
            })
    
    async def handle_moderate_user(self, user_id, action, reason=None, duration=None):
        """Moderate a user (ban, kick, mute, timeout)"""
        if not self.current_guild:
            return
            
        member = self.current_guild.get_member(int(user_id))
        if not member:
            return
            
        try:
            if action == 'ban':
                await member.ban(reason=reason)
            elif action == 'kick':
                await member.kick(reason=reason)
            elif action == 'mute':
                # Server mute (voice)
                await member.edit(mute=True)
            elif action == 'deafen':
                # Server deafen (voice)
                await member.edit(deafen=True)
            elif action == 'timeout':
                # Timeout for duration (in minutes)
                if duration:
                    from datetime import timedelta
                    timeout_duration = timedelta(minutes=int(duration))
                    await member.timeout(timeout_duration, reason=reason)
            
            self.bot.logger.log(MODULE_NAME, f"Performed {action} on {member}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to {action} user", e)
    
    async def handle_update_bot_status(self, status, activity_type=None, activity_text=None):
        """Update bot status and activity"""
        try:
            # Update status
            discord_status = getattr(discord.Status, status, discord.Status.online)
            await self.bot.change_presence(status=discord_status)
            
            # Update activity
            if activity_type and activity_text:
                activity_map = {
                    'playing': discord.Game,
                    'streaming': discord.Streaming,
                    'listening': discord.ActivityType.listening,
                    'watching': discord.ActivityType.watching
                }
                
                activity_class = activity_map.get(activity_type, discord.Game)
                if activity_type == 'streaming':
                    activity = discord.Streaming(name=activity_text, url="https://twitch.tv/example")
                else:
                    activity = discord.Game(name=activity_text)
                
                await self.bot.change_presence(activity=activity)
            
            self.bot.logger.log(MODULE_NAME, f"Updated bot status to {status}")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to update bot status", e)
    
    async def handle_update_bot_profile(self, username=None, avatar_data=None):
        """Update bot username and avatar"""
        try:
            kwargs = {}
            if username:
                kwargs['username'] = username
            if avatar_data:
                # Convert base64 to bytes
                avatar_bytes = base64.b64decode(avatar_data.split(',')[1])
                kwargs['avatar'] = avatar_bytes
            
            if kwargs:
                await self.bot.user.edit(**kwargs)
                self.bot.logger.log(MODULE_NAME, "Updated bot profile")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to update bot profile", e)
    
    async def handle_record_voice_message(self, audio_data, channel_id=None):
        """Handle voice message recording and sending"""
        channel = self.bot.get_channel(int(channel_id)) if channel_id else self.current_channel
        if not channel:
            return
            
        try:
            # Convert base64 audio to file
            audio_bytes = base64.b64decode(audio_data.split(',')[1])
            file = discord.File(io.BytesIO(audio_bytes), filename="voice-message.ogg")
            
            await channel.send(file=file)
            self.bot.logger.log(MODULE_NAME, "Sent voice message")
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send voice message", e)
    
    async def broadcast(self, data):
        """Broadcast data to all connected WebSocket clients"""
        if self.websockets:
            for ws in self.websockets.copy():
                try:
                    await ws.send_json(data)
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, "Failed to send WebSocket message", e)
                    self.websockets.remove(ws)
    
    # Enhanced Serialization Methods
    def _serialize_user(self, user):
        if not user:
            return None
        return {
            'id': str(user.id),
            'username': user.name,
            'discriminator': user.discriminator,
            'display_name': user.display_name,
            'avatar_url': str(user.avatar.url) if user.avatar else None,
            'bot': user.bot,
            'created_at': user.created_at.isoformat() if user.created_at else None
        }
    
    def _serialize_guild(self, guild):
        if not guild:
            return None
        return {
            'id': str(guild.id),
            'name': guild.name,
            'icon_url': str(guild.icon.url) if guild.icon else None,
            'banner_url': str(guild.banner.url) if guild.banner else None,
            'member_count': guild.member_count,
            'owner_id': str(guild.owner_id) if guild.owner_id else None,
            'description': guild.description,
            'features': guild.features
        }
    
    def _serialize_channel(self, channel):
        if not channel:
            return None
            
        data = {
            'id': str(channel.id),
            'name': getattr(channel, 'name', 'DM'),
            'type': str(channel.type),
            'position': getattr(channel, 'position', 0)
        }
        
        if isinstance(channel, discord.TextChannel):
            data['topic'] = channel.topic
            data['nsfw'] = channel.nsfw
            data['slowmode_delay'] = channel.slowmode_delay
            data['category_id'] = str(channel.category_id) if channel.category_id else None
        
        elif isinstance(channel, discord.DMChannel):
            data['recipient'] = self._serialize_user(channel.recipient) if channel.recipient else None
        
        return data
    
    def _serialize_member(self, member):
        status = str(member.status)
        status_class = 'offline'
        if status == 'online':
            status_class = 'online'
        elif status == 'idle':
            status_class = 'idle'
        elif status == 'dnd':
            status_class = 'dnd'
        
        return {
            'id': str(member.id),
            'username': member.name,
            'display_name': member.display_name,
            'avatar_url': str(member.avatar.url) if member.avatar else None,
            'bot': member.bot,
            'status': status_class,
            'joined_at': member.joined_at.isoformat() if member.joined_at else None,
            'activities': [self._serialize_activity(activity) for activity in member.activities if activity]
        }
    
    def _serialize_role(self, role):
        return {
            'id': str(role.id),
            'name': role.name,
            'color': role.color.value,
            'position': role.position,
            'permissions': role.permissions.value,
            'hoist': role.hoist,
            'mentionable': role.mentionable
        }
    
    def _serialize_emoji(self, emoji):
        return {
            'id': str(emoji.id),
            'name': emoji.name,
            'url': str(emoji.url),
            'animated': emoji.animated,
            'available': emoji.available
        }
    
    def _serialize_activity(self, activity):
        return {
            'type': str(activity.type),
            'name': activity.name,
            'details': getattr(activity, 'details', None),
            'url': getattr(activity, 'url', None)
        }
    
    def _serialize_message(self, message):
        data = {
            'id': str(message.id),
            'content': message.content,
            'author': self._serialize_user(message.author),
            'timestamp': message.created_at.isoformat(),
            'edited_timestamp': message.edited_at.isoformat() if message.edited_at else None,
            'attachments': [self._serialize_attachment(attachment) for attachment in message.attachments],
            'embeds': [self._serialize_embed(embed) for embed in message.embeds],
            'reactions': [self._serialize_reaction(reaction) for reaction in message.reactions],
            'mention_everyone': message.mention_everyone,
            'mentions': [self._serialize_user(user) for user in message.mentions],
            'channel_id': str(message.channel.id),
            'pinned': message.pinned,
            'type': str(message.type)
        }
        
        # Add reply reference if exists
        if message.reference and message.reference.message_id:
            data['reply_to'] = str(message.reference.message_id)
        
        return data
    
    def _serialize_attachment(self, attachment):
        data = {
            'id': str(attachment.id),
            'filename': attachment.filename,
            'url': attachment.url,
            'size': attachment.size,
            'content_type': getattr(attachment, 'content_type', None)
        }
        
        # Check if it's an image
        if attachment.content_type and attachment.content_type.startswith('image/'):
            data['is_image'] = True
            data['width'] = getattr(attachment, 'width', None)
            data['height'] = getattr(attachment, 'height', None)
        
        return data
    
    def _serialize_embed(self, embed):
        return {
            'title': embed.title,
            'description': embed.description,
            'url': embed.url,
            'color': embed.color.value if embed.color else None,
            'timestamp': embed.timestamp.isoformat() if embed.timestamp else None,
            'fields': [{'name': field.name, 'value': field.value, 'inline': field.inline} for field in embed.fields],
            'thumbnail': {'url': embed.thumbnail.url} if embed.thumbnail else None,
            'image': {'url': embed.image.url} if embed.image else None,
            'footer': {'text': embed.footer.text, 'icon_url': embed.footer.icon_url} if embed.footer else None,
            'author': {'name': embed.author.name, 'url': embed.author.url, 'icon_url': embed.author.icon_url} if embed.author else None
        }
    
    def _serialize_reaction(self, reaction):
        return {
            'emoji': self._serialize_emoji(reaction.emoji) if hasattr(reaction.emoji, 'id') else str(reaction.emoji),
            'count': reaction.count,
            'me': reaction.me
        }
    
    def _generate_html(self):
            """Generate the complete advanced HTML interface"""
            
            html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Advanced Discord Web Client</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #36393f;
                color: #dcddde;
                overflow: hidden;
            }

            .app {
                display: flex;
                height: 100vh;
            }

            /* Server List */
            .server-list {
                width: 72px;
                background: #202225;
                display: flex;
                flex-direction: column;
                align-items: center;
                padding: 12px 0;
                overflow-y: auto;
            }

            .server-icon {
                width: 48px;
                height: 48px;
                border-radius: 50%;
                background: #36393f;
                margin-bottom: 8px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: all 0.2s;
                position: relative;
            }

            .server-icon:hover {
                border-radius: 16px;
                background: #5865f2;
            }

            .server-icon.active {
                border-radius: 16px;
                background: #5865f2;
            }

            .server-icon.active::before {
                content: '';
                position: absolute;
                left: -12px;
                width: 4px;
                height: 40px;
                background: #fff;
                border-radius: 0 4px 4px 0;
            }

            .server-icon img {
                width: 100%;
                height: 100%;
                border-radius: inherit;
            }

            .server-icon-text {
                font-size: 18px;
                font-weight: 600;
                color: #fff;
            }

            .home-icon {
                background: #5865f2;
                margin-bottom: 16px;
            }

            .add-server {
                color: #3ba55d;
                font-size: 28px;
            }

            /* Channel Sidebar */
            .channel-sidebar {
                width: 240px;
                background: #2f3136;
                display: flex;
                flex-direction: column;
            }

            .guild-header {
                height: 48px;
                padding: 0 16px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                border-bottom: 1px solid #202225;
                cursor: pointer;
                font-weight: 600;
            }

            .guild-header:hover {
                background: #36393f;
            }

            .channels {
                flex: 1;
                overflow-y: auto;
                padding: 8px;
            }

            .channel-category {
                padding: 8px 4px;
                font-size: 12px;
                font-weight: 600;
                color: #8e9297;
                text-transform: uppercase;
                cursor: pointer;
                display: flex;
                align-items: center;
            }

            .channel-category-arrow {
                margin-right: 4px;
                transition: transform 0.2s;
            }

            .channel-category.collapsed .channel-category-arrow {
                transform: rotate(-90deg);
            }

            .channel-item {
                padding: 6px 8px;
                margin: 2px 0;
                border-radius: 4px;
                cursor: pointer;
                display: flex;
                align-items: center;
                color: #96989d;
            }

            .channel-item:hover {
                background: #36393f;
                color: #dcddde;
            }

            .channel-item.active {
                background: #42454a;
                color: #fff;
            }

            .channel-icon {
                margin-right: 8px;
                opacity: 0.6;
            }

            .dm-list {
                padding: 8px;
            }

            .dm-item {
                padding: 8px;
                margin: 2px 0;
                border-radius: 4px;
                cursor: pointer;
                display: flex;
                align-items: center;
            }

            .dm-item:hover {
                background: #36393f;
            }

            .dm-avatar {
                width: 32px;
                height: 32px;
                border-radius: 50%;
                margin-right: 12px;
            }

            /* Main Content */
            .main-content {
                flex: 1;
                display: flex;
                flex-direction: column;
            }

            .channel-header {
                height: 48px;
                padding: 0 16px;
                display: flex;
                align-items: center;
                border-bottom: 1px solid #202225;
                font-weight: 600;
            }

            .channel-name {
                flex: 1;
            }

            .channel-controls {
                display: flex;
                gap: 16px;
            }

            .icon-btn {
                background: none;
                border: none;
                color: #b9bbbe;
                cursor: pointer;
                font-size: 18px;
                padding: 4px;
            }

            .icon-btn:hover {
                color: #dcddde;
            }

            .messages {
                flex: 1;
                overflow-y: auto;
                padding: 16px;
            }

            .message {
                display: flex;
                padding: 4px 0;
                margin: 8px 0;
            }

            .message:hover {
                background: #32353b;
            }

            .message-avatar {
                width: 40px;
                height: 40px;
                border-radius: 50%;
                margin-right: 16px;
                flex-shrink: 0;
            }

            .message-content {
                flex: 1;
            }

            .message-header {
                display: flex;
                align-items: baseline;
                margin-bottom: 4px;
            }

            .message-author {
                font-weight: 600;
                margin-right: 8px;
                cursor: pointer;
            }

            .message-author:hover {
                text-decoration: underline;
            }

            .message-timestamp {
                font-size: 12px;
                color: #72767d;
            }

            .message-text {
                color: #dcddde;
                line-height: 1.375;
                word-wrap: break-word;
            }

            .message-attachment {
                margin-top: 8px;
                max-width: 400px;
            }

            .message-attachment img {
                max-width: 100%;
                border-radius: 4px;
                cursor: pointer;
            }

            .message-embed {
                margin-top: 8px;
                padding: 8px 12px;
                border-left: 4px solid #5865f2;
                background: #2f3136;
                border-radius: 4px;
                max-width: 520px;
            }

            .embed-title {
                font-weight: 600;
                margin-bottom: 4px;
            }

            .embed-description {
                font-size: 14px;
                color: #dcddde;
            }

            .message-reactions {
                display: flex;
                gap: 4px;
                margin-top: 8px;
            }

            .reaction {
                padding: 4px 8px;
                background: #2f3136;
                border-radius: 4px;
                cursor: pointer;
                display: flex;
                align-items: center;
                gap: 4px;
                border: 1px solid transparent;
            }

            .reaction:hover {
                border-color: #dcddde;
            }

            .reaction.reacted {
                background: #5865f220;
                border-color: #5865f2;
            }

            .message-actions {
                position: absolute;
                right: 16px;
                background: #18191c;
                border-radius: 4px;
                padding: 4px;
                display: none;
                gap: 4px;
                box-shadow: 0 0 8px rgba(0,0,0,0.3);
            }

            .message:hover .message-actions {
                display: flex;
            }

            /* Message Input */
            .message-input-container {
                padding: 16px;
            }

            .message-input-wrapper {
                background: #40444b;
                border-radius: 8px;
                padding: 12px;
            }

            .message-input {
                width: 100%;
                background: none;
                border: none;
                color: #dcddde;
                font-size: 15px;
                outline: none;
                resize: none;
                font-family: inherit;
            }

            .message-input-tools {
                display: flex;
                gap: 8px;
                margin-top: 8px;
            }

            .file-input {
                display: none;
            }

            /* Member Sidebar */
            .member-sidebar {
                width: 240px;
                background: #2f3136;
                overflow-y: auto;
                padding: 8px;
            }

            .member-group {
                margin: 16px 0;
            }

            .member-group-title {
                font-size: 12px;
                font-weight: 600;
                color: #8e9297;
                text-transform: uppercase;
                padding: 8px 4px;
            }

            .member-item {
                padding: 4px 8px;
                margin: 2px 0;
                border-radius: 4px;
                cursor: pointer;
                display: flex;
                align-items: center;
            }

            .member-item:hover {
                background: #36393f;
            }

            .member-avatar {
                width: 32px;
                height: 32px;
                border-radius: 50%;
                margin-right: 12px;
                position: relative;
            }

            .member-status {
                width: 10px;
                height: 10px;
                border-radius: 50%;
                position: absolute;
                bottom: 0;
                right: 0;
                border: 2px solid #2f3136;
            }

            .member-status.online { background: #3ba55d; }
            .member-status.idle { background: #faa61a; }
            .member-status.dnd { background: #ed4245; }
            .member-status.offline { background: #747f8d; }

            .member-name {
                flex: 1;
            }

            .member-roles {
                display: flex;
                gap: 4px;
                margin-top: 2px;
            }

            .role-badge {
                font-size: 10px;
                padding: 2px 6px;
                border-radius: 3px;
                font-weight: 600;
            }

            /* Modal */
            .modal-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0,0,0,0.85);
                align-items: center;
                justify-content: center;
                z-index: 1000;
            }

            .modal-overlay.active {
                display: flex;
            }

            .modal {
                background: #36393f;
                border-radius: 8px;
                padding: 24px;
                max-width: 500px;
                width: 90%;
                max-height: 80vh;
                overflow-y: auto;
            }

            .modal-header {
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 16px;
            }

            .modal-close {
                float: right;
                cursor: pointer;
                font-size: 24px;
                color: #b9bbbe;
            }

            .modal-close:hover {
                color: #dcddde;
            }

            .profile-header {
                text-align: center;
                margin-bottom: 24px;
            }

            .profile-avatar-large {
                width: 100px;
                height: 100px;
                border-radius: 50%;
                margin-bottom: 16px;
            }

            .profile-username {
                font-size: 24px;
                font-weight: 600;
            }

            .profile-section {
                margin: 16px 0;
            }

            .profile-section-title {
                font-size: 12px;
                font-weight: 600;
                color: #8e9297;
                text-transform: uppercase;
                margin-bottom: 8px;
            }

            .moderation-controls {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }

            .mod-button {
                padding: 10px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-weight: 600;
                font-size: 14px;
            }

            .mod-button.danger {
                background: #ed4245;
                color: #fff;
            }

            .mod-button.warning {
                background: #faa61a;
                color: #fff;
            }

            .mod-button:hover {
                opacity: 0.8;
            }

            /* Bot Controls */
            .bot-controls {
                position: fixed;
                bottom: 0;
                left: 72px;
                width: 240px;
                background: #292b2f;
                padding: 8px;
                border-top: 1px solid #202225;
            }

            .bot-user {
                display: flex;
                align-items: center;
                padding: 8px;
                border-radius: 4px;
                cursor: pointer;
            }

            .bot-user:hover {
                background: #36393f;
            }

            .bot-avatar {
                width: 32px;
                height: 32px;
                border-radius: 50%;
                margin-right: 12px;
            }

            .bot-info {
                flex: 1;
            }

            .bot-username {
                font-weight: 600;
                font-size: 14px;
            }

            .bot-status {
                font-size: 12px;
                color: #b9bbbe;
            }

            .status-controls {
                display: flex;
                gap: 8px;
                margin-top: 8px;
            }

            .status-btn {
                padding: 6px 12px;
                border: none;
                border-radius: 4px;
                background: #40444b;
                color: #dcddde;
                cursor: pointer;
                font-size: 12px;
            }

            .status-btn:hover {
                background: #4f545c;
            }

            /* Voice Recorder */
            .voice-recorder {
                display: none;
                padding: 12px;
                background: #ed4245;
                border-radius: 8px;
                margin-bottom: 8px;
                align-items: center;
                gap: 12px;
            }

            .voice-recorder.active {
                display: flex;
            }

            .voice-timer {
                flex: 1;
                font-weight: 600;
            }

            .voice-wave {
                width: 100px;
                height: 30px;
                background: repeating-linear-gradient(
                    to right,
                    #fff 0px,
                    #fff 2px,
                    transparent 2px,
                    transparent 6px
                );
                animation: wave 1s linear infinite;
            }

            @keyframes wave {
                0% { transform: translateX(0); }
                100% { transform: translateX(4px); }
            }

            /* Emoji Picker */
            .emoji-picker {
                display: none;
                position: absolute;
                bottom: 100%;
                right: 16px;
                background: #36393f;
                border-radius: 8px;
                padding: 16px;
                box-shadow: 0 0 16px rgba(0,0,0,0.3);
                width: 320px;
                max-height: 400px;
                overflow-y: auto;
            }

            .emoji-picker.active {
                display: block;
            }

            .emoji-grid {
                display: grid;
                grid-template-columns: repeat(8, 1fr);
                gap: 8px;
            }

            .emoji {
                font-size: 24px;
                cursor: pointer;
                padding: 4px;
                border-radius: 4px;
            }

            .emoji:hover {
                background: #40444b;
            }

            /* Scrollbar */
            ::-webkit-scrollbar {
                width: 8px;
            }

            ::-webkit-scrollbar-track {
                background: #2f3136;
            }

            ::-webkit-scrollbar-thumb {
                background: #202225;
                border-radius: 4px;
            }

            ::-webkit-scrollbar-thumb:hover {
                background: #18191c;
            }

            /* Loading */
            .loading {
                text-align: center;
                padding: 20px;
                color: #b9bbbe;
            }

            .typing-indicator {
                display: none;
                padding: 8px 16px;
                color: #b9bbbe;
                font-size: 13px;
            }

            .typing-indicator.active {
                display: block;
            }

            .typing-dots {
                display: inline-block;
                animation: typing 1.4s infinite;
            }

            @keyframes typing {
                0%, 60%, 100% { opacity: 0.4; }
                30% { opacity: 1; }
            }
        </style>
    </head>
    <body>
        <div class="app">
            <!-- Server List -->
            <div class="server-list">
                <div class="server-icon home-icon" data-guild-id="home" title="Home">
                    <span class="server-icon-text">🏠</span>
                </div>
                <div id="serverList"></div>
                <div class="server-icon add-server" title="Add Server">
                    <span>+</span>
                </div>
            </div>

            <!-- Channel Sidebar -->
            <div class="channel-sidebar">
                <div class="guild-header" id="guildHeader">
                    <span id="guildName">Select a Server</span>
                    <span>▼</span>
                </div>
                <div class="channels" id="channelList"></div>
                <div class="bot-controls">
                    <div class="bot-user" id="botUser">
                        <img class="bot-avatar" id="botAvatar" src="" alt="Bot">
                        <div class="bot-info">
                            <div class="bot-username" id="botUsername">Bot</div>
                            <div class="bot-status" id="botStatus">Online</div>
                        </div>
                        <span>⚙️</span>
                    </div>
                </div>
            </div>

            <!-- Main Content -->
            <div class="main-content">
                <div class="channel-header">
                    <div class="channel-name">
                        <span id="currentChannelName"># Select a channel</span>
                    </div>
                    <div class="channel-controls">
                        <button class="icon-btn" id="toggleMembers" title="Members">👥</button>
                        <button class="icon-btn" id="searchMessages" title="Search">🔍</button>
                        <button class="icon-btn" id="channelSettings" title="Settings">⚙️</button>
                    </div>
                </div>
                
                <div class="messages" id="messages">
                    <div class="loading">Select a channel to view messages</div>
                </div>

                <div class="typing-indicator" id="typingIndicator">
                    <span class="typing-dots">...</span> typing
                </div>

                <div class="voice-recorder" id="voiceRecorder">
                    <button class="icon-btn" id="stopRecording">⏹️</button>
                    <div class="voice-timer" id="voiceTimer">0:00</div>
                    <div class="voice-wave"></div>
                </div>

                <div class="message-input-container">
                    <div class="message-input-wrapper">
                        <textarea class="message-input" id="messageInput" placeholder="Type a message..." rows="1"></textarea>
                        <div class="message-input-tools">
                            <button class="icon-btn" id="uploadFile" title="Upload File">📎</button>
                            <input type="file" class="file-input" id="fileInput">
                            <button class="icon-btn" id="recordVoice" title="Voice Message">🎤</button>
                            <button class="icon-btn" id="showEmojis" title="Emoji">😊</button>
                            <button class="icon-btn" id="sendMessage" title="Send">➤</button>
                        </div>
                    </div>
                    <div class="emoji-picker" id="emojiPicker">
                        <div class="emoji-grid" id="emojiGrid"></div>
                    </div>
                </div>
            </div>

            <!-- Member Sidebar -->
            <div class="member-sidebar" id="memberSidebar">
                <div id="memberList"></div>
            </div>
        </div>

        <!-- Modals -->
        <div class="modal-overlay" id="profileModal">
            <div class="modal">
                <span class="modal-close" onclick="closeModal('profileModal')">×</span>
                <div class="modal-header">User Profile</div>
                <div class="profile-header">
                    <img class="profile-avatar-large" id="profileAvatar" src="" alt="User">
                    <div class="profile-username" id="profileUsername">Username</div>
                </div>
                <div class="profile-section">
                    <div class="profile-section-title">Mutual Servers</div>
                    <div id="mutualGuilds"></div>
                </div>
                <div class="profile-section" id="moderationSection" style="display: none;">
                    <div class="profile-section-title">Moderation</div>
                    <div class="moderation-controls">
                        <button class="mod-button warning" onclick="moderateUser('timeout')">Timeout</button>
                        <button class="mod-button warning" onclick="moderateUser('kick')">Kick</button>
                        <button class="mod-button danger" onclick="moderateUser('ban')">Ban</button>
                    </div>
                </div>
            </div>
        </div>

        <div class="modal-overlay" id="botSettingsModal">
            <div class="modal">
                <span class="modal-close" onclick="closeModal('botSettingsModal')">×</span>
                <div class="modal-header">Bot Settings</div>
                <div class="profile-section">
                    <div class="profile-section-title">Status</div>
                    <div class="status-controls">
                        <button class="status-btn" onclick="updateBotStatus('online')">🟢 Online</button>
                        <button class="status-btn" onclick="updateBotStatus('idle')">🟡 Idle</button>
                        <button class="status-btn" onclick="updateBotStatus('dnd')">🔴 DND</button>
                        <button class="status-btn" onclick="updateBotStatus('invisible')">⚫ Invisible</button>
                    </div>
                </div>
                <div class="profile-section">
                    <div class="profile-section-title">Activity</div>
                    <input type="text" id="activityText" placeholder="Activity text" style="width: 100%; padding: 8px; background: #40444b; border: none; color: #dcddde; border-radius: 4px;">
                    <select id="activityType" style="width: 100%; padding: 8px; margin-top: 8px; background: #40444b; border: none; color: #dcddde; border-radius: 4px;">
                        <option value="playing">Playing</option>
                        <option value="watching">Watching</option>
                        <option value="listening">Listening</option>
                        <option value="streaming">Streaming</option>
                    </select>
                    <button class="mod-button warning" style="margin-top: 8px; width: 100%;" onclick="updateBotActivity()">Update Activity</button>
                </div>
            </div>
        </div>

        <script>
            // WebSocket connection
            let ws = null;
            let currentGuildId = null;
            let currentChannelId = null;
            let currentUserId = null;
            let guilds = [];
            let channels = [];
            let members = [];
            let messages = [];
            let mediaRecorder = null;
            let recordingChunks = [];

            // Initialize WebSocket
            function initWebSocket() {
                ws = new WebSocket('ws://localhost:8080/ws');

                ws.onopen = () => {
                    console.log('Connected to Discord bot');
                };

                ws.onmessage = (event) => {
                    const data = JSON.parse(event.data);
                    handleWebSocketMessage(data);
                };

                ws.onclose = () => {
                    console.log('Disconnected from Discord bot');
                    setTimeout(initWebSocket, 3000);
                };

                ws.onerror = (error) => {
                    console.error('WebSocket error:', error);
                };
            }

            // Handle WebSocket messages
            function handleWebSocketMessage(data) {
                switch(data.type) {
                    case 'initial_state':
                        handleInitialState(data.data);
                        break;
                    case 'guild_channels':
                        displayChannels(data.data);
                        break;
                    case 'guild_members':
                        displayMembers(data.data);
                        break;
                    case 'guild_roles':
                        // Store roles for later use
                        break;
                    case 'guild_emojis':
                        displayEmojis(data.data);
                        break;
                    case 'messages_loaded':
                        displayMessages(data.data.messages);
                        break;
                    case 'message_create':
                        addMessage(data.data);
                        break;
                    case 'channel_selected':
                        // Channel selected confirmation
                        break;
                    case 'dm_channels_loaded':
                        displayDMChannels(data.data);
                        break;
                    case 'user_profile':
                        displayUserProfile(data.data);
                        break;
                }
            }

            // Handle initial state
            function handleInitialState(state) {
                guilds = state.guilds;
                
                // Display bot user
                if (state.user) {
                    document.getElementById('botUsername').textContent = state.user.username;
                    if (state.user.avatar_url) {
                        document.getElementById('botAvatar').src = state.user.avatar_url;
                    }
                }

                // Display servers
                displayServers();
            }

            // Display servers
            function displayServers() {
                const serverList = document.getElementById('serverList');
                serverList.innerHTML = '';

                guilds.forEach(guild => {
                    const serverIcon = document.createElement('div');
                    serverIcon.className = 'server-icon';
                    serverIcon.dataset.guildId = guild.id;
                    serverIcon.title = guild.name;
                    serverIcon.onclick = () => selectGuild(guild.id);

                    if (guild.icon_url) {
                        const img = document.createElement('img');
                        img.src = guild.icon_url;
                        serverIcon.appendChild(img);
                    } else {
                        const text = document.createElement('span');
                        text.className = 'server-icon-text';
                        text.textContent = guild.name.substring(0, 2).toUpperCase();
                        serverIcon.appendChild(text);
                    }

                    serverList.appendChild(serverIcon);
                });
            }

            // Select guild
            function selectGuild(guildId) {
                if (guildId === 'home') {
                    loadDMChannels();
                    return;
                }

                currentGuildId = guildId;
                const guild = guilds.find(g => g.id === guildId);
                
                if (guild) {
                    document.getElementById('guildName').textContent = guild.name;
                }

                // Update active state
                document.querySelectorAll('.server-icon').forEach(icon => {
                    icon.classList.remove('active');
                });
                document.querySelector(`[data-guild-id="${guildId}"]`).classList.add('active');

                // Request guild data
                sendWebSocket({
                    type: 'select_guild',
                    guild_id: guildId
                });
            }

            // Display channels
            function displayChannels(data) {
                const channelList = document.getElementById('channelList');
                channelList.innerHTML = '';

                // Group channels by category
                const categories = {};
                data.text_channels.forEach(channel => {
                    const categoryId = channel.category_id || 'uncategorized';
                    if (!categories[categoryId]) {
                        categories[categoryId] = [];
                    }
                    categories[categoryId].push(channel);
                });

                // Display categories and channels
                for (const [categoryId, channelGroup] of Object.entries(categories)) {
                    if (categoryId !== 'uncategorized') {
                        const category = data.categories.find(c => c.id === categoryId);
                        const categoryDiv = document.createElement('div');
                        categoryDiv.className = 'channel-category';
                        categoryDiv.innerHTML = `
                            <span class="channel-category-arrow">▼</span>
                            ${category ? category.name : 'Category'}
                        `;
                        channelList.appendChild(categoryDiv);
                    }

                    channelGroup.forEach(channel => {
                        const channelDiv = document.createElement('div');
                        channelDiv.className = 'channel-item';
                        channelDiv.dataset.channelId = channel.id;
                        channelDiv.onclick = () => selectChannel(channel.id);
                        channelDiv.innerHTML = `
                            <span class="channel-icon">#</span>
                            ${channel.name}
                        `;
                        channelList.appendChild(channelDiv);
                    });
                }
            }

            // Select channel
            function selectChannel(channelId) {
                currentChannelId = channelId;

                // Update active state
                document.querySelectorAll('.channel-item').forEach(item => {
                    item.classList.remove('active');
                });
                document.querySelector(`[data-channel-id="${channelId}"]`).classList.add('active');

                // Update channel name
                const channel = channels.find(c => c.id === channelId);
                if (channel) {
                    document.getElementById('currentChannelName').textContent = `# ${channel.name}`;
                }

                // Clear messages
                document.getElementById('messages').innerHTML = '<div class="loading">Loading messages...</div>';

                // Request channel data
                sendWebSocket({
                    type: 'select_channel',
                    channel_id: channelId
                });
            }

            // Display messages
            function displayMessages(msgs) {
                messages = msgs;
                const messagesDiv = document.getElementById('messages');
                messagesDiv.innerHTML = '';

                msgs.forEach(msg => {
                    const messageDiv = createMessageElement(msg);
                    messagesDiv.appendChild(messageDiv);
                });

                // Scroll to bottom
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            }

            // Create message element
            function createMessageElement(msg) {
                const messageDiv = document.createElement('div');
                messageDiv.className = 'message';
                messageDiv.dataset.messageId = msg.id;

                const avatar = document.createElement('img');
                avatar.className = 'message-avatar';
                avatar.src = msg.author.avatar_url || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="%235865f2"/></svg>';
                
                const content = document.createElement('div');
                content.className = 'message-content';

                const header = document.createElement('div');
                header.className = 'message-header';

                const author = document.createElement('span');
                author.className = 'message-author';
                author.textContent = msg.author.display_name || msg.author.username;
                author.onclick = () => showUserProfile(msg.author.id);

                const timestamp = document.createElement('span');
                timestamp.className = 'message-timestamp';
                timestamp.textContent = formatTimestamp(msg.timestamp);

                header.appendChild(author);
                header.appendChild(timestamp);

                const text = document.createElement('div');
                text.className = 'message-text';
                text.textContent = msg.content;

                content.appendChild(header);
                content.appendChild(text);

                // Add attachments
                if (msg.attachments && msg.attachments.length > 0) {
                    msg.attachments.forEach(att => {
                        const attDiv = document.createElement('div');
                        attDiv.className = 'message-attachment';
                        
                        if (att.is_image) {
                            const img = document.createElement('img');
                            img.src = att.url;
                            img.onclick = () => window.open(att.url, '_blank');
                            attDiv.appendChild(img);
                        } else {
                            const link = document.createElement('a');
                            link.href = att.url;
                            link.target = '_blank';
                            link.textContent = `📎 ${att.filename}`;
                            attDiv.appendChild(link);
                        }
                        
                        content.appendChild(attDiv);
                    });
                }

                // Add embeds
                if (msg.embeds && msg.embeds.length > 0) {
                    msg.embeds.forEach(embed => {
                        const embedDiv = document.createElement('div');
                        embedDiv.className = 'message-embed';
                        
                        if (embed.title) {
                            const title = document.createElement('div');
                            title.className = 'embed-title';
                            title.textContent = embed.title;
                            embedDiv.appendChild(title);
                        }
                        
                        if (embed.description) {
                            const desc = document.createElement('div');
                            desc.className = 'embed-description';
                            desc.textContent = embed.description;
                            embedDiv.appendChild(desc);
                        }
                        
                        content.appendChild(embedDiv);
                    });
                }

                // Add reactions
                if (msg.reactions && msg.reactions.length > 0) {
                    const reactionsDiv = document.createElement('div');
                    reactionsDiv.className = 'message-reactions';
                    
                    msg.reactions.forEach(reaction => {
                        const reactionDiv = document.createElement('div');
                        reactionDiv.className = 'reaction' + (reaction.me ? ' reacted' : '');
                        reactionDiv.onclick = () => toggleReaction(msg.id, reaction.emoji);
                        
                        const emoji = typeof reaction.emoji === 'string' ? reaction.emoji : reaction.emoji.name;
                        reactionDiv.innerHTML = `${emoji} <span>${reaction.count}</span>`;
                        
                        reactionsDiv.appendChild(reactionDiv);
                    });
                    
                    content.appendChild(reactionsDiv);
                }

                // Add message actions
                const actions = document.createElement('div');
                actions.className = 'message-actions';
                actions.innerHTML = `
                    <button class="icon-btn" onclick="addReactionToMessage('${msg.id}')" title="React">😊</button>
                    <button class="icon-btn" onclick="replyToMessage('${msg.id}')" title="Reply">↩️</button>
                    <button class="icon-btn" onclick="editMessage('${msg.id}')" title="Edit">✏️</button>
                    <button class="icon-btn" onclick="deleteMessage('${msg.id}')" title="Delete">🗑️</button>
                `;

                messageDiv.appendChild(avatar);
                messageDiv.appendChild(content);
                messageDiv.appendChild(actions);

                return messageDiv;
            }

            // Add new message to display
            function addMessage(msg) {
                const messagesDiv = document.getElementById('messages');
                const messageDiv = createMessageElement(msg);
                messagesDiv.appendChild(messageDiv);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            }

            // Display members
            function displayMembers(memberData) {
                members = memberData;
                const memberList = document.getElementById('memberList');
                memberList.innerHTML = '';

                // Group members by status
                const online = memberData.filter(m => m.status === 'online');
                const idle = memberData.filter(m => m.status === 'idle');
                const dnd = memberData.filter(m => m.status === 'dnd');
                const offline = memberData.filter(m => m.status === 'offline');

                function addMemberGroup(title, members) {
                    if (members.length === 0) return;

                    const groupDiv = document.createElement('div');
                    groupDiv.className = 'member-group';

                    const titleDiv = document.createElement('div');
                    titleDiv.className = 'member-group-title';
                    titleDiv.textContent = `${title} — ${members.length}`;
                    groupDiv.appendChild(titleDiv);

                    members.forEach(member => {
                        const memberDiv = document.createElement('div');
                        memberDiv.className = 'member-item';
                        memberDiv.onclick = () => showUserProfile(member.id);

                        const avatarContainer = document.createElement('div');
                        avatarContainer.style.position = 'relative';

                        const avatar = document.createElement('img');
                        avatar.className = 'member-avatar';
                        avatar.src = member.avatar_url || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="%235865f2"/></svg>';

                        const status = document.createElement('div');
                        status.className = `member-status ${member.status}`;

                        avatarContainer.appendChild(avatar);
                        avatarContainer.appendChild(status);

                        const name = document.createElement('div');
                        name.className = 'member-name';
                        name.textContent = member.display_name || member.username;

                        memberDiv.appendChild(avatarContainer);
                        memberDiv.appendChild(name);

                        groupDiv.appendChild(memberDiv);
                    });

                    memberList.appendChild(groupDiv);
                }

                addMemberGroup('Online', online);
                addMemberGroup('Idle', idle);
                addMemberGroup('Do Not Disturb', dnd);
                addMemberGroup('Offline', offline);
            }

            // Display emojis
            function displayEmojis(emojiData) {
                const emojiGrid = document.getElementById('emojiGrid');
                emojiGrid.innerHTML = '';

                // Add standard emojis
                const standardEmojis = ['😀', '😃', '😄', '😁', '😆', '😅', '🤣', '😂', '🙂', '🙃', '😉', '😊', '😇', '🥰', '😍', '🤩', '😘', '😗', '😚', '😙', '😋', '😛', '😜', '🤪', '😝', '🤑', '🤗', '🤭', '🤫', '🤔', '🤐', '🤨', '😐', '😑', '😶', '😏', '😒', '🙄', '😬', '🤥', '😌', '😔', '😪', '🤤', '😴', '😷', '🤒', '🤕', '🤢', '🤮', '🤧', '🥵', '🥶', '😎', '🤓', '🧐', '😕', '😟', '🙁', '☹️', '😮', '😯', '😲', '😳', '🥺', '😦', '😧', '😨', '😰', '😥', '😢', '😭', '😱', '😖', '😣', '😞', '😓', '😩', '😫', '🥱', '😤', '😡', '😠', '🤬', '👍', '👎', '👏', '🙌', '👋', '🤝', '❤️', '💔', '💯', '🔥', '✨', '⭐', '💫'];
                
                standardEmojis.forEach(emoji => {
                    const emojiDiv = document.createElement('div');
                    emojiDiv.className = 'emoji';
                    emojiDiv.textContent = emoji;
                    emojiDiv.onclick = () => insertEmoji(emoji);
                    emojiGrid.appendChild(emojiDiv);
                });

                // Add custom emojis
                emojiData.forEach(emoji => {
                    const emojiDiv = document.createElement('div');
                    emojiDiv.className = 'emoji';
                    emojiDiv.innerHTML = `<img src="${emoji.url}" style="width: 24px; height: 24px;">`;
                    emojiDiv.onclick = () => insertEmoji(`:${emoji.name}:`);
                    emojiGrid.appendChild(emojiDiv);
                });
            }

            // Send message
            function sendMessage() {
                const input = document.getElementById('messageInput');
                const content = input.value.trim();

                if (!content || !currentChannelId) return;

                sendWebSocket({
                    type: 'send_message',
                    content: content,
                    channel_id: currentChannelId
                });

                input.value = '';
                input.style.height = 'auto';
            }

            // Show user profile
            function showUserProfile(userId) {
                currentUserId = userId;
                
                sendWebSocket({
                    type: 'get_user_profile',
                    user_id: userId
                });

                document.getElementById('profileModal').classList.add('active');
            }

            // Display user profile
            function displayUserProfile(data) {
                document.getElementById('profileAvatar').src = data.user.avatar_url || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="%235865f2"/></svg>';
                document.getElementById('profileUsername').textContent = data.user.display_name || data.user.username;

                const mutualGuilds = document.getElementById('mutualGuilds');
                mutualGuilds.innerHTML = '';
                
                data.mutual_guilds.forEach(guild => {
                    const guildDiv = document.createElement('div');
                    guildDiv.textContent = guild.name;
                    guildDiv.style.padding = '4px 0';
                    mutualGuilds.appendChild(guildDiv);
                });

                // Show moderation controls if in a guild
                if (currentGuildId) {
                    document.getElementById('moderationSection').style.display = 'block';
                }
            }

            // Toggle reaction
            function toggleReaction(messageId, emoji) {
                const emojiStr = typeof emoji === 'string' ? emoji : emoji.name;
                
                sendWebSocket({
                    type: 'add_reaction',
                    message_id: messageId,
                    emoji: emojiStr,
                    channel_id: currentChannelId
                });
            }

            // Moderate user
            function moderateUser(action) {
                if (!currentUserId) return;

                const reason = prompt(`Reason for ${action}:`);
                let duration = null;

                if (action === 'timeout') {
                    duration = prompt('Duration in minutes:');
                }

                sendWebSocket({
                    type: 'moderate_user',
                    user_id: currentUserId,
                    action: action,
                    reason: reason,
                    duration: duration
                });

                closeModal('profileModal');
            }

            // Update bot status
            function updateBotStatus(status) {
                sendWebSocket({
                    type: 'update_bot_status',
                    status: status
                });

                document.getElementById('botStatus').textContent = status.charAt(0).toUpperCase() + status.slice(1);
                closeModal('botSettingsModal');
            }

            // Update bot activity
            function updateBotActivity() {
                const text = document.getElementById('activityText').value;
                const type = document.getElementById('activityType').value;

                sendWebSocket({
                    type: 'update_bot_status',
                    status: 'online',
                    activity_type: type,
                    activity_text: text
                });

                closeModal('botSettingsModal');
            }

            // Start voice recording
            function startVoiceRecording() {
                navigator.mediaDevices.getUserMedia({ audio: true })
                    .then(stream => {
                        mediaRecorder = new MediaRecorder(stream);
                        recordingChunks = [];

                        mediaRecorder.ondataavailable = (e) => {
                            recordingChunks.push(e.data);
                        };

                        mediaRecorder.onstop = () => {
                            const blob = new Blob(recordingChunks, { type: 'audio/ogg' });
                            const reader = new FileReader();
                            reader.onloadend = () => {
                                sendWebSocket({
                                    type: 'record_voice_message',
                                    audio_data: reader.result,
                                    channel_id: currentChannelId
                                });
                            };
                            reader.readAsDataURL(blob);

                            stream.getTracks().forEach(track => track.stop());
                        };

                        mediaRecorder.start();
                        document.getElementById('voiceRecorder').classList.add('active');

                        // Start timer
                        let seconds = 0;
                        const timer = setInterval(() => {
                            seconds++;
                            const mins = Math.floor(seconds / 60);
                            const secs = seconds % 60;
                            document.getElementById('voiceTimer').textContent = 
                                `${mins}:${secs.toString().padStart(2, '0')}`;
                        }, 1000);

                        document.getElementById('stopRecording').onclick = () => {
                            clearInterval(timer);
                            mediaRecorder.stop();
                            document.getElementById('voiceRecorder').classList.remove('active');
                        };
                    })
                    .catch(err => {
                        console.error('Error accessing microphone:', err);
                        alert('Could not access microphone');
                    });
            }

            // Upload file
            function uploadFile(file) {
                const formData = new FormData();
                formData.append('file', file);
                formData.append('channel_id', currentChannelId);

                fetch('/upload', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        console.log('File uploaded successfully');
                    } else {
                        alert('Failed to upload file');
                    }
                })
                .catch(err => {
                    console.error('Upload error:', err);
                    alert('Failed to upload file');
                });
            }

            // Insert emoji
            function insertEmoji(emoji) {
                const input = document.getElementById('messageInput');
                input.value += emoji;
                input.focus();
                document.getElementById('emojiPicker').classList.remove('active');
            }

            // Utility functions
            function formatTimestamp(timestamp) {
                const date = new Date(timestamp);
                const now = new Date();
                const diff = now - date;

                if (diff < 86400000) { // Less than 24 hours
                    return date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
                } else {
                    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
                }
            }

            function sendWebSocket(data) {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify(data));
                }
            }

            function closeModal(modalId) {
                document.getElementById(modalId).classList.remove('active');
            }

            function loadDMChannels() {
                sendWebSocket({
                    type: 'load_dm_channels'
                });
            }

            function displayDMChannels(dmChannels) {
                const channelList = document.getElementById('channelList');
                channelList.innerHTML = '<div class="channel-category">Direct Messages</div>';

                dmChannels.forEach(dm => {
                    const dmDiv = document.createElement('div');
                    dmDiv.className = 'dm-item';
                    dmDiv.onclick = () => selectChannel(dm.id);

                    if (dm.recipient) {
                        const avatar = document.createElement('img');
                        avatar.className = 'dm-avatar';
                        avatar.src = dm.recipient.avatar_url || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="%235865f2"/></svg>';

                        const name = document.createElement('div');
                        name.textContent = dm.recipient.display_name || dm.recipient.username;

                        dmDiv.appendChild(avatar);
                        dmDiv.appendChild(name);
                    }

                    channelList.appendChild(dmDiv);
                });
            }

            // Event listeners
            document.getElementById('messageInput').addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                }

                // Auto-resize textarea
                e.target.style.height = 'auto';
                e.target.style.height = e.target.scrollHeight + 'px';

                // Typing indicator
                if (currentChannelId) {
                    sendWebSocket({
                        type: 'typing_start',
                        channel_id: currentChannelId
                    });
                }
            });

            document.getElementById('sendMessage').addEventListener('click', sendMessage);

            document.getElementById('uploadFile').addEventListener('click', () => {
                document.getElementById('fileInput').click();
            });

            document.getElementById('fileInput').addEventListener('change', (e) => {
                if (e.target.files.length > 0) {
                    uploadFile(e.target.files[0]);
                }
            });

            document.getElementById('recordVoice').addEventListener('click', startVoiceRecording);

            document.getElementById('showEmojis').addEventListener('click', () => {
                const picker = document.getElementById('emojiPicker');
                picker.classList.toggle('active');
            });

            document.getElementById('toggleMembers').addEventListener('click', () => {
                const sidebar = document.getElementById('memberSidebar');
                sidebar.style.display = sidebar.style.display === 'none' ? 'block' : 'none';
            });

            document.getElementById('botUser').addEventListener('click', () => {
                document.getElementById('botSettingsModal').classList.add('active');
            });

            // Close modals when clicking outside
            document.querySelectorAll('.modal-overlay').forEach(overlay => {
                overlay.addEventListener('click', (e) => {
                    if (e.target === overlay) {
                        overlay.classList.remove('active');
                    }
                });
            });

            // Initialize
            initWebSocket();
        </script>
    </body>
    </html>
            """
            
            return html_content
    
    def start_web_client(self):
        """Start the web client in a separate thread"""
        if self.running:
            self.bot.logger.log(MODULE_NAME, "Web client already running", "WARNING")
            return
        
        def run_in_thread():
            time.sleep(2)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.start_server())
                loop.run_forever()
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Web client error", e)
            finally:
                loop.close()
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        self.bot.logger.log(MODULE_NAME, "Advanced web client thread started")
    
    async def stop_server(self):
        """Stop the web server"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        self.running = False
        self.bot.logger.log(MODULE_NAME, "Web client stopped")

def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up ADVANCED web-based control module")
    
    # Create web client manager
    web_client = DiscordWebClient(bot)
    bot.web_client = web_client
    
    # Register console command
    def register_console_command(name, description, handler):
        if hasattr(bot, 'console_commands'):
            bot.console_commands[name] = {
                'description': description,
                'handler': handler
            }
    
    async def handle_web(args):
        if web_client.running:
            print("âš ï¸ Web client is already running")
        else:
            web_client.start_web_client()
            print("âœ… Advanced web client launched")
            print("ðŸŽ¯ Features: Real-time updates, Moderation, Voice messages, File upload, Profiles, DMs")
    
    register_console_command("web", "Launch advanced web-based Discord client", handle_web)
    
    # Add DM forwarding to @officialemball
    @bot.event
    async def on_message(message):
        if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
            # Find officialemball user
            for guild in bot.guilds:
                emball = discord.utils.get(guild.members, name="officialemball")
                if emball:
                    try:
                        embed = discord.Embed(
                            title=f"DM from {message.author}",
                            description=message.content,
                            color=0x00ff00,
                            timestamp=message.created_at
                        )
                        embed.set_author(
                            name=str(message.author),
                            icon_url=message.author.avatar.url if message.author.avatar else None
                        )
                        
                        if message.attachments:
                            embed.add_field(
                                name="Attachments",
                                value=", ".join([att.filename for att in message.attachments]),
                                inline=False
                            )
                        
                        await emball.send(embed=embed)
                        bot.logger.log(MODULE_NAME, f"Forwarded DM from {message.author} to officialemball")
                    except Exception as e:
                        bot.logger.error(MODULE_NAME, "Failed to forward DM", e)
                    break
    
    bot.logger.log(MODULE_NAME, "Advanced web-based control module setup complete")
    bot.logger.log(MODULE_NAME, "Use 'web' command in console to launch the advanced web client")