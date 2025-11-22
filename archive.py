import os
import re
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from io import BytesIO
import asyncio
import difflib
from discord import app_commands
import discord
from discord.ext import tasks
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor  # ✅ REMOVED DUPLICATE IMPORTS

METADATA_EXECUTOR = ThreadPoolExecutor(max_workers=4)
MODULE_NAME = "ARCHIVE"

# Configuration
EMINEM_ROOT = Path(r"D:\Media\Music\Eminem")
FORMATS = ["FLAC", "MP3"]
CACHE_CHANNEL_NAME = "songcache"
INDEX_FILE = "song_index.json"
CACHE_INDEX = "cache_index.json"
INDEX_REFRESH_HOURS = 24
CACHE_EXPIRE_DAYS = 7
VERSION_KEYWORDS = ['live', 'remix', 'demo', 'acoustic', 'version', 'edit', 'radio']
SPECIAL_FOLDERS = {
    "8 - Features": "Feature",
    "7 - Singles": "Single",
    "10 - Freestyles (MP3 Only)": "Freestyle",
    "11 - Leaks (Mostly MP3)": "Leak"
}
LARGE_FILE_MSG = "Sorry! The song file was too big to upload in the server."
MAX_SEARCH_RESULTS = 5

async def extract_metadata_async(file_path):
    """Extract metadata from audio files using Mutagen in a non-blocking way"""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(METADATA_EXECUTOR, extract_metadata_sync, file_path)
    except Exception as e:
        print(f"Async metadata error for {file_path}: {e}")
        return None

def extract_metadata_sync(file_path):
    """Synchronous metadata extraction (run in executor)"""
    try:
        if file_path.lower().endswith('.flac'):
            audio = FLAC(file_path)
            return {
                'title': audio.get('title', [''])[0],
                'album': audio.get('album', [''])[0],
                'artist': audio.get('artist', [''])[0],
                'year': audio.get('date', [''])[0].split('-')[0] if audio.get('date') else '',
            }
        elif file_path.lower().endswith('.mp3'):
            try:
                audio = MP3(file_path, ID3=ID3)
                tags = audio.tags
                if not tags:
                    return None
                return {
                    'title': tags['TIT2'].text[0] if 'TIT2' in tags else '',
                    'album': tags['TALB'].text[0] if 'TALB' in tags else '',
                    'artist': tags['TPE1'].text[0] if 'TPE1' in tags else '',
                    'year': str(tags['TDRC'].text[0]) if 'TDRC' in tags and tags['TDRC'].text else '',
                }
            except Exception as mp3_error:
                # If MP3 metadata extraction fails, fall back to filename
                print(f"MP3 metadata error for {file_path}: {mp3_error}")
                return {
                    'title': Path(file_path).stem,
                    'album': 'Unknown',
                    'artist': 'Eminem', 
                    'year': ''
                }
    except Exception as e:
        print(f"General metadata error for {file_path}: {e}")
    return None

def fallback_metadata(file_path):
    """Fallback metadata when extraction fails"""
    return {
        'title': Path(file_path).stem,
        'album': 'Unknown',
        'artist': 'Eminem', 
        'year': ''
    }

def extract_artwork(file_path):
    """Extract cover art from FLAC or MP3"""
    try:
        if file_path.lower().endswith('.flac'):
            audio = FLAC(file_path)
            if audio.pictures:
                return audio.pictures[0].data
        elif file_path.lower().endswith('.mp3'):
            audio = MP3(file_path, ID3=ID3)
            if audio.tags:
                for tag in audio.tags.values():
                    if getattr(tag, "FrameID", None) == 'APIC':
                        return tag.data
    except Exception:
        pass
    return None


