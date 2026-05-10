import os
import re
import json
import sqlite3
import atexit
import time
import io
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
from _utils import script_dir, _now
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
import asyncio
import aiohttp
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
script_dir().joinpath("temp").mkdir(parents=True, exist_ok=True)
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
    file_path       TEXT PRIMARY KEY,
    cdn_url         TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    file_checksum   TEXT NOT NULL DEFAULT '',
    transcoded      INTEGER NOT NULL DEFAULT 0,
    cached_at       TEXT NOT NULL,
    accessed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS song_cache_fails (
    file_path   TEXT NOT NULL,
    failed_at   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (file_path, failed_at)
);
"""

def _file_checksum(file_path: str) -> str:
    import hashlib
    h = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ''

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _db_init() -> None:
    with _db_conn() as c:
        c.executescript(DB_SCHEMA)
        try:
            c.execute("ALTER TABLE song_cache ADD COLUMN file_checksum TEXT NOT NULL DEFAULT ''")
            c.commit()
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE song_cache ADD COLUMN transcoded INTEGER NOT NULL DEFAULT 0")
            c.commit()
        except Exception:
            pass

def _cache_lookup(file_path: str) -> Optional[dict]:
    key = str(Path(file_path))
    with _db_conn() as c:
        c.execute("UPDATE song_cache SET accessed_at=? WHERE file_path=?",
                  (_now().isoformat(), key))
        c.commit()
        row = c.execute("SELECT * FROM song_cache WHERE file_path=?", (key,)).fetchone()
    return dict(row) if row else None

def _cache_store(file_path: str, cdn_url: str, message_id: str, channel_id: str,
                 file_name: str, file_size: int, checksum: str = '', transcoded: int = 0) -> None:
    key = str(Path(file_path))
    now = _now().isoformat()
    with _db_conn() as c:
        c.execute(
            "INSERT INTO song_cache "
            "(file_path, cdn_url, message_id, channel_id, file_name, file_size, file_checksum, transcoded, cached_at, accessed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_path) DO UPDATE SET "
            "cdn_url=excluded.cdn_url, message_id=excluded.message_id, channel_id=excluded.channel_id, "
            "file_name=excluded.file_name, file_size=excluded.file_size, "
            "file_checksum=CASE WHEN excluded.file_checksum != '' THEN excluded.file_checksum ELSE file_checksum END, "
            "transcoded=excluded.transcoded, accessed_at=excluded.accessed_at",
            (key, cdn_url, str(message_id), str(channel_id), file_name, file_size, checksum, transcoded, now, now)
        )
        c.commit()

async def _cache_refresh_url(bot, file_path: str, entry: dict) -> Optional[str]:
    try:
        chan = bot.get_channel(int(entry["channel_id"]))
        if not chan:
            return None
        msg = await chan.fetch_message(int(entry["message_id"]))
        for att in msg.attachments:
            if att.filename == entry["file_name"]:
                _cache_store(file_path, att.url, entry["message_id"], entry["channel_id"],
                             entry["file_name"], entry["file_size"])
                return att.url
    except Exception:
        pass
    return None

def _cache_fail(file_path: str, reason: str) -> None:
    with _db_conn() as c:
        c.execute(
            "INSERT INTO song_cache_fails (file_path, failed_at, reason) VALUES (?,?,?)",
            (str(Path(file_path)), _now().isoformat(), reason)
        )
        c.commit()

async def _get_or_upload_cache(bot, file_path: str) -> Optional[str]:
    p = Path(file_path)
    cached = _cache_lookup(file_path)
    if cached:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.head(cached["cdn_url"], timeout=5) as resp:
                    if resp.status == 200:
                        bot.logger.log(MODULE_NAME, f"Cache hit: {p.name}")
                        return cached["cdn_url"]
        except Exception:
            pass
        fresh = await _cache_refresh_url(bot, file_path, cached)
        if fresh:
            bot.logger.log(MODULE_NAME, f"Cache refreshed: {p.name}")
            return fresh
        bot.logger.log(MODULE_NAME, f"Cache stale, re-uploading: {p.name}", "WARNING")

    chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
    if not chan:
        bot.logger.log(MODULE_NAME, f"Missing channel {CACHE_CHANNEL_NAME}", "WARNING")
        return None
    if not p.exists():
        bot.logger.error(MODULE_NAME, f"File not found: {file_path}")
        return None
    size = p.stat().st_size
    max_bytes = getattr(getattr(chan, 'guild', None), 'filesize_limit', 25 * 1024 * 1024)
    if size > max_bytes:
        bot.logger.log(MODULE_NAME, f"File too large: {p.name} ({size // 1024 // 1024}MB)", "WARNING")
        return "FILE_TOO_LARGE"

    try:
        bot.logger.log(MODULE_NAME, f"Uploading {p.name} to {CACHE_CHANNEL_NAME}")
        mf = discord.File(p, filename=p.name)
        msg = await asyncio.wait_for(chan.send(file=mf), timeout=120)
        url = msg.attachments[0].url
        _cache_store(file_path, url, msg.id, chan.id, p.name,
                     p.stat().st_size if p.exists() else 0)
        bot.logger.log(MODULE_NAME, f"Cached: {p.name}")
        return url
    except asyncio.TimeoutError:
        bot.logger.log(MODULE_NAME, f"Upload timed out: {p.name}", "WARNING")
        return None
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

    entry = _cache_lookup(str(p))
    transcoded_note = "\n-# Served transcoded — source exceeds Discord's upload limit" if entry and entry.get("transcoded") else ""
    msg = f"[{p.name}]({url}){transcoded_note}"

    if interaction.guild is None:
        await interaction.followup.send(msg)
    else:
        await interaction.followup.send(msg, ephemeral=True)
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


def _downsample_flac(source_path: str):
    import subprocess
    p = Path(source_path)
    tmp_path = script_dir() / 'temp' / p.name

    def _probe_sample_rate():
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', source_path],
                capture_output=True, timeout=30)
            import json as _json
            return int(_json.loads(r.stdout)['streams'][0]['sample_rate'])
        except Exception:
            return 48000

    def _run(args):
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', source_path,
             '-c:a', 'flac', '-compression_level', '8', '-map_metadata', '0']
            + args + [str(tmp_path)],
            capture_output=True, timeout=180)
        if result.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            return None, None
        return str(tmp_path), tmp_path.stat().st_size

    try:
        src_rate = _probe_sample_rate()
        rate_args = ['-af', 'aresample=resampler=soxr:precision=28', '-ar', '48000'] if src_rate > 48000 else []

        # first pass: resample if needed, keep bit depth
        path, sz = _run(rate_args)
        if path and sz:
            return path, sz, True  # resampled — still flag as transcoded

        # second pass: also reduce to 16-bit
        path, sz = _run(rate_args + ['-sample_fmt', 's16'])
        if path and sz:
            return path, sz, True

        return None, None, False
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None, None, False

class ARCHIVEManager:
    def __init__(self, bot):
        self.bot = bot
        self.song_index = None
        self.song_index_ready = asyncio.Event()
        self.initialization_task = None
        self._status_msg_id = None

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
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(METADATA_EXECUTOR, self._migrate_checksums)
            asyncio.create_task(self.backfill_cache())
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Background initialization failed", e)

    def _migrate_checksums(self):
        with _db_conn() as c:
            rows = c.execute(
                "SELECT file_path FROM song_cache WHERE file_checksum = ''"
            ).fetchall()
        if not rows:
            return
        self.bot.logger.log(MODULE_NAME, f"Migrating checksums for {len(rows)} cached files...")
        updated = 0
        for row in rows:
            fp = row["file_path"]
            checksum = _file_checksum(fp)
            if checksum:
                with _db_conn() as c:
                    c.execute("UPDATE song_cache SET file_checksum=? WHERE file_path=?", (checksum, fp))
                    c.commit()
                updated += 1
        self.bot.logger.log(MODULE_NAME, f"Checksum migration complete: {updated}/{len(rows)} files")

    async def ensure_ready(self):
        if not self.song_index_ready.is_set() and self.initialization_task:
            try:
                await asyncio.wait_for(self.song_index_ready.wait(), timeout=30)
            except asyncio.TimeoutError:
                self.bot.logger.log(MODULE_NAME, "Song index init timed out", "WARNING")

    async def backfill_cache(self):
        if not self.song_index:
            self.bot.logger.log(MODULE_NAME, "No song index to backfill from", "WARNING")
            return
        chan = discord.utils.get(self.bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
        if not chan:
            self.bot.logger.log(MODULE_NAME, f"Cannot backfill — no #{CACHE_CHANNEL_NAME} channel", "WARNING")
            return
        max_bytes = min(getattr(chan.guild, 'filesize_limit', 25 * 1024 * 1024), 95 * 1024 * 1024)

        try:
            async for msg in chan.history(limit=100):
                if msg.author != self.bot.user:
                    await msg.delete()
                    continue
                if msg.embeds:
                    for e in msg.embeds:
                        if e.title and "Cache Backfill" in e.title:
                            await msg.delete()
                            break
        except Exception:
            pass

        seen = set()
        for fmt in FORMATS:
            for entries in self.song_index.get(fmt, {}).values():
                for e in entries:
                    fp = e['path']
                    if fp not in seen:
                        seen.add(fp)
        total = len(seen)

        self.bot.logger.log(MODULE_NAME, f"Scanning {total} files against cache DB...")
        loop = asyncio.get_running_loop()
        pending, cached, _ = await loop.run_in_executor(
            METADATA_EXECUTOR, self._scan_pending, list(seen))

        self.bot.logger.log(MODULE_NAME,
            f"Cache backfill: {len(pending)}/{total} files need upload "
            f"(max {max_bytes // 1024 // 1024}MB per message)")

        if not pending:
            await chan.send(embed=discord.Embed(
                title="📦 Cache Backfill",
                description="All files already cached. Nothing to do.",
                color=0x2ecc71))
            return

        uploaded = 0
        errors = 0
        uploaded_bytes = 0
        total_bytes = 0
        start = time.time()

        batch = []
        batch_size = 0
        sent_batches = 0

        for fp in pending:
            if _cache_lookup(fp):
                cached += 1
                continue
            p = Path(fp)
            try:
                sz = await loop.run_in_executor(METADATA_EXECUTOR, lambda: p.stat().st_size)
            except (OSError, asyncio.TimeoutError):
                self.bot.logger.log(MODULE_NAME, f"Skipping {p.name} — stat timed out", "WARNING")
                continue
            if sz > max_bytes:
                await self._update_backfill_embed(
                    chan, start, total, cached, uploaded, errors,
                    uploaded_bytes, total_bytes, f"Downsampling {p.name}...")
                ds_path, ds_sz, transcoded = await loop.run_in_executor(
                    METADATA_EXECUTOR, _downsample_flac, fp)
                if ds_path and ds_sz and ds_sz <= max_bytes:
                    ok = await self._send_batch(chan, [(fp, Path(ds_path), ds_sz)], source_path=fp, transcoded=transcoded)
                    try:
                        Path(ds_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                    if ok:
                        uploaded += 1
                        uploaded_bytes += ds_sz
                    else:
                        errors += 1
                else:
                    if ds_path:
                        try:
                            Path(ds_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                    self.bot.logger.log(MODULE_NAME, f"Downsample failed or still too large: {p.name}", "WARNING")
                    errors += 1
                sent_batches += 1
                await asyncio.sleep(2)
                continue
            if batch and batch_size + sz > max_bytes:
                await self._update_backfill_embed(
                    chan, start, total, cached, uploaded, errors,
                    uploaded_bytes, total_bytes, f"Uploading batch ({len(batch)} files)...")
                ok = await self._send_batch(chan, batch)
                if ok:
                    uploaded += len(batch)
                    uploaded_bytes += sum(s for _, _, s in batch)
                else:
                    uploaded, uploaded_bytes, errors = await self._fallback_batch(
                        chan, batch, uploaded, uploaded_bytes, errors)
                sent_batches += 1
                batch = []
                batch_size = 0
                await asyncio.sleep(2)
            batch.append((fp, p, sz))
            batch_size += sz
            total_bytes += sz

        if batch:
            batch = [(fp, p, sz) for fp, p, sz in batch if not _cache_lookup(fp)]
            if batch:
                await self._update_backfill_embed(
                    chan, start, total, cached, uploaded, errors,
                    uploaded_bytes, total_bytes, f"Uploading batch ({len(batch)} files)...")
                ok = await self._send_batch(chan, batch)
                if ok:
                    uploaded += len(batch)
                    uploaded_bytes += sum(s for _, _, s in batch)
                else:
                    uploaded, uploaded_bytes, errors = await self._fallback_batch(
                        chan, batch, uploaded, uploaded_bytes, errors)
                sent_batches += 1

        await self._update_backfill_embed(
            chan, start, total, cached, uploaded, errors,
            uploaded_bytes, total_bytes, None)
        self.bot.logger.log(MODULE_NAME,
            f"Cache backfill complete: {uploaded} uploaded in {sent_batches} batch(es), "
            f"{cached} cached, {errors} errors")

    def _scan_pending(self, seen):
        pending = []
        cached = 0
        for fp in sorted(seen):
            try:
                if _cache_lookup(fp):
                    cached += 1
                else:
                    pending.append(fp)
            except Exception:
                pass
        return pending, cached, len(seen) - len(pending) - cached

    async def _fallback_batch(self, chan, batch, uploaded, uploaded_bytes, errors):
        for fp, p, sz in batch:
            ok = await self._send_batch(chan, [(fp, p, sz)])
            if ok:
                uploaded += 1
                uploaded_bytes += sz
            else:
                _cache_fail(fp, "individual upload failed")
                errors += 1
            await asyncio.sleep(1)
        return uploaded, uploaded_bytes, errors

    async def _update_backfill_embed(self, chan, start, total, cached, uploaded, errors, uploaded_bytes, total_bytes, current_name):
        elapsed = time.time() - start
        done = uploaded + cached
        pct = done / total * 100 if total else 0

        bar_len = 12
        filled = int(bar_len * done / total) if total else 0
        bar = "▓" * filled + "░" * (bar_len - filled)

        if elapsed > 0 and uploaded > 0:
            files_per_sec = uploaded / elapsed
            remaining = total - done
            eta_secs = remaining / files_per_sec if files_per_sec > 0 else 0
            speed = f"{uploaded_bytes / 1024 / 1024 / elapsed:.1f} MB/s"
        else:
            eta_secs = 0
            speed = "—"

        embed = discord.Embed(
            title="📦 Cache Backfill",
            description=f"`{bar}` **{pct:.1f}%** ({done}/{total})",
            color=0x3498db if current_name else 0x2ecc71,
        )
        embed.add_field(name="Uploaded", value=f"{uploaded} files\n{uploaded_bytes / 1024 / 1024:.0f} MB", inline=True)
        embed.add_field(name="Speed / ETA",
                        value=f"{speed}\n{'—' if not eta_secs else f'{eta_secs / 60:.0f}m {eta_secs % 60:.0f}s'}",
                        inline=True)
        embed.add_field(name="Status", value=f"✅ {cached} cached\n❌ {errors} errors", inline=True)
        if current_name:
            embed.set_footer(text=f"Current: {current_name[:120]}")
        else:
            embed.set_footer(text="Complete!")

        if self._status_msg_id:
            try:
                msg = await chan.fetch_message(self._status_msg_id)
                await msg.delete()
            except Exception:
                pass
        msg = await chan.send(embed=embed)
        self._status_msg_id = msg.id

    async def _send_batch(self, chan, batch, source_path=None, transcoded=False) -> bool:
        read_start = time.time()
        loop = asyncio.get_running_loop()
        file_data = []
        for fp, p, sz in batch:
            data = await loop.run_in_executor(METADATA_EXECUTOR, p.read_bytes)
            cache_key = source_path if source_path and len(batch) == 1 else fp
            file_data.append((cache_key, p.name, data, sz))
        read_elapsed = time.time() - read_start
        files = [discord.File(io.BytesIO(d), filename=n) for _, n, d, _ in file_data]
        total_mb = sum(sz for _, _, _, sz in file_data) // 1024 // 1024
        self.bot.logger.log(MODULE_NAME,
            f"Uploading batch: {len(batch)} file(s), {total_mb}MB "
            f"(read: {read_elapsed:.1f}s)")
        try:
            timeout = 120 + 30 * len(batch)
            up_start = time.time()
            msg = await asyncio.wait_for(chan.send(files=files), timeout=timeout)
            up_elapsed = time.time() - up_start
        except asyncio.TimeoutError:
            self.bot.logger.log(MODULE_NAME, f"Batch upload timed out ({len(batch)} files)", "WARNING")
            return False
        except discord.HTTPException as e:
            self.bot.logger.log(MODULE_NAME, f"Batch upload failed ({len(batch)} files): {e}", "WARNING")
            return False
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Batch upload error ({len(batch)} files): {e}", "WARNING")
            return False
        ok = True
        for (fp, name, data, sz), att in zip(file_data, msg.attachments):
            actual_path = source_path if source_path and len(batch) == 1 else fp
            checksum = _file_checksum(actual_path)
            _cache_store(fp, att.url, att.id, chan.id, name, sz, checksum, int(transcoded))
        self.bot.logger.log(MODULE_NAME,
            f"Cached batch: {len(batch)} file(s), {total_mb}MB "
            f"(read: {read_elapsed:.1f}s, upload: {up_elapsed:.1f}s)")
        if len(msg.attachments) != len(batch):
            self.bot.logger.log(MODULE_NAME,
                f"Batch mismatch: sent {len(batch)} files, got {len(msg.attachments)} attachments", "WARNING")
            ok = False
        return ok

def setup(bot):
    from mod_core import is_owner

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
        if not _cache_lookup(best['path']):
            await interaction.followup.send("Uploading to cache (first time, may be slow)...", ephemeral=True)
        await _deliver_song(bot, interaction, best)
        await _log_delivery(bot, interaction.user, best, source="slash command")

    @bot.tree.command(name="rebuild_index", description="[Owner only] Rebuild the song index cache")
    async def rebuild_index(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        await interaction.response.send_message("Rebuilding song index...", ephemeral=True)
        try:
            ARCHIVE_manager.song_index_ready.clear()
            ARCHIVE_manager.song_index = await build_song_index(bot)
            ARCHIVE_manager.song_index_ready.set()
            await interaction.followup.send("Song index rebuilt successfully! Starting cache backfill...", ephemeral=True)
            asyncio.create_task(ARCHIVE_manager.backfill_cache())
            bot.logger.log(MODULE_NAME, "Index rebuilt successfully, backfill started")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Index rebuild failed", e)
            await interaction.followup.send("Failed to rebuild index.", ephemeral=True)

    @bot.tree.command(name="cache_backfill",
                      description="[Owner only] Pre-upload all uncached songs to the CDN cache")
    async def cache_backfill(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Starting cache backfill in the background. Check the bot logs for progress.", ephemeral=True)
        asyncio.create_task(ARCHIVE_manager.backfill_cache())

    bot.logger.log(MODULE_NAME, "ARCHIVE module setup complete")
