# emarchive.py
import os
import re
import difflib
import json
import discord
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from discord import app_commands
from typing import Optional
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from io import BytesIO

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
LARGE_FILE_MSG = (
    "Sorry! The song file was too big to upload in the server. "
    "You can find the file here: "
    "https://docs.google.com/document/d/179l9aN3Y5gStie83tI-oS9dwE45UoYtU/edit?tab=t.0#heading=h.gjdgxs"
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('emarchive')

def extract_metadata(file_path):
    """Extract metadata from audio files using Mutagen."""
    try:
        logger.debug(f"[METADATA] Extracting metadata from: {file_path}")
        if file_path.lower().endswith('.flac'):
            audio = FLAC(file_path)
            return {
                'title': audio.get('title', [''])[0],
                'album': audio.get('album', [''])[0],
                'artist': audio.get('artist', [''])[0],
                'year': audio.get('date', [''])[0].split('-')[0],
            }
        elif file_path.lower().endswith('.mp3'):
            audio = MP3(file_path, ID3=ID3)
            tags = audio.tags
            if not tags:
                logger.warning(f"[METADATA] No tags found in MP3: {file_path}")
                return None
            return {
                'title': tags['TIT2'].text[0] if 'TIT2' in tags else '',
                'album': tags['TALB'].text[0] if 'TALB' in tags else '',
                'artist': tags['TPE1'].text[0] if 'TPE1' in tags else '',
                'year': str(tags['TDRC'].text[0]) if 'TDRC' in tags and tags['TDRC'].text else '',
            }
    except Exception as e:
        logger.error(f"[METADATA] Error extracting metadata from {file_path}: {e}")
    return None

def extract_artwork(file_path):
    """Extract cover art from FLAC or MP3."""
    try:
        logger.debug(f"[ARTWORK] Extracting artwork from: {file_path}")
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
        logger.debug(f"[ARTWORK] No artwork found in: {file_path}")
    except Exception as e:
        logger.error(f"[ARTWORK] Error extracting artwork from {file_path}: {e}")
    return None

def handle_special_folder(file_path, metadata, folder_name):
    """Apply special metadata parsing for specific folders."""
    logger.debug(f"[SPECIAL] Handling special folder: {folder_name} for {file_path}")
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
                feat1 = re.match(r'\((\d{4})\)\s*(.+?)\s*-\s*(.+?)\s*\(feat', metadata.get('title',''))
                if feat1:
                    metadata['year'], metadata['artist'], metadata['title'] = feat1.groups()
                else:
                    feat2 = re.match(r'(.+?)\s*-\s*(.+?)\s*\(feat', metadata.get('title',''))
                    if feat2:
                        metadata['artist'], metadata['title'] = feat2.groups()
            elif folder_key in ["7 - Singles", "10 - Freestyles (MP3 Only)"]:
                sm = re.match(r'\((\d{4})\)\s*(.+)', metadata.get('title',''))
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
    """Clean and normalize titles for searching."""
    t = re.sub(r'^(\d+\s*-\s*)?\d+\s+', '', title)
    t = re.sub(r'[({\[].*?[)}\]](?=\s*$)', '', t)
    t = re.sub(r'\b(?:feat\.?|ft\.?|with)\s+.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.casefold()

async def build_song_index(bot):
    logger.info("[EMARCHIVE] Building song index...")
    song_index = {fmt: {} for fmt in FORMATS}
    total = 0

    for fmt in FORMATS:
        fmt_path = EMINEM_ROOT / fmt
        if not fmt_path.exists():
            logger.warning(f"[EMARCHIVE] Format directory missing: {fmt_path}")
            continue
        logger.info(f"[EMARCHIVE] Scanning {fmt} directory...")
        for root, _, files in os.walk(fmt_path):
            folder = Path(root).name
            logger.debug(f"[EMARCHIVE] Processing folder: {folder}")
            for fn in files:
                if not fn.lower().endswith(('.flac', '.mp3')):
                    continue
                full = Path(root) / fn
                logger.debug(f"[EMARCHIVE] Processing file: {fn}")
                md = extract_metadata(str(full))
                if any(k in folder for k in SPECIAL_FOLDERS):
                    logger.debug(f"[EMARCHIVE] Applying special folder rules to: {fn}")
                    md = handle_special_folder(str(full), md, folder)
                if not md:
                    md = {'title': full.stem, 'album': folder, 'artist': 'Eminem', 'year': ''}
                key = normalize_title(md['title'])
                song_index[fmt].setdefault(key, []).append({
                    'path': str(full),
                    'original_title': full.stem,
                    'folder': folder,
                    'metadata': md
                })
                total += 1
        logger.info(f"[EMARCHIVE] Processed {len(files)} files in {fmt} format")

    tmp = INDEX_FILE + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({
            'version': 4,
            'created_at': datetime.utcnow().isoformat(),
            'songs': song_index
        }, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_FILE)
    logger.info(f"[EMARCHIVE] Indexed {total} songs total")
    return song_index

def load_song_index():
    if not Path(INDEX_FILE).exists():
        logger.warning("[EMARCHIVE] Index file not found")
        return None
    try:
        with open(INDEX_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version', 0) < 4:
            logger.warning("[EMARCHIVE] Outdated index version")
            return None
        created = datetime.fromisoformat(data['created_at'])
        if datetime.utcnow() - created < timedelta(hours=INDEX_REFRESH_HOURS):
            logger.info("[EMARCHIVE] Loaded index from cache")
            return data['songs']
        logger.info("[EMARCHIVE] Index is outdated, needs refresh")
    except Exception as e:
        logger.error(f"[EMARCHIVE] Error loading index: {e}")
    return None

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
        # Penalize versions with keywords
        p = sum(1 for kw in VERSION_KEYWORDS if kw in c['original_title'].lower() or kw in c['folder'].lower())
        y = 9999
        try:
            yv = c['metadata'].get('year', '')
            y = int(yv[:4]) if yv and yv[:4].isdigit() else 9999
        except:
            pass
        scored.append((p, y, c))
    scored.sort(key=lambda x: (x[0], x[1]))
    
    best = scored[0][2] if scored else None
    if best:
        logger.debug(f"[SELECT] Selected: {best['original_title']} (folder: {best['folder']})")
    return best

async def get_cached_url(bot, file_path):
    logger.debug(f"[CACHE] Getting cached URL for: {file_path}")
    p = Path(file_path)
    key = str(p.resolve())
    try:
        with open(CACHE_INDEX, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except:
        cache = {}
        logger.debug("[CACHE] Cache index not found, starting new")

    now = datetime.utcnow()
    if key in cache:
        cache_time = datetime.fromisoformat(cache[key]['timestamp'])
        if now - cache_time < timedelta(days=CACHE_EXPIRE_DAYS):
            logger.debug(f"[CACHE] Using cached URL for {file_path}")
            return cache[key]['url']
        logger.debug(f"[CACHE] Cached URL expired for {file_path}")

    chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
    if not chan:
        logger.warning(f"[CACHE] Missing channel {CACHE_CHANNEL_NAME}")
        return None
    if not p.exists():
        logger.error(f"[CACHE] File not found: {file_path}")
        return None

    try:
        logger.info(f"[CACHE] Uploading {p.name} to cache channel")
        mf = discord.File(p, filename=p.name)
        msg = await chan.send(file=mf)
        url = msg.attachments[0].url
        cache[key] = {'url': url, 'timestamp': now.isoformat(), 'message_id': msg.id}
        
        with open(CACHE_INDEX, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        logger.info(f"[CACHE] Cached new URL for {file_path}")
        return url

    except discord.HTTPException as e:
        if e.status == 413:  # Payload Too Large
            logger.warning(f"[CACHE] File too large: {p.name} ({p.stat().st_size/1024/1024:.2f} MB)")
            return "FILE_TOO_LARGE"
        logger.error(f"[CACHE] Upload failed: {e}")
        return None
    except Exception as e:
        logger.error(f"[CACHE] Unexpected error: {e}")
        return None

async def send_song_embed(user, metadata, url, file_path):
    logger.debug(f"[EMBED] Creating embed for {metadata.get('title')}")
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
            logger.debug("[EMBED] Adding artwork to embed")
            fobj = discord.File(BytesIO(art), filename="cover.jpg")
            embed.set_thumbnail(url="attachment://cover.jpg")
            await user.send(file=fobj, embed=embed)
            return
        except Exception as e:
            logger.warning(f"[EMBED] Failed to attach artwork: {e}")
    await user.send(embed=embed)
    logger.debug("[EMBED] Embed sent without artwork")

async def cache_purge_task(bot):
    logger.info("[PURGE] Starting cache purge task")
    while True:
        await asyncio.sleep(CACHE_EXPIRE_DAYS * 24 * 3600)
        try:
            logger.info("[PURGE] Purging cache...")
            chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
            if chan:
                await chan.purge(limit=None)
                logger.info("[PURGE] Cache channel purged")
            if Path(CACHE_INDEX).exists():
                os.remove(CACHE_INDEX)
                logger.info("[PURGE] Cache index removed")
            bot.song_index_ready.clear()
            bot.song_index = await build_song_index(bot)
            bot.song_index_ready.set()
            logger.info("[PURGE] Cache and index refreshed")
        except Exception as e:
            logger.error(f"[PURGE] Error purging cache: {e}")

async def send_bot_log(bot, log_data):
    """Send command execution details to bot-logs channel"""
    try:
        logger.debug("[BOT-LOG] Sending log to bot-logs channel")
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            logger.warning("[BOT-LOG] bot-logs channel not found")
            return
            
        embed = discord.Embed(
            title="Command Execution Log",
            color=0x3498db if log_data.get('success') else 0xe74c3c,
            timestamp=datetime.utcnow()
        )
        
        # Add command info
        if 'action' in log_data:
            embed.add_field(name="Command", value=log_data['action'], inline=False)
        else:
            embed.add_field(name="Command", value="emarchive", inline=False)
        
        # Add command parameters if available
        if 'params' in log_data:
            params = "\n".join([f"• {k}: {v}" for k, v in log_data['params'].items()])
            embed.add_field(name="Parameters", value=params, inline=False)
        
        # Add user info
        embed.add_field(name="User", value=f"{log_data['user']} ({log_data['user_id']})", inline=True)
        
        # Add result info
        status = "✅ SUCCESS" if log_data['success'] else "❌ FAILURE"
        embed.add_field(name="Status", value=status, inline=True)
        
        # Add song details if successful
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
        logger.info("[BOT-LOG] Log sent to bot-logs channel")
    except Exception as e:
        logger.error(f"[BOT-LOG] Failed to send log: {str(e)}")

async def log_command_execution(bot, interaction, command_data, success, song_metadata=None, file_path=None, error=None):
    """Log command details to console and bot-logs channel"""
    # Console logging
    user_info = f"{interaction.user} ({interaction.user.id})"
    logger.info(f"[COMMAND] User: {user_info}")
    logger.info(f"[COMMAND] Parameters: {command_data}")
    
    if success:
        title = song_metadata.get('title', 'Unknown')
        logger.info(f"[COMMAND] Sent song: {title}")
        logger.info(f"[COMMAND] Embed Details: Title={title}, "
                   f"Artist={song_metadata.get('artist', 'Eminem')}, "
                   f"Album={song_metadata.get('album', 'Unknown')}, "
                   f"Year={song_metadata.get('year', 'N/A')}")
    else:
        logger.error(f"[COMMAND] Error: {error}")
    
    # Prepare data for bot-logs
    log_data = {
        'user': str(interaction.user),
        'user_id': interaction.user.id,
        'success': success,
        'error': str(error) if error else ''
    }
    
    if 'params' in command_data:
        log_data['params'] = command_data
    if success:
        log_data['song_metadata'] = song_metadata
        log_data['file_path'] = file_path
    
    await send_bot_log(bot, log_data)

def setup_emarchive(bot):
    logger.info("[SETUP] Initializing emarchive extension")
    bot.song_index = None
    bot.song_index_ready = asyncio.Event()

    async def init_index():
        logger.info("[INIT] Loading song index...")
        bot.song_index = load_song_index()
        if not bot.song_index:
            logger.info("[INIT] Building new song index...")
            bot.song_index = await build_song_index(bot)
        bot.song_index_ready.set()
        logger.info("[INIT] Song index ready")
        asyncio.create_task(cache_purge_task(bot))

    original = getattr(bot, 'setup_hook', None)
    async def new_hook():
        if original:
            await original()
        asyncio.create_task(init_index())
    bot.setup_hook = new_hook

    @bot.tree.command(name="emarchive", description="Get a song from Eminem's archive")
    @app_commands.describe(format="File format", song_name="Name of the song", version="Specific version (optional)")
    @app_commands.choices(format=[app_commands.Choice(name="FLAC", value="FLAC"), app_commands.Choice(name="MP3", value="MP3")])
    async def emarchive(interaction: discord.Interaction, format: str, song_name: str, version: Optional[str] = None):
        command_data = {
            'format': format,
            'song_name': song_name,
            'version': version if version else 'N/A'
        }
        
        if not bot.song_index_ready.is_set():
            logger.warning(f"[COMMAND] Index not ready when requested by {interaction.user}")
            await interaction.response.send_message("🔄 Initializing—please try again shortly.", ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                False, 
                error="Index not ready"
            )
            return
            
        await interaction.response.defer(ephemeral=True, thinking=True)
        logger.info(f"[COMMAND] Received: '{song_name}' (Format: {format}, Version: {version})")

        key = find_best_match(bot.song_index, format, song_name)
        if not key:
            logger.warning(f"[COMMAND] Song not found: '{song_name}' in {format}")
            error_msg = f"❌ '{song_name}' not found in {format}"
            await interaction.followup.send(error_msg, ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                False, 
                error="Song not found"
            )
            return

        c = bot.song_index[format][key]
        best = select_best_candidate(c, version)
        if not best:
            logger.warning(f"[COMMAND] Version not found: '{song_name}' version '{version}'")
            error_msg = f"❌ '{song_name}'"
            if version:
                error_msg += f" (version '{version}')"
            error_msg += " not found."
            await interaction.followup.send(error_msg, ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                False, 
                error="Version not found"
            )
            return

        try:
            logger.info(f"[COMMAND] Selected song: {best['path']}")
            url = await get_cached_url(bot, best['path'])
            if url == "FILE_TOO_LARGE":
                logger.warning(f"[COMMAND] File too large: {best['path']}")
                await interaction.followup.send(LARGE_FILE_MSG, ephemeral=True)
                await log_command_execution(
                    bot, 
                    interaction, 
                    command_data, 
                    False, 
                    error="File too large"
                )
                return
            if not url:
                logger.error(f"[COMMAND] Cache failed for: {best['path']}")
                await interaction.followup.send("❌ Failed to retrieve song.", ephemeral=True)
                await log_command_execution(
                    bot, 
                    interaction, 
                    command_data, 
                    False, 
                    error="Cache failed"
                )
                return

            await send_song_embed(interaction.user, best['metadata'], url, best['path'])
            await interaction.followup.send("✅ Check your DMs for the song link!", ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                True, 
                song_metadata=best['metadata'], 
                file_path=best['path']
            )
            logger.info("[COMMAND] Song sent successfully")
                                      
        except discord.Forbidden:
            logger.warning(f"[COMMAND] DM blocked for user: {interaction.user}")
            error_msg = "❌ I couldn't send you a DM. Please enable DMs from server members."
            await interaction.followup.send(error_msg, ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                False, 
                error="DM blocked"
            )
        except Exception as e:
            logger.exception(f"[COMMAND] Unexpected error: {str(e)}")
            error_msg = "❌ An unexpected error occurred."
            await interaction.followup.send(error_msg, ephemeral=True)
            await log_command_execution(
                bot, 
                interaction, 
                command_data, 
                False, 
                error=str(e)
            )

    @bot.tree.command(name="rebuild_index", description="[Admin] Rebuild song index")
    async def rebuild_index(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            logger.warning(f"[REBUILD] Unauthorized attempt by {interaction.user}")
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return
            
        logger.info(f"[REBUILD] Rebuilding index requested by {interaction.user}")
        await interaction.response.send_message("🔄 Rebuilding song index...", ephemeral=True)
        try:
            bot.song_index_ready.clear()
            bot.song_index = await build_song_index(bot)
            bot.song_index_ready.set()
            await interaction.followup.send("✅ Song index rebuilt successfully!", ephemeral=True)
            logger.info("[REBUILD] Index rebuilt successfully")
            
            # Log to bot-logs
            log_data = {
                'user': str(interaction.user),
                'user_id': interaction.user.id,
                'success': True,
                'action': 'rebuild_index'
            }
            await send_bot_log(bot, log_data)
            
        except Exception as e:
            logger.error(f"[REBUILD] Index rebuild failed: {str(e)}")
            error_msg = "❌ Failed to rebuild index."
            await interaction.followup.send(error_msg, ephemeral=True)
            
            # Log to bot-logs
            log_data = {
                'user': str(interaction.user),
                'user_id': interaction.user.id,
                'success': False,
                'error': str(e),
                'action': 'rebuild_index'
            }
            await send_bot_log(bot, log_data)

    logger.info("[SETUP] emarchive commands registered")