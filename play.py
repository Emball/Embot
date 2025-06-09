import discord
from discord import app_commands
import asyncio
import logging
import random
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import os
import difflib
import re

# Reuse constants from emarchive.py
FORMATS = ["FLAC", "MP3"]
VERSION_KEYWORDS = ['live', 'remix', 'demo', 'acoustic', 'version', 'edit', 'radio']
CACHE_CHANNEL_NAME = "songcache"
INDEX_FILE = "song_index.json"

# Configure logging
logger = logging.getLogger('emarchive.play')

# Reuse helper functions from emarchive
def normalize_title(title):
    t = re.sub(r'^(\d+\s*-\s*)?\d+\s+', '', title)
    t = re.sub(r'[({\[].*?[)}\]](?=\s*$)', '', t)
    t = re.sub(r'\b(?:feat\.?|ft\.?|with)\s+.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.casefold()

def find_best_match(idx, fmt, query):
    logger.debug(f"[SEARCH] Finding best match for '{query}' in {fmt}")
    key = normalize_title(query)
    return difflib.get_close_matches(key, idx.get(fmt, {}).keys(), n=1, cutoff=0.6)[0] if idx.get(fmt) else None

def select_best_candidate(cands, version=None):
    logger.debug(f"[SELECT] Selecting from {len(cands)} candidates, version={version}")
    if version:
        vl = version.lower()
        filtered = [c for c in cands if vl in c['original_title'].lower() or vl in c['folder'].lower()]
        if not filtered:
            logger.debug(f"[SELECT] No candidates match version '{version}'")
            return None
        cands = filtered
        logger.debug(f"[SELECT] Filtered to {len(cands)} candidates matching version")

    scored = []
    for c in cands:
        p = sum(1 for kw in VERSION_KEYWORDS if kw in c['original_title'].lower() or kw in c['folder'].lower())
        y = 9999
        try:
            yv = c['metadata'].get('year', '')
            y = int(yv[:4]) if yv and yv[:4].isdigit() else 9999
        except:
            pass
        scored.append((p, y, c))
    scored.sort(key=lambda x: (x[0], x[1]))
    
    return scored[0][2] if scored else None

class Song:
    __slots__ = ('file_path', 'metadata', 'requested_by', 'title')
    
    def __init__(self, file_path, metadata, requested_by):
        self.file_path = file_path
        self.metadata = metadata
        self.requested_by = requested_by
        self.title = metadata.get('title', Path(file_path).stem)

class MusicPlayer:
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = asyncio.Queue()
        self.current = None
        self.voice_client = None
        self.lock = asyncio.Lock()
        self.paused = False
        self.loop = bot.loop
        self.now_playing_msg = None
        self.text_channel = None

    async def play_next(self, error=None):
        if error:
            logger.error(f'Player error: {error}')
        
        async with self.lock:
            if self.queue.empty():
                self.current = None
                await self.update_now_playing()
                # Auto-disconnect after 5 minutes of inactivity
                await asyncio.sleep(300)
                if self.queue.empty() and (self.voice_client and not self.voice_client.is_playing()):
                    await self.voice_client.disconnect()
                return
                
            self.current = await self.queue.get()

            # Use FFmpeg with fixed parameters to avoid speed issues
            # For example, disable 'aresample' filter that may cause speed changes
            # Also ensure volume filter disabled to avoid audio distortion if any
            source = discord.FFmpegPCMAudio(self.current.file_path, options='-vn -af "aresample=resampler=soxr"')

            def after_play(error):
                fut = self.loop.create_task(self.play_next(error))
                try:
                    fut.result()
                except Exception as e:
                    logger.error(f"Error in after_play task: {e}")

            self.voice_client.play(
                source, 
                after=after_play
            )
            await self.update_now_playing()

    async def update_now_playing(self):
        if not self.text_channel:
            return
            
        try:
            if self.now_playing_msg:
                await self.now_playing_msg.delete()
        except:
            pass
            
        if not self.current:
            return
            
        embed = discord.Embed(
            title="Now Playing",
            description=f"**{self.current.title}**",
            color=0x1abc9c
        )
        embed.add_field(name="Requested by", value=self.current.requested_by.mention)
        embed.add_field(name="Duration", value="Unknown")
        
        self.now_playing_msg = await self.text_channel.send(embed=embed)

    async def add_to_queue(self, song, interaction=None):
        await self.queue.put(song)
        if interaction:
            await interaction.followup.send(
                f"✅ Added **{song.title}** to queue (position: {self.queue.qsize()})"
            )
        
        if not self.voice_client.is_playing() and not self.paused:
            await self.play_next()

    def skip(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    def pause(self):
        if self.voice_client and self.voice_client.is_playing() and not self.paused:
            self.voice_client.pause()
            self.paused = True

    def resume(self):
        if self.voice_client and self.voice_client.is_paused() and self.paused:
            self.voice_client.resume()
            self.paused = False

    def stop(self):
        self.queue = asyncio.Queue()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None

    def is_playing(self):
        return self.voice_client and (self.voice_client.is_playing() or self.paused)

def setup_play(bot):
    logger.info("[SETUP] Initializing play extension")
    bot.music_players = {}  # guild_id: MusicPlayer

    async def log_command_execution(interaction, command_data, success, song_metadata=None, file_path=None, error=None):
        # Placeholder for command execution logging
        pass

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
            if player.voice_client:
                await player.voice_client.disconnect()
            del bot.music_players[guild_id]

    @bot.tree.command(name="play", description="Play a song in voice channel")
    @app_commands.describe(format="File format", song_name="Song name", version="Version (optional)")
    @app_commands.choices(format=[app_commands.Choice(name=fmt, value=fmt) for fmt in FORMATS])
    async def play(interaction: discord.Interaction, format: str, song_name: str, version: Optional[str] = None):
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
                    "❌ You're not in a voice channel!"
                )
                return
            voice_channel = default_channel

        # Ensure index is ready
        if not bot.song_index_ready.is_set():
            await interaction.response.send_message("🔄 Initializing—please try again shortly.")
            return
            
        await interaction.response.defer(thinking=True)
        
        # Find song
        key = find_best_match(bot.song_index, format, song_name)
        if not key:
            await interaction.followup.send(f"❌ Song not found: {song_name}")
            return
            
        candidates = bot.song_index[format][key]
        best = select_best_candidate(candidates, version)
        if not best:
            await interaction.followup.send(f"❌ Version not found: {version or 'default'}")
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
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to join voice: {e}")
                return
        elif player.voice_client.channel != voice_channel:
            await player.voice_client.move_to(voice_channel)
            
        # Add to queue
        await player.add_to_queue(song, interaction)
        await log_command_execution(
            interaction,
            command_data,
            True,
            song_metadata=best['metadata'],
            file_path=best['path']
        )

    @bot.tree.command(name="stop", description="Stop playback and clear queue")
    async def stop(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.is_playing():
            await interaction.response.send_message("❌ Nothing is playing")
            return
            
        player.stop()
        await interaction.response.send_message("⏹️ Stopped playback and cleared queue")

    @bot.tree.command(name="pause", description="Pause playback")
    async def pause(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.voice_client.is_playing():
            await interaction.response.send_message("❌ Nothing is playing")
            return
            
        if player.paused:
            await interaction.response.send_message("❌ Already paused")
            return
            
        player.pause()
        await interaction.response.send_message("⏸️ Playback paused")

    @bot.tree.command(name="resume", description="Resume playback")
    async def resume(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.paused:
            await interaction.response.send_message("❌ Playback not paused")
            return
            
        player.resume()
        await interaction.response.send_message("▶️ Playback resumed")

    @bot.tree.command(name="skip", description="Skip current song")
    async def skip(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.is_playing():
            await interaction.response.send_message("❌ Nothing is playing")
            return
            
        player.skip()
        await interaction.response.send_message("⏭️ Skipped current song")

    @bot.tree.command(name="queue", description="Show current queue")
    async def show_queue(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or player.queue.empty():
            await interaction.response.send_message("❌ Queue is empty")
            return
            
        queue_items = []
        # Direct access to internal queue _queue is safe here
        for i in range(player.queue.qsize()):
            song = player.queue._queue[i]
            queue_items.append(f"{i+1}. {song.title} (requested by {song.requested_by.mention})")
            
        embed = discord.Embed(
            title="Music Queue",
            description="\n".join(queue_items),
            color=0x3498db
        )
        
        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"{player.current.title} (requested by {player.current.requested_by.mention})",
                inline=False
            )
            
        await interaction.response.send_message(embed=embed)

    # === New admin-only command to play any file by path ===
    @bot.tree.command(name="adminplay", description="Admin only: Play an audio file from any path")
    @app_commands.describe(file_path="Full path to the audio file")
    async def adminplay(interaction: discord.Interaction, file_path: str):
        # Check admin permission
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You must be an administrator to use this command.", ephemeral=True)
            return

        # Validate file existence and is file
        if not os.path.isfile(file_path):
            await interaction.response.send_message(f"❌ File does not exist or is not a file:\n`{file_path}`", ephemeral=True)
            return

        # Check file extension for supported formats
        supported_exts = ['.mp3', '.flac', '.wav', '.ogg', '.m4a']
        if not any(file_path.lower().endswith(ext) for ext in supported_exts):
            await interaction.response.send_message(f"❌ Unsupported audio file extension. Supported: {', '.join(supported_exts)}", ephemeral=True)
            return

        # Get voice channel of user or default to 'Voice 1'
        voice_channel = interaction.user.voice.channel if interaction.user.voice else None
        if not voice_channel:
            voice_channel = discord.utils.get(interaction.guild.voice_channels, name="Voice 1")
            if not voice_channel:
                await interaction.response.send_message("❌ You are not in a voice channel and no default 'Voice 1' channel found.", ephemeral=True)
                return

        await interaction.response.defer(thinking=True)

        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        # Connect or move bot to the voice channel
        if not player:
            try:
                voice_client = await voice_channel.connect()
                player = MusicPlayer(bot, guild_id)
                player.voice_client = voice_client
                player.text_channel = interaction.channel
                bot.music_players[guild_id] = player
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to join voice channel: {e}")
                return
        elif player.voice_client.channel != voice_channel:
            await player.voice_client.move_to(voice_channel)

        # Create a Song object with minimal metadata
        song = Song(
            file_path=file_path,
            metadata={'title': os.path.basename(file_path)},
            requested_by=interaction.user
        )

        # Add to queue and start playback if needed
        await player.add_to_queue(song, interaction)

    logger.info("[SETUP] Play commands registered")