def handle_special_folder(file_path, metadata, folder_name):
    """Apply special metadata parsing for specific folders"""
    if not metadata:
        metadata = {
            'title': Path(file_path).stem,
            'album': folder_name,
            'artist': 'Eminem',
            'year': ''
        }

    for folder_key, folder_type in SPECIAL_FOLDERS.items():
        if folder_key in folder_name:
            metadata['album'] = folder_type
            ym = re.search(r'\((\d{4})\)', folder_name)
            if ym:
                metadata['year'] = ym.group(1)

            if folder_key == "8 - Features":
                feat1 = re.match(r'\((\d{4})\)\s*(.+?)\s*-\s*(.+?)\s*\(feat', metadata.get('title', ''))
                if feat1:
                    metadata['year'], metadata['artist'], metadata['title'] = feat1.groups()
                else:
                    feat2 = re.match(r'(.+?)\s*-\s*(.+?)\s*\(feat', metadata.get('title', ''))
                    if feat2:
                        metadata['artist'], metadata['title'] = feat2.groups()
            elif folder_key in ["7 - Singles", "10 - Freestyles (MP3 Only)"]:
                sm = re.match(r'\((\d{4})\)\s*(.+)', metadata.get('title', ''))
                if sm:
                    metadata['year'], metadata['title'] = sm.groups()
            elif folder_key == "11 - Leaks (Mostly MP3)":
                era = re.search(r'\((\d{4}-\d{4})\)', folder_name)
                if era:
                    metadata['year'] = era.group(1)
                    metadata['album'] = "Leak"

    if not metadata.get('artist') or 'eminem' not in metadata['artist'].lower():
        metadata['artist'] = "Eminem"
    return metadata


