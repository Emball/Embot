import discord
import asyncio
from pathlib import Path
from datetime import timedelta
from discord import app_commands
from typing import Optional
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

MODULE_NAME = "MUSIC PLAYER"

def import_archive_functions():
    try:
        from music_archive import (
            find_best_match,
            select_best_candidate,
            FORMATS
        )
        return find_best_match, select_best_candidate, FORMATS
    except ImportError as e:
        print(f"Failed to import from archive module: {e}")
        return None, None, None

class Song:
    __slots__ = ('file_path', 'metadata', 'requested_by', 'title', 'duration')

    def __init__(self, file_path, metadata, requested_by):
        self.file_path = file_path
        self.metadata = metadata
        self.requested_by = requested_by
        self.title = metadata.get('title', Path(file_path).stem)
        self.duration = self._get_duration()

    def _get_duration(self):
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

class UrlSong:
    __slots__ = ('url', 'requested_by', 'title', 'duration')

    def __init__(self, url, title, duration, requested_by):
        self.url = url
        self.requested_by = requested_by
        self.title = title
        self.duration = duration

    # No metadata/album fields needed — update_now_playing guards with .get()

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
        self.disconnect_timer = None
        self._current_source = None
        self.bot.logger.log(MODULE_NAME, f"Initialized player for guild {guild_id}")

    async def play_next(self, error=None):
        if error:
            self.bot.logger.error(MODULE_NAME, f"Player error: {error}")

        if self.disconnect_timer:
            self.disconnect_timer.cancel()
            self.disconnect_timer = None

        async with self.lock:
            if self.loop and self.current:
                self.bot.logger.log(MODULE_NAME, "Looping current song")
                await self.queue.put(self.current)

            if self.queue.empty():
                self.current = None
                await self.update_now_playing()
                self.disconnect_timer = asyncio.create_task(self.auto_disconnect())
                return

            self.current = await self.queue.get()
            self.skip_votes.clear()
            self.bot.logger.log(MODULE_NAME, f"Now playing: {self.current.title}")

            if isinstance(self.current, UrlSong):
                # Resolve fresh opus stream URL at play time to avoid expiry
                proc = await asyncio.create_subprocess_exec(
                    'yt-dlp', '-f', 'bestaudio[ext=webm][acodec=opus]/bestaudio[ext=opus]/bestaudio/best',
                    '--get-url', '--no-playlist', '--quiet', self.current.url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                stream_url = stdout.decode().strip().splitlines()[0]
                source = discord.FFmpegOpusAudio(
                    stream_url,
                    before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    bitrate=128
                )
            else:
                source = discord.FFmpegPCMAudio(
                    self.current.file_path,
                    before_options='-nostdin',
                    options='-vn'
                )
            self._current_source = source

            def after_play(error):
                self._current_source = None
                if error and not self.voice_client:
                    return
                fut = asyncio.run_coroutine_threadsafe(self.play_next(error), self.bot.loop)
                try:
                    fut.result()
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, f"Error in after_play: {e}")

            try:
                if self.voice_client and not self.voice_client.is_connected():
                    self._current_source = None
                    self.bot.logger.log(MODULE_NAME, "Voice client disconnected, cannot play")
                    return

                self.voice_client.play(source, after=after_play)
                await self.update_now_playing()
            except Exception as e:
                self._current_source = None
                self.bot.logger.error(MODULE_NAME, f"Playback failed", e)
                await self.play_next()

    async def auto_disconnect(self):
        try:
            await asyncio.sleep(300)
            if self.queue.empty() and (self.voice_client and self.voice_client.is_connected() and not self.voice_client.is_playing()):
                await self.voice_client.disconnect()
                guild_id = self.guild_id
                if guild_id in self.bot.music_players:
                    del self.bot.music_players[guild_id]
                self.bot.logger.log(MODULE_NAME, f"Auto-disconnected from guild {guild_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Auto-disconnect error", e)

    async def update_now_playing(self):
        if not self.text_channel:
            return

        try:
            if self.now_playing_msg:
                try:
                    await self.now_playing_msg.delete()
                except discord.NotFound:
                    pass
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, "Failed to delete now playing message", "WARNING")

        if not self.current:
            return

        duration_str = str(timedelta(seconds=self.current.duration)) if self.current.duration > 0 else "Unknown"

        parts = [f"**{self.current.title}**"]
        parts.append(f"**Requested by** {self.current.requested_by.mention}")
        parts.append(f"**Duration** {duration_str}")

        metadata = getattr(self.current, 'metadata', {})
        if metadata.get('album'):
            parts.append(f"**Album** {metadata['album']}")
        if metadata.get('year'):
            parts.append(f"**Year** {metadata['year']}")

        if not self.queue.empty():
            queue_items: list = []
            while not self.queue.empty():
                try:
                    queue_items.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if queue_items:
                next_song = queue_items[0]
                parts.append(f"**Next Up**\n{next_song.title}")
            for item in queue_items:
                await self.queue.put(item)

        footer = f"Loop: {'🔁 ON' if self.loop else '⏹ OFF'} | Queue: {self.queue.qsize()}"
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(parts)),
            accent_color=0x1abc9c
        ))
        view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        view.add_item(discord.ui.TextDisplay(f"-# {footer}"))

        try:
            self.now_playing_msg = await self.text_channel.send(view=view)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send now playing message", e)

    async def add_to_queue(self, song, interaction=None):
        await self.queue.put(song)
        position = self.queue.qsize()

        if interaction:
            await interaction.followup.send(
                f"Added **{song.title}** to queue (position: {position})"
            )

        self.bot.logger.log(MODULE_NAME, f"Added to queue: {song.title} (position: {position})")

        if self.disconnect_timer:
            self.disconnect_timer.cancel()
            self.disconnect_timer = None

        if self.voice_client and not self.voice_client.is_playing() and not self.paused:
            await self.play_next()

    def skip(self, user_id=None):
        if user_id:
            self.skip_votes.add(user_id)
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
        if self.voice_client and self.voice_client.is_playing() and not self.paused:
            self.voice_client.pause()
            self.paused = True
            self.bot.logger.log(MODULE_NAME, "Playback paused")
            return True
        return False

    def resume(self):
        if self.voice_client and self.voice_client.is_paused() and self.paused:
            self.voice_client.resume()
            self.paused = False
            self.bot.logger.log(MODULE_NAME, "Playback resumed")
            return True
        return False

    def toggle_loop(self):
        self.loop = not self.loop
        self.bot.logger.log(MODULE_NAME, f"Loop {'enabled' if self.loop else 'disabled'}")
        return self.loop

    def stop(self):
        self.queue = asyncio.Queue()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None
        self.loop = False
        self.skip_votes.clear()
        if self.disconnect_timer:
            self.disconnect_timer.cancel()
            self.disconnect_timer = None
        self.bot.logger.log(MODULE_NAME, "Playback stopped and queue cleared")

    def is_playing(self):
        return self.voice_client and (self.voice_client.is_playing() or self.paused)

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up player module")

    find_best_match, select_best_candidate, FORMATS = import_archive_functions()

    if not find_best_match or not select_best_candidate:
        bot.logger.error(MODULE_NAME, "Failed to import required functions from archive")
        return

    import shutil as _shutil
    if not _shutil.which("ffmpeg"):
        bot.logger.log(
            MODULE_NAME,
            " FFmpeg not found on PATH — music playback will not work. "
            "Install FFmpeg and ensure it is accessible from PATH.",
            "WARNING",
        )

    bot.music_players = {}

    @bot.listen()
    async def on_voice_state_update(member, before, after):
        if member.id != bot.user.id:
            return

        guild_id = member.guild.id
        if guild_id not in bot.music_players:
            return

        player = bot.music_players[guild_id]

        if not after.channel or (before.channel and not after.channel):
            try:
                player.stop()
                if player.voice_client:
                    await player.voice_client.disconnect()
                if guild_id in bot.music_players:
                    del bot.music_players[guild_id]
                bot.logger.log(MODULE_NAME, f"Cleaned up player for guild {guild_id}")
            except Exception as e:
                bot.logger.error(MODULE_NAME, "Voice disconnect cleanup error", e)
        elif before.channel and after.channel and before.channel != after.channel:
            player.voice_client = member.guild.voice_client

    @bot.tree.command(name="play", description="Play a song — archive name or YouTube/SoundCloud URL")
    @app_commands.describe(
        song_name="Song name to search archive, or a YouTube/SoundCloud URL",
        format="File format (FLAC or MP3) — ignored for URLs",
        version="Specific version (optional, archive only)"
    )
    @app_commands.choices(format=[app_commands.Choice(name=fmt, value=fmt) for fmt in FORMATS])
    async def play(interaction: discord.Interaction, song_name: str, format: Optional[str] = None, version: Optional[str] = None):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to use this command!",
                ephemeral=True
            )
            return

        voice_channel = interaction.user.voice.channel
        is_url = song_name.startswith('http://') or song_name.startswith('https://')

        if is_url:
            await interaction.response.defer(thinking=True)
            bot.logger.log(MODULE_NAME, f"Play URL: {song_name}")

            # Resolve stream URL and metadata via yt-dlp
            try:
                proc = await asyncio.create_subprocess_exec(
                    'yt-dlp', '--get-title', '--get-duration',
                    '--no-playlist', '--quiet', song_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                await interaction.followup.send("yt-dlp timed out resolving that URL.", ephemeral=True)
                bot.logger.log(MODULE_NAME, "yt-dlp timed out", "WARNING")
                return
            except Exception as e:
                await interaction.followup.send("Failed to run yt-dlp.", ephemeral=True)
                bot.logger.error(MODULE_NAME, "yt-dlp subprocess error", e)
                return

            if proc.returncode != 0:
                err = stderr.decode(errors='replace').strip()
                await interaction.followup.send(f"yt-dlp couldn't resolve that URL.", ephemeral=True)
                bot.logger.log(MODULE_NAME, f"yt-dlp failed: {err}", "WARNING")
                return

            lines = stdout.decode(errors='replace').strip().splitlines()
            if len(lines) < 2:
                await interaction.followup.send("Couldn't parse yt-dlp output.", ephemeral=True)
                bot.logger.log(MODULE_NAME, f"Unexpected yt-dlp output: {lines}", "WARNING")
                return

            title = lines[0]
            try:
                duration = int(lines[1])
            except ValueError:
                duration = 0

            song = UrlSong(
                url=song_name,
                title=title,
                duration=duration,
                requested_by=interaction.user
            )

        else:
            if not format:
                await interaction.response.send_message(
                    "Please specify a format (FLAC or MP3) when searching by name.",
                    ephemeral=True
                )
                return

            if not hasattr(bot, 'ARCHIVE_manager') or not bot.ARCHIVE_manager.song_index_ready.is_set():
                await interaction.response.send_message(
                    "Music index not ready—please try again shortly.",
                    ephemeral=True
                )
                bot.logger.log(MODULE_NAME, "Index not ready", "WARNING")
                return

            await interaction.response.defer(thinking=True)
            bot.logger.log(MODULE_NAME, f"Play command: '{song_name}' (Format: {format}, Version: {version})")

            key = find_best_match(bot.ARCHIVE_manager.song_index, format, song_name)
            if not key:
                await interaction.followup.send(
                    f"Song not found: '{song_name}' in {format}",
                    ephemeral=True
                )
                bot.logger.log(MODULE_NAME, f"Song not found: '{song_name}'", "WARNING")
                return

            candidates = bot.ARCHIVE_manager.song_index[format][key]
            best = select_best_candidate(candidates, version)

            if not best:
                error_msg = f"Version '{version}' not found for '{song_name}'" if version else f"Song not found: '{song_name}'"
                await interaction.followup.send(error_msg, ephemeral=True)
                bot.logger.log(MODULE_NAME, f"Version not found: {version}", "WARNING")
                return

            song = Song(
                file_path=best['path'],
                metadata=best['metadata'],
                requested_by=interaction.user
            )

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
                    f"Failed to join voice channel: {str(e)}",
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

        await player.add_to_queue(song, interaction)

    @bot.tree.command(name="stop", description="Stop playback and clear queue")
    async def stop(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player or not player.is_playing():
            await interaction.response.send_message(
                "Nothing is playing",
                ephemeral=True
            )
            return

        player.stop()
        await interaction.response.send_message("⏹ Stopped playback and cleared queue")
        bot.logger.log(MODULE_NAME, f"Playback stopped by {interaction.user}")

    @bot.tree.command(name="skip", description="Skip current song (vote-based)")
    async def skip(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player or not player.is_playing():
            await interaction.response.send_message(
                "Nothing is playing",
                ephemeral=True
            )
            return

        member_count = len([m for m in player.voice_client.channel.members if not m.bot])

        if interaction.user.guild_permissions.administrator or member_count <= 1:
            player.skip()
            await interaction.response.send_message("⏭ Skipped current song")
            bot.logger.log(MODULE_NAME, f"Admin/solo skip by {interaction.user}")
            return

        if player.skip(interaction.user.id):
            await interaction.response.send_message("⏭ Skipped current song (vote passed)")
            bot.logger.log(MODULE_NAME, f"Vote skip passed in guild {guild_id}")
        else:
            required = max(2, member_count // 2)
            await interaction.response.send_message(
                f"Vote to skip: {len(player.skip_votes)}/{required} votes"
            )

    @bot.tree.command(name="pause", description="Pause playback")
    async def pause(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player or not player.voice_client:
            await interaction.response.send_message(
                "Nothing is playing",
                ephemeral=True
            )
            return

        if not player.voice_client.is_playing():
            await interaction.response.send_message(
                "Nothing is playing",
                ephemeral=True
            )
            return

        if player.paused:
            await interaction.response.send_message(
                "Already paused",
                ephemeral=True
            )
            return

        player.pause()
        await interaction.response.send_message("⏸ Playback paused")
        bot.logger.log(MODULE_NAME, f"Playback paused by {interaction.user}")

    @bot.tree.command(name="resume", description="Resume playback")
    async def resume(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player or not player.paused:
            await interaction.response.send_message(
                "Playback not paused",
                ephemeral=True
            )
            return

        player.resume()
        await interaction.response.send_message("▶ Playback resumed")
        bot.logger.log(MODULE_NAME, f"Playback resumed by {interaction.user}")

    @bot.tree.command(name="queue", description="Show current queue")
    async def show_queue(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player:
            await interaction.response.send_message(
                "No active player",
                ephemeral=True
            )
            return

        if not player.current and player.queue.empty():
            await interaction.response.send_message(
                "Queue is empty",
                ephemeral=True
            )
            return

        parts = ["# 🎵 Music Queue"]

        if player.current:
            duration = str(timedelta(seconds=player.current.duration)) if player.current.duration > 0 else "Unknown"
            parts.append(f"**Now Playing**\n{player.current.title} ({duration})\nRequested by {player.current.requested_by.mention}")

        if not player.queue.empty():
            queue_items = []
            while not player.queue.empty():
                try:
                    queue_items.append(player.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                lines = []
                for i, song in enumerate(queue_items[:10], 1):
                    duration = str(timedelta(seconds=song.duration)) if song.duration > 0 else "?"
                    lines.append(f"{i}. **{song.title}** ({duration}) - {song.requested_by.mention}")
            finally:
                for item in queue_items:
                    await player.queue.put(item)

            parts.append(f"**Up Next ({len(queue_items)} songs)**\n" + "\n".join(lines))

        footer = f"Loop: {'🔁 ON' if player.loop else '⏹ OFF'}"
        if len(queue_items) > 10:
            footer = f"Plus {len(queue_items) - 10} more songs..."

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n\n".join(parts)),
            accent_color=0x3498db
        ))
        view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        view.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        await interaction.response.send_message(view=view)

    @bot.tree.command(name="loop", description="Toggle loop mode for current song")
    async def toggle_loop(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player:
            await interaction.response.send_message(
                "No active player",
                ephemeral=True
            )
            return

        state = player.toggle_loop()
        await interaction.response.send_message(
            f"Loop {'enabled' if state else 'disabled'}"
        )
        bot.logger.log(MODULE_NAME, f"Loop {'enabled' if state else 'disabled'} by {interaction.user}")

    @bot.tree.command(name="leave", description="Disconnect bot from voice channel")
    async def leave(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        player = bot.music_players.get(guild_id)

        if not player or not player.voice_client:
            await interaction.response.send_message(
                "Not in a voice channel",
                ephemeral=True
            )
            return

        if not interaction.user.guild_permissions.administrator:
            if not interaction.user.voice or interaction.user.voice.channel != player.voice_client.channel:
                await interaction.response.send_message(
                    "You must be in the same voice channel",
                    ephemeral=True
                )
                return

        player.stop()
        await player.voice_client.disconnect()
        del bot.music_players[guild_id]

        await interaction.response.send_message("Disconnected from voice channel")
        bot.logger.log(MODULE_NAME, f"Left voice channel in guild {guild_id}")

    @bot.listen()
    async def on_close():
        for guild_id, player in list(bot.music_players.items()):
            try:
                player.stop()
                if player.disconnect_timer and not player.disconnect_timer.done():
                    player.disconnect_timer.cancel()
                if player.voice_client and player.voice_client.is_connected():
                    await player.voice_client.disconnect(force=True)
            except Exception:
                pass
        bot.music_players.clear()
        bot.logger.log(MODULE_NAME, "Player shutdown complete")

    bot.logger.log(MODULE_NAME, "Player module setup complete")
