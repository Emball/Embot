import os
import re
import json
import sqlite3
import atexit
from pathlib import Path
from typing import Optional
from _utils import script_dir, _now
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
import asyncio
import difflib
from discord import app_commands
import discord

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

METADATA_EXECUTOR = ThreadPoolExecutor(max_workers=4)
atexit.register(METADATA_EXECUTOR.shutdown, wait=False)
MODULE_NAME = "MUSIC ARCHIVE"

def _migrate_path(new_path: Path, old_path: Path) -> Path:
    if new_path.exists():
        return new_path
    if old_path.exists():
        old_path.rename(new_path)
        print(f"[MUSICARCHIVE] Migrated {old_path.name} → {new_path.name}")
    return new_path

def _load_eminem_root() -> Path:
    env_val = os.environ.get("EMINEM_ROOT")
    if env_val:
        return Path(env_val)
    from _utils import migrate_config
    config_dir = script_dir() / "config"
    _migrate_path(config_dir / "music.json", config_dir / "music_archive.json")
    _migrate_path(config_dir / "music.json", config_dir / "musicarchive_config.json")
    _migrate_path(config_dir / "music.json", config_dir / "archive_config.json")
    data = migrate_config(config_dir / "music.json", {"eminem_root": "."})
    if data.get("eminem_root"):
        return Path(data["eminem_root"])
    raise FileNotFoundError(
        "EMINEM_ROOT is not configured. Set the EMINEM_ROOT environment variable "
        "or edit config/music.json and set 'eminem_root' to your Eminem music folder."
    )

try:
    EMINEM_ROOT = _load_eminem_root()
except FileNotFoundError as _e:
    import sys as _sys
    print(f"[ARCHIVE] WARNING: {_e}", file=_sys.stderr)
    EMINEM_ROOT = Path(".")

FORMATS = ["FLAC", "MP3"]
CACHE_CHANNEL_NAME = "songcache"
DB_PATH = str(_migrate_path(
    script_dir() / "db" / "musicarchive.db",
    script_dir() / "db" / "archive.db",
))
script_dir().joinpath("db").mkdir(parents=True, exist_ok=True)
VERSION_KEYWORDS = ['live', 'remix', 'demo', 'acoustic', 'version', 'edit', 'radio']
SPECIAL_FOLDERS = {
    "8 - Features": "Feature",
    "7 - Singles": "Single",
    "10 - Freestyles (MP3 Only)": "Freestyle",
    "11 - Leaks (Mostly MP3)": "Leak"
}
LARGE_FILE_MSG = "Sorry! The song file was too big to upload."
MAX_SEARCH_RESULTS = 5

async def extract_metadata_async(file_path):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(METADATA_EXECUTOR, extract_metadata_sync, file_path)
    except Exception as e:
        print(f"Async metadata error for {file_path}: {e}")
        return None

def extract_metadata_sync(file_path):
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

def handle_special_folder(file_path, metadata, folder_name):
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
    t = re.sub(r'^(\d+\s*-\s*)?\d+\s+', '', title)
    t = re.sub(r'[({\[].*?[)}\]](?=\s*$)', '', t)
    t = re.sub(r'\b(?:feat\.?|ft\.?|with)\s+.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', '', t).strip()
    return t.casefold()

async def build_song_index(bot):
    bot.logger.log(MODULE_NAME, "Building song index...")
    with _db_conn() as c:
        c.execute("DELETE FROM song_index")
        c.commit()
    total = 0
    for fmt in FORMATS:
        fmt_path = EMINEM_ROOT / fmt
        if not fmt_path.exists():
            bot.logger.log(MODULE_NAME, f"Format directory missing: {fmt_path}", "WARNING")
            continue
        bot.logger.log(MODULE_NAME, f"Scanning {fmt} directory...")
        all_files = []
        for root, _, files in os.walk(fmt_path):
            root_path = Path(root)
            folder = root_path.name
            try:
                category = root_path.relative_to(fmt_path).parts[0]
            except (ValueError, IndexError):
                category = folder
            for fn in files:
                if not fn.lower().endswith(('.flac', '.mp3')):
                    continue
                full_path = root_path / fn
                all_files.append((full_path, folder, category))
        batch_size = 50
        for i in range(0, len(all_files), batch_size):
            batch = all_files[i:i + batch_size]
            batch_tasks = [
                process_single_file(bot, full_path, folder, category, fmt)
                for full_path, folder, category in batch
            ]
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            total += len([r for r in results if r is not None])
            if i + batch_size < len(all_files):
                await asyncio.sleep(0.1)
            bot.logger.log(MODULE_NAME,
                f"Processed {min(i + batch_size, len(all_files))}/{len(all_files)} files...")
    bot.logger.log(MODULE_NAME, f"Indexed {total} songs total")
    return _load_song_index_from_db()