def normalize_title(title):
    """Clean and normalize titles for searching"""
    t = re.sub(r'^(\d+\s*-\s*)?\d+\s+', '', title)
    t = re.sub(r'[({\[].*?[)}\]](?=\s*$)', '', t)
    t = re.sub(r'\b(?:feat\.?|ft\.?|with)\s+.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.casefold()


async def check_file_modifications(bot):  # ✅ ADDED BOT PARAMETER
    """Check if any music files have been modified since last index"""
    if not Path(INDEX_FILE).exists():
        return True
    
    try:
        # Get index file modification time
        last_index_time = datetime.fromtimestamp(Path(INDEX_FILE).stat().st_mtime, timezone.utc)
        
        # Check if index is older than refresh threshold
        if datetime.now(timezone.utc) - last_index_time > timedelta(hours=INDEX_REFRESH_HOURS):
            bot.logger.log(MODULE_NAME, f"Index older than {INDEX_REFRESH_HOURS} hours, needs refresh")
            return True
        
        # Only check for actual file additions/deletions, not modifications
        # This prevents unnecessary rebuilds when file timestamps change
        try:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                index_data = json.load(f)
                songs = index_data.get('songs', {})
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not read index file: {e}", "WARNING")
            return True
        
        # Count files in current filesystem
        current_file_count = 0
        for fmt in FORMATS:
            fmt_path = EMINEM_ROOT / fmt
            if not fmt_path.exists():
                continue
            for root, _, files in os.walk(fmt_path):
                for file in files:
                    if file.lower().endswith(('.flac', '.mp3')):
                        current_file_count += 1
        
        # Count files in index
        index_file_count = 0
        for fmt in FORMATS:
            if fmt in songs:
                for entries in songs[fmt].values():
                    index_file_count += len(entries)
        
        # Only rebuild if file counts differ (files added or removed)
        if current_file_count != index_file_count:
            bot.logger.log(MODULE_NAME, 
                f"File count changed: {index_file_count} → {current_file_count}, rebuilding index")
            return True
        
        bot.logger.log(MODULE_NAME, 
            f"Index up-to-date: {current_file_count} files, age: {(datetime.now(timezone.utc) - last_index_time).total_seconds()/3600:.1f}h")
        return False
        
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Error checking file modifications", e)
        return False

async def build_song_index(bot):  # ✅ FIXED: Use parameter instead of global
    """Build the complete song index asynchronously"""
    bot.logger.log(MODULE_NAME, "Building song index...")
    song_index = {fmt: defaultdict(list) for fmt in FORMATS}
    total = 0

    for fmt in FORMATS:
        fmt_path = EMINEM_ROOT / fmt
        if not fmt_path.exists():
            bot.logger.log(MODULE_NAME, f"Format directory missing: {fmt_path}", "WARNING")
            continue
            
        bot.logger.log(MODULE_NAME, f"Scanning {fmt} directory...")
        
        # Collect all files first
        all_files = []
        for root, _, files in os.walk(fmt_path):
            folder = Path(root).name
            for fn in files:
                if not fn.lower().endswith(('.flac', '.mp3')):
                    continue
                full_path = Path(root) / fn
                all_files.append((full_path, folder))
        
        # Process files in batches with delays to prevent blocking
        batch_size = 50
        for i in range(0, len(all_files), batch_size):
            batch = all_files[i:i + batch_size]
            tasks = []
            
            for full_path, folder in batch:
                tasks.append(process_single_file(bot, full_path, folder, song_index, fmt))
            
            # Process batch and allow event loop to run
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total += len([r for r in results if r is not None])
            
            # Small delay to prevent blocking
            if i + batch_size < len(all_files):
                await asyncio.sleep(0.1)
            
            bot.logger.log(MODULE_NAME, f"Processed {min(i + batch_size, len(all_files))}/{len(all_files)} files...")

    tmp = INDEX_FILE + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({
                'version': 5,
                'created_at': datetime.utcnow().isoformat(),
                'songs': {k: dict(v) for k, v in song_index.items()}
            }, f, ensure_ascii=False, indent=2)
        
        os.replace(tmp, INDEX_FILE)
        bot.logger.log(MODULE_NAME, f"Indexed {total} songs total")
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to save index file", e)
        if os.path.exists(tmp):
            os.remove(tmp)
    return song_index

async def process_single_file(bot, full_path, folder, song_index, fmt):
    """Process a single file asynchronously"""
    try:
        md = await extract_metadata_async(str(full_path))
        
        if any(k in folder for k in SPECIAL_FOLDERS):
            md = handle_special_folder(str(full_path), md, folder)
            
        if not md:
            md = {'title': full_path.stem, 'album': folder, 'artist': 'Eminem', 'year': ''}
            
        key = normalize_title(md['title'])
        song_index[fmt][key].append({
            'path': str(full_path),
            'original_title': full_path.stem,
            'folder': folder,
            'metadata': md
        })
        return True
    except Exception as e:
        bot.logger.error(MODULE_NAME, f"Error processing {full_path}", e)
        return None

def load_song_index(bot):
    """Load the song index from file"""
    if not Path(INDEX_FILE).exists():
        bot.logger.log(MODULE_NAME, "Index file not found", "WARNING")
        return None
        
    try:
        with open(INDEX_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        if data.get('version', 0) < 5:
            bot.logger.log(MODULE_NAME, "Outdated index version", "WARNING")
            return None
            
        created = datetime.fromisoformat(data['created_at'])
        if datetime.utcnow() - created < timedelta(hours=INDEX_REFRESH_HOURS):
            bot.logger.log(MODULE_NAME, "Loaded index from cache")
            return data['songs']
            
        bot.logger.log(MODULE_NAME, "Index is outdated, needs refresh")
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Error loading index", e)
    return None


def find_best_match(idx, fmt, query):
    """Find the closest matching song title"""
    key = normalize_title(query)
    matches = difflib.get_close_matches(key, idx.get(fmt, {}).keys(), n=MAX_SEARCH_RESULTS, cutoff=0.5)
    return matches[0] if matches else None


def select_best_candidate(cands, version=None):
    """Select the best version of a song"""
    if version:
        vl = version.lower()
        filtered = [c for c in cands if vl in c['original_title'].lower() or vl in c['folder'].lower()]
        if not filtered:
            return None
        cands = filtered

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


async def get_cached_url(bot, file_path):
    """Get or create a cached Discord URL for a file"""
    p = Path(file_path)
    key = str(p.resolve())
    
    try:
        with open(CACHE_INDEX, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except FileNotFoundError:
        cache = {}
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Error loading cache index", e)
        cache = {}

    now = datetime.utcnow()
    if key in cache:
        cache_time = datetime.fromisoformat(cache[key]['timestamp'])
        if now - cache_time < timedelta(days=CACHE_EXPIRE_DAYS):
            bot.logger.log(MODULE_NAME, f"Using cached URL for {p.name}")
            return cache[key]['url']

    chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
    if not chan:
        bot.logger.log(MODULE_NAME, f"Missing channel {CACHE_CHANNEL_NAME}", "WARNING")
        return None
        
    if not p.exists():
        bot.logger.error(MODULE_NAME, f"File not found: {file_path}")
        return None

    try:
        bot.logger.log(MODULE_NAME, f"Uploading {p.name} to cache channel")
        mf = discord.File(p, filename=p.name)
        msg = await chan.send(file=mf)
        url = msg.attachments[0].url
        cache[key] = {'url': url, 'timestamp': now.isoformat(), 'message_id': msg.id}
        
        try:
            with open(CACHE_INDEX, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            bot.logger.log(MODULE_NAME, f"Cached new URL for {p.name}")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to save cache index", e)
            
        return url

    except discord.HTTPException as e:
        if e.status == 413:
            bot.logger.log(MODULE_NAME, f"File too large: {p.name} ({p.stat().st_size/1024/1024:.2f} MB)", "WARNING")
            return "FILE_TOO_LARGE"
        bot.logger.error(MODULE_NAME, f"Upload failed", e)
        return None
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Unexpected upload error", e)
        return None


async def send_song_embed(bot, user, metadata, url, file_path):
    """Send song info as an embed"""
    art = extract_artwork(file_path)
    embed = discord.Embed(
        title=metadata.get('title', 'Unknown Track'),
        description=f"**Artist:** {metadata.get('artist', 'Eminem')}",
        color=0x1abc9c,
        url=url
    )
    
    if metadata.get('album'):
        embed.add_field(name="Album", value=metadata['album'], inline=True)
    if metadata.get('year'):
        embed.add_field(name="Year", value=metadata['year'], inline=True)
    embed.add_field(name="Download", value=f"[Click Here]({url})", inline=False)
    embed.set_footer(text="Link expires when the cache purges")

    if art:
        try:
            fobj = discord.File(BytesIO(art), filename="cover.jpg")
            embed.set_thumbnail(url="attachment://cover.jpg")
            await user.send(file=fobj, embed=embed)
            return
        except Exception as e:
            bot.logger.log(MODULE_NAME, "Failed to attach artwork", "WARNING")
    await user.send(embed=embed)


async def send_bot_log(bot, log_data):
    """Send command execution details to bot-logs channel"""
    try:
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            bot.logger.log(MODULE_NAME, "bot-logs channel not found", "WARNING")
            return
            
        embed = discord.Embed(
            title="Command Execution Log",
            color=0x3498db if log_data.get('success') else 0xe74c3c,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="User", value=f"{log_data['user']} ({log_data['user_id']})", inline=False)
        
        if 'action' in log_data:
            embed.add_field(name="Command", value=log_data['action'], inline=False)
        
        if 'params' in log_data:
            params = "\n".join([f"• {k}: {v}" for k, v in log_data['params'].items()])
            embed.add_field(name="Parameters", value=params, inline=False)
        
        status = "✅ SUCCESS" if log_data['success'] else "❌ FAILURE"
        embed.add_field(name="Status", value=status, inline=True)
        
        if log_data['success'] and 'song_metadata' in log_data:
            song = log_data['song_metadata']
            song_info = (
                f"**Title:** {song.get('title', 'Unknown')}\n"
                f"**Artist:** {song.get('artist', 'Eminem')}\n"
                f"**Album:** {song.get('album', 'Unknown')}\n"
                f"**Year:** {song.get('year', 'N/A')}"
            )
            embed.add_field(name="Song Details", value=song_info, inline=False)
            embed.add_field(name="File Path", value=f"`{log_data.get('file_path', '')}`", inline=False)
        elif not log_data['success']:
            embed.add_field(name="Error", value=f"```{log_data.get('error', 'Unknown error')}```", inline=False)
        
        await channel.send(embed=embed)
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to send log", e)


async def log_command_execution(bot, interaction, command_data, success, song_metadata=None, file_path=None, error=None):
    """Log command details to console and bot-logs channel"""
    user_info = f"{interaction.user} ({interaction.user.id})"
    bot.logger.log(MODULE_NAME, f"Command from {user_info}")
    
    if success:
        title = song_metadata.get('title', 'Unknown')
        bot.logger.log(MODULE_NAME, f"Sent song: {title}")
    else:
        bot.logger.error(MODULE_NAME, f"Command error: {error}")
    
    log_data = {
        'user': str(interaction.user),
        'user_id': interaction.user.id,
        'success': success,
        'error': str(error) if error else '',
        'action': 'ARCHIVE',
        'params': command_data
    }
    
    if success:
        log_data['song_metadata'] = song_metadata
        log_data['file_path'] = file_path
    
    await send_bot_log(bot, log_data)


class ARCHIVEManager:
    """Manages the ARCHIVE system"""
    
    def __init__(self, bot):
        self.bot = bot
        self.song_index = None
        self.song_index_ready = asyncio.Event()
        self.initialization_task = None
    
    async def initialize(self):
        """Initialize the song index in background"""
        self.bot.logger.log(MODULE_NAME, "Starting song index initialization...")
        self.initialization_task = asyncio.create_task(self._initialize_background())
    
    async def _initialize_background(self):
        """Background initialization that won't block the bot"""
        try:
            self.song_index = load_song_index(self.bot)
            if not self.song_index:
                self.bot.logger.log(MODULE_NAME, "Building new song index in background...")
                self.song_index = await build_song_index(self.bot)
            self.song_index_ready.set()
            self.bot.logger.log(MODULE_NAME, "Song index ready")
            
            # Start cache purge task
            self.cache_purge_loop.start()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Background initialization failed", e)
    
    async def ensure_ready(self):
        """Ensure the index is ready, wait if necessary"""
        if not self.song_index_ready.is_set() and self.initialization_task:
            await self.song_index_ready.wait()
    
    @tasks.loop(hours=CACHE_EXPIRE_DAYS * 24)
    async def cache_purge_loop(self):
        """Periodic task to purge old cache and refresh index"""
        self.bot.logger.log(MODULE_NAME, "Running cache purge task")
        try:
            chan = discord.utils.get(self.bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
            if chan:
                cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_EXPIRE_DAYS)
                await chan.purge(before=cutoff, limit=None)
                self.bot.logger.log(MODULE_NAME, "Cache purged")
            
            # ✅ FIXED: Pass self.bot to the function
            if await check_file_modifications(self.bot):
                self.bot.logger.log(MODULE_NAME, "File modifications detected, rebuilding index")
                self.song_index_ready.clear()
                self.song_index = await build_song_index(self.bot)
                self.song_index_ready.set()
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Purge task error", e)
    
    @cache_purge_loop.before_loop
    async def before_cache_purge(self):
        """Wait until bot is ready before starting the loop"""
        await self.bot.wait_until_ready()

def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up ARCHIVE module")
    
    # Initialize the manager
    ARCHIVE_manager = ARCHIVEManager(bot)
    
    # Store reference on bot for access in commands
    bot.ARCHIVE_manager = ARCHIVE_manager
    
    # ✅ REMOVED UNDEFINED EVENT
    
    # Schedule initialization
    asyncio.create_task(ARCHIVE_manager.initialize())
    
    @bot.tree.command(name="archive", description="Get a song from Eminem's archive")
    @app_commands.describe(
        format="File format",
        song_name="Name of the song",
        version="Specific version (optional)"
    )
    @app_commands.choices(format=[app_commands.Choice(name=fmt, value=fmt) for fmt in FORMATS])
    async def ARCHIVE(interaction: discord.Interaction, format: str, song_name: str, version: Optional[str] = None):
        """Main ARCHIVE command"""
        command_data = {
            'format': format,
            'song_name': song_name,
            'version': version if version else 'N/A'
        }
        
        # Use ensure_ready instead of direct check
        await ARCHIVE_manager.ensure_ready()
        
        if not ARCHIVE_manager.song_index_ready.is_set():
            bot.logger.log(MODULE_NAME, f"Index not ready when requested by {interaction.user}", "WARNING")  # ✅ FIXED BOT REFERENCE
            await interaction.response.send_message("🔄 Initializing—please try again shortly.", ephemeral=True)
            await log_command_execution(bot, interaction, command_data, False, error="Index not ready")
            return
            
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.logger.log(MODULE_NAME, f"Command: '{song_name}' (Format: {format}, Version: {version})")

        # Rest of your ARCHIVE command remains the same...
        key = find_best_match(ARCHIVE_manager.song_index, format, song_name)
        if not key:
            bot.logger.log(MODULE_NAME, f"Song not found: '{song_name}' in {format}", "WARNING")  # ✅ FIXED BOT REFERENCE
            await interaction.followup.send(f"❌ '{song_name}' not found in {format}", ephemeral=True)
            await log_command_execution(bot, interaction, command_data, False, error="Song not found")
            return

        candidates = ARCHIVE_manager.song_index[format][key]
        best = select_best_candidate(candidates, version)
        if not best:
            bot.logger.log(MODULE_NAME, f"Version not found: '{song_name}' version '{version}'", "WARNING")  # ✅ FIXED BOT REFERENCE
            error_msg = f"❌ '{song_name}'"
            if version:
                error_msg += f" (version '{version}')"
            error_msg += " not found."
            await interaction.followup.send(error_msg, ephemeral=True)
            await log_command_execution(bot, interaction, command_data, False, error="Version not found")
            return

        try:
            bot.logger.log(MODULE_NAME, f"Selected song: {best['original_title']}")  # ✅ FIXED BOT REFERENCE
            url = await get_cached_url(bot, best['path'])
            if url == "FILE_TOO_LARGE":
                bot.logger.log(MODULE_NAME, f"File too large: {best['path']}", "WARNING")  # ✅ FIXED BOT REFERENCE
                await interaction.followup.send(LARGE_FILE_MSG, ephemeral=True)
                await log_command_execution(bot, interaction, command_data, False, error="File too large")
                return
                
            if not url:
                bot.logger.error(MODULE_NAME, f"Cache failed for: {best['path']}")  # ✅ FIXED BOT REFERENCE
                await interaction.followup.send("❌ Failed to retrieve song.", ephemeral=True)
                await log_command_execution(bot, interaction, command_data, False, error="Cache failed")
                return

            await send_song_embed(bot, interaction.user, best['metadata'], url, best['path'])
            await interaction.followup.send("✅ Check your DMs for the song link!", ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                True, 
                song_metadata=best['metadata'], 
                file_path=best['path']
            )
            bot.logger.log(MODULE_NAME, "Song sent successfully")  # ✅ FIXED BOT REFERENCE
                                      
        except discord.Forbidden:
            bot.logger.log(MODULE_NAME, f"DM blocked for user: {interaction.user}", "WARNING")  # ✅ FIXED BOT REFERENCE
            await interaction.followup.send(
                "❌ I couldn't send you a DM. Please enable DMs from server members.",
                ephemeral=True
            )
            await log_command_execution(bot, interaction, command_data, False, error="DM blocked")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Unexpected error in ARCHIVE command", e)  # ✅ FIXED BOT REFERENCE
            await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)
            await log_command_execution(bot, interaction, command_data, False, error=str(e))

    @bot.tree.command(name="rebuild_index", description="[Admin] Rebuild song index")
    async def rebuild_index(interaction: discord.Interaction):
        """Admin command to rebuild index"""
        if not interaction.user.guild_permissions.administrator:
            bot.logger.log(MODULE_NAME, f"Unauthorized rebuild attempt by {interaction.user}", "WARNING")  # ✅ FIXED BOT REFERENCE
            await interaction.response.send_message(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True
            )
            return
            
        bot.logger.log(MODULE_NAME, f"Rebuilding index requested by {interaction.user}")  # ✅ FIXED BOT REFERENCE
        await interaction.response.send_message("🔄 Rebuilding song index...", ephemeral=True)
        try:
            ARCHIVE_manager.song_index_ready.clear()
            ARCHIVE_manager.song_index = await build_song_index(bot)
            ARCHIVE_manager.song_index_ready.set()
            await interaction.followup.send("✅ Song index rebuilt successfully!", ephemeral=True)
            bot.logger.log(MODULE_NAME, "Index rebuilt successfully")  # ✅ FIXED BOT REFERENCE
            
            await send_bot_log(bot, {
                'user': str(interaction.user),
                'user_id': interaction.user.id,
                'success': True,
                'action': 'rebuild_index'
            })
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Index rebuild failed", e)  # ✅ FIXED BOT REFERENCE
            await interaction.followup.send("❌ Failed to rebuild index.", ephemeral=True)
            
            await send_bot_log(bot, {
                'user': str(interaction.user),
                'user_id': interaction.user.id,
                'success': False,
                'error': str(e),
                'action': 'rebuild_index'
            })

    bot.logger.log(MODULE_NAME, "ARCHIVE module setup complete")