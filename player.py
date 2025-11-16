import discord
import asyncio
from pathlib import Path
from datetime import timedelta
from discord import app_commands
from typing import Optional
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

MODULE_NAME = "PLAYER"

# Import shared functions from archive
def import_archive_functions(bot):
    """Import necessary functions from archive module"""
    try:
        from archive import (
            find_best_match,
            select_best_candidate,
            FORMATS
        )
        return find_best_match, select_best_candidate, FORMATS
    except ImportError:
        bot.logger.error(MODULE_NAME, "Failed to import from archive module")
        return None, None, None


class Song:
    """Represents a song in the queue"""
    __slots__ = ('file_path', 'metadata', 'requested_by', 'title', 'duration')
    
    def __init__(self, file_path, metadata, requested_by):
        self.file_path = file_path
        self.metadata = metadata
        self.requested_by = requested_by
        self.title = metadata.get('title', Path(file_path).stem)
        self.duration = self._get_duration()

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
            return 0
        return 0


class MusicPlayer:
    """Handles music playback for a guild"""
    
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
        self.bot.logger.log(MODULE_NAME, f"Initialized player for guild {guild_id}")

    async def play_next(self, error=None):
        """Play the next song in queue"""
        if error:
            self.bot.logger.error(MODULE_NAME, f"Player error: {error}")

        async with self.lock:
            if self.loop and self.current:
                self.bot.logger.log(MODULE_NAME, "Looping current song")
                await self.queue.put(self.current)
            
            if self.queue.empty():
                self.current = None
                await self.update_now_playing()
                # Auto-disconnect after 5 minutes of inactivity
                await asyncio.sleep(300)
                if self.queue.empty() and (self.voice_client and not self.voice_client.is_playing()):
                    await self.voice_client.disconnect()
                    self.bot.logger.log(MODULE_NAME, f"Auto-disconnected from guild {self.guild_id}")
                return
                
            self.current = await self.queue.get()
            self.skip_votes.clear()
            self.bot.logger.log(MODULE_NAME, f"Now playing: {self.current.title}")

            # Use FFmpeg for audio playback
            source = discord.FFmpegPCMAudio(
                self.current.file_path,
                before_options='-nostdin',
                options='-vn'
            )

            def after_play(error):
                fut = asyncio.run_coroutine_threadsafe(self.play_next(error), self.bot.loop)
                try:
                    fut.result()
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, f"Error in after_play: {e}")

            try:
                self.voice_client.play(source, after=after_play)
                await self.update_now_playing()
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, f"Playback failed", e)
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
            title="ðŸŽµ Now Playing",
            description=f"**{self.current.title}**",
            color=0x1abc9c
        )
        embed.add_field(name="Requested by", value=self.current.requested_by.mention, inline=True)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        
        if self.current.metadata.get('album'):
            embed.add_field(name="Album", value=self.current.metadata['album'], inline=True)
        if self.current.metadata.get('year'):
            embed.add_field(name="Year", value=self.current.metadata['year'], inline=True)
        
        if not self.queue.empty():
            # Peek at next song without removing it
            queue_list = list(self.queue._queue)
            if queue_list:
                next_song = queue_list[0]
                embed.add_field(name="Next Up", value=next_song.title, inline=False)
        
        embed.set_footer(text=f"Loop: {'ðŸ” ON' if self.loop else 'â¹ï¸ OFF'} | Queue: {self.queue.qsize()}")

        try:
            self.now_playing_msg = await self.text_channel.send(embed=embed)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send now playing message", e)

    async def add_to_queue(self, song, interaction=None):
        """Add song to queue with notification"""
        await self.queue.put(song)
        position = self.queue.qsize()
        
        if interaction:
            await interaction.followup.send(
                f"âœ… Added **{song.title}** to queue (position: {position})"
            )
        
        self.bot.logger.log(MODULE_NAME, f"Added to queue: {song.title} (position: {position})")
        
        if not self.voice_client.is_playing() and not self.paused:
            await self.play_next()

    def skip(self, user_id=None):
        """Skip current song with vote tracking"""
        if user_id:
            self.skip_votes.add(user_id)
            # Exclude bots from member count
            member_count = len([m for m in self.voice_client.channel.members if not m.bot])
            required = max(2, member_count // 2)
            
            if len(self.skip_votes) < required:
                self.bot.logger.log(MODULE_NAME, f"Skip vote: {len(self.skip_votes)}/{required}")
                return False
        
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            self.bot.logger.log(MODULE_NAME, "Skipped current song")
            return True
        return False

    def pause(self):
        """Pause playback"""
        if self.voice_client and self.voice_client.is_playing() and not self.paused:
            self.voice_client.pause()
            self.paused = True
            self.bot.logger.log(MODULE_NAME, "Playback paused")
            return True
        return False

    def resume(self):
        """Resume playback"""
        if self.voice_client and self.voice_client.is_paused() and self.paused:
            self.voice_client.resume()
            self.paused = False
            self.bot.logger.log(MODULE_NAME, "Playback resumed")
            return True
        return False

    def toggle_loop(self):
        """Toggle loop mode"""
        self.loop = not self.loop
        self.bot.logger.log(MODULE_NAME, f"Loop {'enabled' if self.loop else 'disabled'}")
        return self.loop

    def stop(self):
        """Stop playback and clear queue"""
        self.queue = asyncio.Queue()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None
        self.loop = False
        self.skip_votes.clear()
        self.bot.logger.log(MODULE_NAME, "Playback stopped and queue cleared")

    def is_playing(self):
        """Check if music is currently playing"""
        return self.voice_client and (self.voice_client.is_playing() or self.paused)


def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up player module")
    
    # Import functions from archive
    find_best_match, select_best_candidate, FORMATS = import_archive_functions(bot)
    
    if not find_best_match or not select_best_candidate:
        bot.logger.error(MODULE_NAME, "Failed to import required functions from archive")
        return
    
    # Initialize music players dictionary
    bot.music_players = {}

    @bot.event
    async def on_voice_state_update(member, before, after):
        """Handle voice state updates for cleanup"""
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
                bot.logger.log(MODULE_NAME, f"Cleaned up player for guild {guild_id}")
            except Exception as e:
                bot.logger.error(MODULE_NAME, "Voice disconnect cleanup error", e)

    @bot.tree.command(name="play", description="Play a song in voice channel")
    @app_commands.describe(
        format="File format (FLAC or MP3)",
        song_name="Name of the song",
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
        
        # Check if user is in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "âŒ You need to be in a voice channel to use this command!",
                ephemeral=True
            )
            return
        
        voice_channel = interaction.user.voice.channel

        # Ensure archive index is ready
        if not hasattr(bot, 'archive_manager') or not bot.archive_manager.song_index_ready.is_set():
            await interaction.response.send_message(
                "ðŸ”„ Music index not readyâ€”please try again shortly.",
                ephemeral=True
            )
            bot.logger.log(MODULE_NAME, "Index not ready", "WARNING")
            return
            
        await interaction.response.defer(thinking=True)
        bot.logger.log(MODULE_NAME, f"Play command: '{song_name}' (Format: {format}, Version: {version})")

        # Find song using archive functions
        key = find_best_match(bot.archive_manager.song_index, format, song_name)
        if not key:
            await interaction.followup.send(
                f"âŒ Song not found: '{song_name}' in {format}",
                ephemeral=True
            )
            bot.logger.log(MODULE_NAME, f"Song not found: '{song_name}'", "WARNING")
            return
            
        candidates = bot.archive_manager.song_index[format][key]
        best = select_best_candidate(candidates, version)
        
        if not best:
            error_msg = f"âŒ Version '{version}' not found for '{song_name}'" if version else f"âŒ Song not found: '{song_name}'"
            await interaction.followup.send(error_msg, ephemeral=True)
            bot.logger.log(MODULE_NAME, f"Version not found: {version}", "WARNING")
            return
            
        # Create song object
        song = Song(
            file_path=best['path'],
            metadata=best['metadata'],
            requested_by=interaction.user
        )
        
        # Get or create player for this guild
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player:
            try:
                voice_client = await voice_channel.connect()
                player = MusicPlayer(bot, guild_id)
                player.voice_client = voice_client
                player.text_channel = interaction.channel
                bot.music_players[guild_id] = player
                bot.logger.log(MODULE_NAME, f"Connected to voice in guild {guild_id}")
            except Exception as e:
                await interaction.followup.send(
                    f"âŒ Failed to join voice channel: {str(e)}",
                    ephemeral=True
                )
                bot.logger.error(MODULE_NAME, "Voice connection failed", e)
                return
        elif player.voice_client.channel != voice_channel:
            try:
                await player.voice_client.move_to(voice_channel)
                bot.logger.log(MODULE_NAME, f"Moved to voice channel: {voice_channel.name}")
            except Exception as e:
                bot.logger.error(MODULE_NAME, "Failed to move voice channel", e)
            
        # Add to queue and start playing
        await player.add_to_queue(song, interaction)

    @bot.tree.command(name="stop", description="Stop playback and clear queue")
    async def stop(interaction: discord.Interaction):
        """Stop music command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.is_playing():
            await interaction.response.send_message(
                "âŒ Nothing is playing",
                ephemeral=True
            )
            return
            
        player.stop()
        await interaction.response.send_message("â¹ï¸ Stopped playback and cleared queue")
        bot.logger.log(MODULE_NAME, f"Playback stopped by {interaction.user}")

    @bot.tree.command(name="skip", description="Skip current song (vote-based)")
    async def skip(interaction: discord.Interaction):
        """Skip command with vote system"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.is_playing():
            await interaction.response.send_message(
                "âŒ Nothing is playing",
                ephemeral=True
            )
            return
            
        # Check if requester is admin or alone in voice (excluding bots)
        member_count = len([m for m in player.voice_client.channel.members if not m.bot])
        
        if interaction.user.guild_permissions.administrator or member_count <= 1:
            player.skip()
            await interaction.response.send_message("â­ï¸ Skipped current song")
            bot.logger.log(MODULE_NAME, f"Admin/solo skip by {interaction.user}")
            return
            
        # Handle vote skip
        if player.skip(interaction.user.id):
            await interaction.response.send_message("â­ï¸ Skipped current song (vote passed)")
            bot.logger.log(MODULE_NAME, f"Vote skip passed in guild {guild_id}")
        else:
            required = max(2, member_count // 2)
            await interaction.response.send_message(
                f"ðŸ—³ï¸ Vote to skip: {len(player.skip_votes)}/{required} votes"
            )

    @bot.tree.command(name="pause", description="Pause playback")
    async def pause(interaction: discord.Interaction):
        """Pause command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.voice_client:
            await interaction.response.send_message(
                "âŒ Nothing is playing",
                ephemeral=True
            )
            return
        
        if not player.voice_client.is_playing():
            await interaction.response.send_message(
                "âŒ Nothing is playing",
                ephemeral=True
            )
            return
            
        if player.paused:
            await interaction.response.send_message(
                "âŒ Already paused",
                ephemeral=True
            )
            return
            
        player.pause()
        await interaction.response.send_message("â¸ï¸ Playback paused")
        bot.logger.log(MODULE_NAME, f"Playback paused by {interaction.user}")

    @bot.tree.command(name="resume", description="Resume playback")
    async def resume(interaction: discord.Interaction):
        """Resume command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.paused:
            await interaction.response.send_message(
                "âŒ Playback not paused",
                ephemeral=True
            )
            return
            
        player.resume()
        await interaction.response.send_message("â–¶ï¸ Playback resumed")
        bot.logger.log(MODULE_NAME, f"Playback resumed by {interaction.user}")

    @bot.tree.command(name="queue", description="Show current queue")
    async def show_queue(interaction: discord.Interaction):
        """Queue command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player:
            await interaction.response.send_message(
                "âŒ No active player",
                ephemeral=True
            )
            return
        
        if not player.current and player.queue.empty():
            await interaction.response.send_message(
                "âŒ Queue is empty",
                ephemeral=True
            )
            return
            
        embed = discord.Embed(
            title="ðŸŽµ Music Queue",
            color=0x3498db
        )
        
        if player.current:
            duration = str(timedelta(seconds=player.current.duration)) if player.current.duration > 0 else "Unknown"
            embed.add_field(
                name="Now Playing",
                value=f"**{player.current.title}** ({duration})\nRequested by {player.current.requested_by.mention}",
                inline=False
            )
        
        if not player.queue.empty():
            queue_items = []
            queue_list = list(player.queue._queue)[:10]
            
            for i, song in enumerate(queue_list, 1):
                duration = str(timedelta(seconds=song.duration)) if song.duration > 0 else "?"
                queue_items.append(f"{i}. **{song.title}** ({duration}) - {song.requested_by.mention}")
            
            embed.add_field(
                name=f"Up Next ({player.queue.qsize()} songs)",
                value="\n".join(queue_items),
                inline=False
            )
            
            if player.queue.qsize() > 10:
                embed.set_footer(text=f"Plus {player.queue.qsize() - 10} more songs...")
        
        embed.set_footer(text=f"Loop: {'ðŸ” ON' if player.loop else 'â¹ï¸ OFF'}")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="loop", description="Toggle loop mode for current song")
    async def toggle_loop(interaction: discord.Interaction):
        """Loop toggle command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player:
            await interaction.response.send_message(
                "âŒ No active player",
                ephemeral=True
            )
            return
            
        state = player.toggle_loop()
        await interaction.response.send_message(
            f"ðŸ” Loop {'enabled' if state else 'disabled'}"
        )
        bot.logger.log(MODULE_NAME, f"Loop {'enabled' if state else 'disabled'} by {interaction.user}")

    @bot.tree.command(name="leave", description="Disconnect bot from voice channel")
    async def leave(interaction: discord.Interaction):
        """Leave voice channel command"""
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)
        
        if not player or not player.voice_client:
            await interaction.response.send_message(
                "âŒ Not in a voice channel",
                ephemeral=True
            )
            return
        
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            # Check if user is in the same voice channel
            if not interaction.user.voice or interaction.user.voice.channel != player.voice_client.channel:
                await interaction.response.send_message(
                    "âŒ You must be in the same voice channel",
                    ephemeral=True
                )
                return
        
        player.stop()
        await player.voice_client.disconnect()
        del bot.music_players[guild_id]
        
        await interaction.response.send_message("ðŸ‘‹ Disconnected from voice channel")
        bot.logger.log(MODULE_NAME, f"Left voice channel in guild {guild_id}")

    bot.logger.log(MODULE_NAME, "Player module setup complete")