async def process_single_file(bot, full_path, folder, category, fmt):
    try:
        md = await extract_metadata_async(str(full_path))
        if any(k in folder for k in SPECIAL_FOLDERS):
            md = handle_special_folder(str(full_path), md, folder)
        if not md:
            md = {'title': full_path.stem, 'album': folder, 'artist': 'Eminem', 'year': ''}
        key = normalize_title(md['title'])
        with _db_conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO song_index "
                "(format, normalized_key, file_path, original_title, folder, category, metadata_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (fmt, key, str(full_path), full_path.stem, folder, category, json.dumps(md))
            )
            c.commit()
        return True
    except Exception as e:
        bot.logger.error(MODULE_NAME, f"Error processing {full_path}", e)
        return None

def _load_song_index_from_db() -> dict:
    from collections import defaultdict
    idx = {fmt: defaultdict(list) for fmt in FORMATS}
    with _db_conn() as c:
        rows = c.execute("SELECT * FROM song_index").fetchall()
    for r in rows:
        idx[r["format"]][r["normalized_key"]].append({
            'path': r["file_path"],
            'original_title': r["original_title"],
            'folder': r["folder"],
            'category': r["category"],
            'metadata': json.loads(r["metadata_json"]),
        })
    return idx

def load_song_index(bot):
    try:
        with _db_conn() as c:
            count = c.execute("SELECT COUNT(*) FROM song_index").fetchone()[0]
        if count == 0:
            bot.logger.log(MODULE_NAME, "Song index empty — needs building", "WARNING")
            return None
        bot.logger.log(MODULE_NAME, f"Loaded song index ({count} entries)")
        return _load_song_index_from_db()
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Error loading index", e)
    return None

def find_best_match(idx, fmt, query):
    key = normalize_title(query)
    matches = difflib.get_close_matches(key, idx.get(fmt, {}).keys(), n=MAX_SEARCH_RESULTS, cutoff=0.5)
    return matches[0] if matches else None

def select_best_candidate(cands, version=None):
    if version:
        vl = version.lower()
        filtered = [c for c in cands if vl in c['original_title'].lower() or vl in c['folder'].lower()]
        if not filtered:
            return None
        cands = filtered
    scored = []
    for c in cands:
        p = sum(1 for kw in VERSION_KEYWORDS
                if kw in c['original_title'].lower() or kw in c['folder'].lower())
        y = 9999
        try:
            yv = c['metadata'].get('year', '')
            y = int(yv[:4]) if yv and yv[:4].isdigit() else 9999
        except Exception:
            pass
        scored.append((p, y, c))
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2] if scored else None

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS song_index (
    format          TEXT NOT NULL,
    normalized_key  TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    original_title  TEXT NOT NULL,
    folder          TEXT NOT NULL,
    category        TEXT NOT NULL,
    metadata_json   TEXT NOT NULL,
    PRIMARY KEY (format, normalized_key, file_path)
);
CREATE INDEX IF NOT EXISTS idx_song_format_key ON song_index(format, normalized_key);

