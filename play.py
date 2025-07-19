# play.py
import discord
import asyncio
import logging
import os
import random
from pathlib import Path
from datetime import datetime
from discord import app_commands
from typing import Optional
from collections import defaultdict
from io import BytesIO

logger = logging.getLogger('play')

# Reuse constants from emarchive.py
FORMATS = ["FLAC", "MP3"]
VERSION_KEYWORDS = ['live', 'remix', 'demo', 'acoustic', 'version', 'edit', 'radio']
CACHE_CHANNEL_NAME = "songcache"
LARGE_FILE_MSG = "Sorry! The song file was too big to upload in the server."

class Song:
    __slots__ = ('file_path', 'metadata', 'requested_by', 'title', 'duration')
    
    def __init__(self, file_path, metadata, requested_by):
        self.file_path = file_path
        self.metadata = metadata
        self.requested_by = requested_by
        self.title = metadata.get('title', Path(file_path).stem)
        self.duration = self._get_duration()
        logger.debug(f"Created song: {self.title} (Duration: {self.duration}s)")

    def _get_duration(self):
        """Get duration in seconds from file metadata"""
        try:
            if self.file_path.lower().endswith('.flac'):
                audio = FLAC(self.file_path)
                return int(audio.info.length)
            elif self.file_path.lower().endswith('.mp3'):
                audio = MP3(self.file_path)
                return int(audio.info.length)
        except Exception as e:
            logger.warning(f"Couldn't get duration for {self.file_path}: {e}")
            return 0