CREATE TABLE IF NOT EXISTS song_cache (
    file_path   TEXT PRIMARY KEY,
    cdn_url     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    file_size   INTEGER NOT NULL DEFAULT 0,
    cached_at   TEXT NOT NULL,
    accessed_at TEXT NOT NULL
);
"""

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _db_init() -> None:
    with _db_conn() as c:
        c.executescript(DB_SCHEMA)
        c.commit()

def _cache_lookup(file_path: str) -> Optional[dict]:
    key = str(Path(file_path).resolve())
    with _db_conn() as c:
        c.execute("UPDATE song_cache SET accessed_at=? WHERE file_path=?",
                  (_now().isoformat(), key))
        c.commit()
        row = c.execute("SELECT * FROM song_cache WHERE file_path=?", (key,)).fetchone()
    return dict(row) if row else None

def _cache_store(file_path: str, cdn_url: str, message_id: str, channel_id: str,
                 file_name: str, file_size: int) -> None:
    key = str(Path(file_path).resolve())
    now = _now().isoformat()
    with _db_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO song_cache "
            "(file_path, cdn_url, message_id, channel_id, file_name, file_size, cached_at, accessed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, cdn_url, str(message_id), str(channel_id), file_name, file_size, now, now)
        )
        c.commit()

async def _get_or_upload_cache(bot, file_path: str) -> Optional[str]:
    p = Path(file_path)
    cached = _cache_lookup(file_path)
    if cached:
        bot.logger.log(MODULE_NAME, f"Cache hit: {p.name}")
        return cached["cdn_url"]

    chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
    if not chan:
        bot.logger.log(MODULE_NAME, f"Missing channel {CACHE_CHANNEL_NAME}", "WARNING")
        return None
    if not p.exists():
        bot.logger.error(MODULE_NAME, f"File not found: {file_path}")
        return None

    try:
        bot.logger.log(MODULE_NAME, f"Uploading {p.name} to {CACHE_CHANNEL_NAME}")
        mf = discord.File(p, filename=p.name)
        msg = await chan.send(file=mf)
        url = msg.attachments[0].url
        _cache_store(file_path, url, msg.id, chan.id, p.name,
                     p.stat().st_size if p.exists() else 0)
        bot.logger.log(MODULE_NAME, f"Cached: {p.name}")
        return url
    except discord.HTTPException as e:
        if e.status == 413:
            bot.logger.log(MODULE_NAME, f"File too large: {p.name}", "WARNING")
            return "FILE_TOO_LARGE"
        bot.logger.error(MODULE_NAME, f"Upload failed", e)
        return None
    except Exception as e:
        bot.logger.error(MODULE_NAME, f"Upload error", e)
        return None

async def _deliver_song(bot, interaction: discord.Interaction, candidate: dict) -> None:
    p = Path(candidate['path'])
    url = await _get_or_upload_cache(bot, str(p))
    if url == "FILE_TOO_LARGE":
        await interaction.followup.send(LARGE_FILE_MSG, ephemeral=True)
        return
    if not url:
        await interaction.followup.send("Failed to retrieve song.", ephemeral=True)
        return

    if interaction.guild is None:
        await interaction.followup.send(f"[{p.name}]({url})")
    else:
        await interaction.followup.send(f"[{p.name}]({url})", ephemeral=True)
    bot.logger.log(MODULE_NAME, f"Delivered '{p.name}'")

def _is_fed(interaction: discord.Interaction) -> bool:
    try:
        from mod_suspicion import is_flagged as _check
        if interaction.guild:
            return _check(str(interaction.guild.id), str(interaction.user.id))
    except Exception:
        pass
    return False

async def _log_delivery(bot, user, candidate: dict, source: str = "command"):
    try:
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            return
        md = candidate['metadata']
        embed = discord.Embed(title="Archive Delivery", color=0x3498db, timestamp=_now())
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Source", value=source, inline=True)
        embed.add_field(name="Title", value=md.get('title', 'Unknown'), inline=True)
        embed.add_field(name="Format", value=Path(candidate['path']).suffix.upper(), inline=True)
        embed.add_field(name="File", value=f"`{Path(candidate['path']).name}`", inline=False)
        await channel.send(embed=embed)
    except Exception:
        pass

async def send_bot_log(bot, log_data):
    try:
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            return
        embed = discord.Embed(
            title="Command Execution Log",
            color=0x3498db if log_data.get('success') else 0xe74c3c,
            timestamp=_now(),
        )
        embed.add_field(name="User", value=f"{log_data['user']} ({log_data['user_id']})", inline=False)
        if 'action' in log_data:
            embed.add_field(name="Command", value=log_data['action'], inline=False)
        if 'params' in log_data:
            params = "\n".join([f"- {k}: {v}"for k, v in log_data['params'].items()])
            embed.add_field(name="Parameters", value=params, inline=False)
        embed.add_field(name="Status", value="SUCCESS"if log_data['success'] else "FAILURE", inline=True)
        if not log_data['success'] and log_data.get('error'):
            embed.add_field(name="Error", value=f"```{log_data['error']}```", inline=False)
        await channel.send(embed=embed)
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to send log", e)

class ARCHIVEManager:
    def __init__(self, bot):
        self.bot = bot
        self.song_index = None
        self.song_index_ready = asyncio.Event()
        self.initialization_task = None

    async def initialize(self):
        self.bot.logger.log(MODULE_NAME, "Starting song index initialization...")
        _db_init()
        self.initialization_task = asyncio.create_task(self._initialize_background())

    async def _initialize_background(self):
        try:
            self.song_index = load_song_index(self.bot)
            if not self.song_index:
                self.bot.logger.log(MODULE_NAME, "Building new song index in background...")
                self.song_index = await build_song_index(self.bot)
            self.song_index_ready.set()
            self.bot.logger.log(MODULE_NAME, "Song index ready")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Background initialization failed", e)

    async def ensure_ready(self):
        if not self.song_index_ready.is_set() and self.initialization_task:
            await self.song_index_ready.wait()

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up ARCHIVE module")

    ARCHIVE_manager = ARCHIVEManager(bot)
    bot.ARCHIVE_manager = ARCHIVE_manager

    asyncio.create_task(ARCHIVE_manager.initialize())

    @bot.tree.command(name="archive", description="Get a song from Eminem's archive")
    @app_commands.describe(
        format="File format",
        song_name="Name of the song",
        version="Specific version (optional)",
    )
    @app_commands.choices(format=[app_commands.Choice(name=fmt, value=fmt) for fmt in FORMATS])
    async def ARCHIVE(interaction: discord.Interaction, format: str, song_name: str,
                      version: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ARCHIVE_manager.ensure_ready()
        if not ARCHIVE_manager.song_index_ready.is_set():
            await interaction.followup.send(
                "Initializing — please try again shortly.", ephemeral=True)
            return
        if _is_fed(interaction):
            await interaction.followup.send("Failed to retrieve song.", ephemeral=True)
            return
        key = find_best_match(ARCHIVE_manager.song_index, format, song_name)
        if not key:
            bot.logger.log(MODULE_NAME, f"Song not found: '{song_name}' in {format}", "WARNING")
            await interaction.followup.send(f"'{song_name}' not found in {format}.", ephemeral=True)
            await send_bot_log(bot, {
                'user': str(interaction.user), 'user_id': interaction.user.id,
                'success': False, 'error': 'Song not found', 'action': 'ARCHIVE',
                'params': {'format': format, 'song_name': song_name, 'version': version or 'N/A'},
            })
            return
        candidates = ARCHIVE_manager.song_index[format][key]
        best = select_best_candidate(candidates, version)
        if not best:
            msg = f"'{song_name}'"
            if version:
                msg += f"(version '{version}')"
            await interaction.followup.send(f"{msg} not found.", ephemeral=True)
            return
        bot.logger.log(MODULE_NAME, f"Delivering: {best['original_title']}")
        await _deliver_song(bot, interaction, best)
        await _log_delivery(bot, interaction.user, best, source="slash command")

    @bot.tree.command(name="rebuild_index", description="[Owner only] Rebuild the song index cache")
    async def rebuild_index(interaction: discord.Interaction):
        from mod_core import is_owner
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        await interaction.response.send_message("Rebuilding song index...", ephemeral=True)
        try:
            ARCHIVE_manager.song_index_ready.clear()
            ARCHIVE_manager.song_index = await build_song_index(bot)
            ARCHIVE_manager.song_index_ready.set()
            await interaction.followup.send("Song index rebuilt successfully!", ephemeral=True)
            bot.logger.log(MODULE_NAME, "Index rebuilt successfully")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Index rebuild failed", e)
            await interaction.followup.send("Failed to rebuild index.", ephemeral=True)

    bot.logger.log(MODULE_NAME, "ARCHIVE module setup complete")