class MusicPlayer:
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = asyncio.Queue()
        self.current = None
        self.voice_client = None
        self.lock = asyncio.Lock()
        self.paused = False
        self.loop = False
        self.now_playing_msg = None
        self.text_channel = None
        self.skip_votes = set()
        logger.info(f"Initialized MusicPlayer for guild {guild_id}")

    async def play_next(self, error=None):
        """Play the next song in queue"""
        if error:
            logger.error(f"Player error: {error}")

        async with self.lock:
            if self.loop and self.current:
                logger.debug("Looping current song")
                await self.queue.put(self.current)
            
            if self.queue.empty():
                self.current = None
                await self.update_now_playing()
                # Auto-disconnect after 5 minutes of inactivity
                await asyncio.sleep(300)
                if self.queue.empty() and (self.voice_client and not self.voice_client.is_playing()):
                    await self.voice_client.disconnect()
                    logger.info(f"Auto-disconnected from voice in guild {self.guild_id}")
                return
                
            self.current = await self.queue.get()
            self.skip_votes.clear()
            logger.info(f"Now playing: {self.current.title}")

            # Use FFmpeg with better audio processing
            source = discord.FFmpegPCMAudio(
                self.current.file_path,
                before_options='-nostdin',
                options='-vn -af "aresample=resampler=soxr:precision=28"'
            )

            def after_play(error):
                fut = asyncio.run_coroutine_threadsafe(self.play_next(error), self.bot.loop)
                try:
                    fut.result()
                except Exception as e:
                    logger.error(f"Error in after_play: {e}")

            try:
                self.voice_client.play(source, after=after_play)
                await self.update_now_playing()
            except Exception as e:
                logger.error(f"Playback failed: {e}")
                await self.play_next()

    async def update_now_playing(self):
        """Update the now playing embed"""
        if not self.text_channel:
            return
            
        try:
            if self.now_playing_msg:
                await self.now_playing_msg.delete()
        except:
            pass
            
        if not self.current:
            return
            
        duration_str = str(timedelta(seconds=self.current.duration)) if self.current.duration > 0 else "Unknown"
        
        embed = discord.Embed(
            title="Now Playing",
            description=f"**{self.current.title}**",
            color=0x1abc9c
        )
        embed.add_field(name="Requested by", value=self.current.requested_by.mention, inline=True)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        
        if not self.queue.empty():
            next_song = self.queue._queue[0]  # Safe access to first item
            embed.add_field(name="Next Up", value=next_song.title, inline=False)
        
        embed.set_footer(text=f"Loop: {'🔁 ON' if self.loop else '⭕ OFF'} | Queue: {self.queue.qsize()}")

        try:
            self.now_playing_msg = await self.text_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send now playing message: {e}")

    async def add_to_queue(self, song, interaction=None):
        """Add song to queue with notification"""
        await self.queue.put(song)
        if interaction:
            await interaction.followup.send(
                f"✅ Added **{song.title}** to queue (position: {self.queue.qsize()})"
            )
        logger.info(f"Added to queue: {song.title} (position: {self.queue.qsize()})")
        
        if not self.voice_client.is_playing() and not self.paused:
            await self.play_next()

    def skip(self, user_id=None):
        """Skip current song with vote tracking"""
        if user_id:
            self.skip_votes.add(user_id)
            required = max(2, len(self.voice_client.channel.members) // 2)
            if len(self.skip_votes) < required:
                logger.debug(f"Skip vote: {len(self.skip_votes)}/{required}")
                return False
        
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            logger.info("Skipped current song")
            return True
        return False

    def pause(self):
        """Pause playback"""
        if self.voice_client and self.voice_client.is_playing() and not self.paused:
            self.voice_client.pause()
            self.paused = True
            logger.info("Playback paused")

    def resume(self):
        """Resume playback"""
        if self.voice_client and self.voice_client.is_paused() and self.paused:
            self.voice_client.resume()
            self.paused = False
            logger.info("Playback resumed")

    def toggle_loop(self):
        """Toggle loop mode"""
        self.loop = not self.loop
        logger.info(f"Loop {'enabled' if self.loop else 'disabled'}")
        return self.loop

    def stop(self):
        """Stop playback and clear queue"""
        self.queue = asyncio.Queue()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None
        self.loop = False
        logger.info("Playback stopped and queue cleared")

    def is_playing(self):
        return self.voice_client and (self.voice_client.is_playing() or self.paused)

def setup(bot):  # Changed from setup_play
    """Setup music commands"""
    logger.info("Initializing play commands")
    bot.music_players = {}

    @bot.event
    async def on_voice_state_update(member, before, after):
        if member.id != bot.user.id:
            return
            
        guild_id = member.guild.id
        if guild_id not in bot.music_players:
            return
            
        player = bot.music_players[guild_id]
        
        # Clean up if bot disconnected
        if not after.channel:
            try:
                if player.voice_client:
                    await player.voice_client.disconnect()
                del bot.music_players[guild_id]
                logger.info(f"Disconnected from voice in guild {guild_id}")
            except Exception as e:
                logger.error(f"Voice disconnect error: {e}")

    @bot.tree.command(name="play", description="Play a song in voice channel")
    @app_commands.describe(
        format="File format",
        song_name="Song name", 
        version="Specific version (optional)"
    )
    @app_commands.choices(format=[app_commands.Choice(name=fmt, value=fmt) for fmt in FORMATS])
    async def play(interaction: discord.Interaction, format: str, song_name: str, version: Optional[str] = None):
        """Play music command"""
        command_data = {
            'format': format,
            'song_name': song_name,
            'version': version or 'N/A'
        }
        
        # Check voice channel
        voice_channel = interaction.user.voice.channel if interaction.user.voice else None
        if not voice_channel:
            default_channel = discord.utils.get(interaction.guild.voice_channels, name="voice 1")
            if not default_channel:
                await interaction.response.send_message(
                    "❌ You're not in a voice channel!", 
                    ephemeral=True
                )
                return
            voice_channel = default_channel

        # Ensure index is ready
        if not bot.song_index_ready.is_set():
            await interaction.response.send_message(
                "🔄 Initializing—please try again shortly.",
                ephemeral=True
            )
            return
            
        await interaction.response.defer(thinking=True)
        logger.info(f"Play command from {interaction.user}: {song_name} ({format})")

        # Find song
        key = find_best_match(bot.song_index, format, song_name)
        if not key:
            await interaction.followup.send(
                f"❌ Song not found: {song_name}",
                ephemeral=True
            )
            logger.warning(f"Song not found: {song_name}")
            return
            
        candidates = bot.song_index[format][key]
        best = select_best_candidate(candidates, version)
        if not best:
            await interaction.followup.send(
                f"❌ Version not found: {version or 'default'}",
                ephemeral=True
            )
            logger.warning(f"Version not found: {version}")
            return
            
        # Create song object
        song = Song(
            file_path=best['path'],
            metadata=best['metadata'],
            requested_by=interaction.user
        )
        
        # Get or create player
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player:
            try:
                voice_client = await voice_channel.connect()
                player = MusicPlayer(bot, guild_id)
                player.voice_client = voice_client
                player.text_channel = interaction.channel
                bot.music_players[guild_id] = player
                logger.info(f"Connected to voice in guild {guild_id}")
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Failed to join voice: {e}",
                    ephemeral=True
                )
                logger.error(f"Voice connection failed: {e}")
                return
        elif player.voice_client.channel != voice_channel:
            await player.voice_client.move_to(voice_channel)
            logger.info(f"Moved to voice channel {voice_channel}")
            
        # Add to queue
        await player.add_to_queue(song, interaction)

    @bot.tree.command(name="stop", description="Stop playback and clear queue")
    async def stop(interaction: discord.Interaction):
        """Stop music command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.is_playing():
            await interaction.response.send_message(
                "❌ Nothing is playing",
                ephemeral=True
            )
            return
            
        player.stop()
        await interaction.response.send_message(
            "⏹️ Stopped playback and cleared queue"
        )
        logger.info(f"Playback stopped by {interaction.user}")

    @bot.tree.command(name="skip", description="Skip current song")
    async def skip(interaction: discord.Interaction):
        """Skip command with vote system"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.is_playing():
            await interaction.response.send_message(
                "❌ Nothing is playing",
                ephemeral=True
            )
            return
            
        # Check if requester is admin or alone in voice
        if interaction.user.guild_permissions.administrator or \
           len(player.voice_client.channel.members) <= 2:
            player.skip()
            await interaction.response.send_message("⏭️ Skipped current song")
            logger.info(f"Admin skip by {interaction.user}")
            return
            
        # Handle vote skip
        if player.skip(interaction.user.id):
            await interaction.response.send_message("⏭️ Skipped current song")
        else:
            required = max(2, len(player.voice_client.channel.members) // 2)
            await interaction.response.send_message(
                f"🗳️ Vote to skip ({len(player.skip_votes)}/{required} votes)"
            )

    @bot.tree.command(name="pause", description="Pause playback")
    async def pause(interaction: discord.Interaction):
        """Pause command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.voice_client.is_playing():
            await interaction.response.send_message(
                "❌ Nothing is playing",
                ephemeral=True
            )
            return
            
        if player.paused:
            await interaction.response.send_message(
                "❌ Already paused",
                ephemeral=True
            )
            return
            
        player.pause()
        await interaction.response.send_message("⏸️ Playback paused")
        logger.info(f"Playback paused by {interaction.user}")

    @bot.tree.command(name="resume", description="Resume playback")
    async def resume(interaction: discord.Interaction):
        """Resume command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.paused:
            await interaction.response.send_message(
                "❌ Playback not paused",
                ephemeral=True
            )
            return
            
        player.resume()
        await interaction.response.send_message("▶️ Playback resumed")
        logger.info(f"Playback resumed by {interaction.user}")

    @bot.tree.command(name="queue", description="Show current queue")
    async def show_queue(interaction: discord.Interaction):
        """Queue command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or player.queue.empty():
            await interaction.response.send_message(
                "❌ Queue is empty",
                ephemeral=True
            )
            return
            
        queue_items = []
        for i, song in enumerate(list(player.queue._queue)[:10], 1):
            queue_items.append(f"{i}. {song.title} (requested by {song.requested_by.mention})")
        
        embed = discord.Embed(
            title="Music Queue",
            description="\n".join(queue_items),
            color=0x3498db
        )
        
        if player.current:
            duration = str(timedelta(seconds=player.current.duration)) if player.current.duration > 0 else "Unknown"
            embed.add_field(
                name="Now Playing",
                value=f"{player.current.title} ({duration})",
                inline=False
            )
            
        if player.queue.qsize() > 10:
            embed.set_footer(text=f"Plus {player.queue.qsize() - 10} more songs...")
            
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="loop", description="Toggle loop mode")
    async def toggle_loop(interaction: discord.Interaction):
        """Loop toggle command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player:
            await interaction.response.send_message(
                "❌ Not currently playing",
                ephemeral=True
            )
            return
            
        state = player.toggle_loop()
        await interaction.response.send_message(
            f"🔁 Loop {'enabled' if state else 'disabled'}"
        )
        logger.info(f"Loop {'enabled' if state else 'disabled'} by {interaction.user}")

    logger.info("Play commands registered")