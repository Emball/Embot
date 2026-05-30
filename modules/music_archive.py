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
import unicodedata
import difflib
from discord import app_commands
import discord

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

METADATA_EXECUTOR = ThreadPoolExecutor(max_workers=4)
UPLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=1)  # backfill file reads — isolated from metadata ops
atexit.register(METADATA_EXECUTOR.shutdown, wait=False)
atexit.register(UPLOAD_EXECUTOR.shutdown, wait=False)
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
    t = title.replace('_', ' ')
    t = unicodedata.normalize('NFD', t)
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    t = re.sub(r'^\(?\d{4}\)?\s*', '', t)
    t = re.sub(r'^(\d+\s*-\s*)?\d+\s+', '', t)
    t = re.sub(r'\b(?:feat\.?[,]?|ft\.?|with)\s+.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
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
    file_path   TEXT PRIMARY KEY,
    failed_at   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    fail_count  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cache_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
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
        for migration in [
            "ALTER TABLE song_cache ADD COLUMN file_checksum TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE song_cache ADD COLUMN transcoded INTEGER NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_song_cache_message_id ON song_cache(message_id)",
            "CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        ]:
            try:
                c.execute(migration)
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
            bot.logger.log(MODULE_NAME, f"Cache refresh failed: channel {entry['channel_id']} not found", "WARNING")
            return None
        msg = await chan.fetch_message(int(entry["message_id"]))
        if not msg.attachments:
            bot.logger.log(MODULE_NAME, f"Cache refresh failed: message {entry['message_id']} has no attachments", "WARNING")
            return None
        # Match by filename — message may contain multiple files from a batch
        att = next(
            (a for a in msg.attachments if normalize_title(Path(a.filename).stem) == normalize_title(Path(entry["file_name"]).stem)),
            msg.attachments[0]
        )
        _cache_store(file_path, att.url, entry["message_id"], entry["channel_id"],
                     entry["file_name"], entry["file_size"])
        return att.url
    except discord.NotFound:
        bot.logger.log(MODULE_NAME, f"Cache refresh failed: message {entry['message_id']} deleted", "WARNING")
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Cache refresh failed: {e}", "WARNING")
    return None

def _cache_fail(file_path: str, reason: str) -> None:
    with _db_conn() as c:
        c.execute(
            """INSERT INTO song_cache_fails (file_path, failed_at, reason, fail_count) VALUES (?,?,?,1)
               ON CONFLICT(file_path) DO UPDATE SET
                   failed_at=excluded.failed_at,
                   reason=excluded.reason,
                   fail_count=fail_count+1""",
            (str(Path(file_path)), _now().isoformat(), reason)
        )
        c.commit()

def _meta_get(key: str) -> Optional[str]:
    with _db_conn() as c:
        row = c.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None

def _meta_set(key: str, value: str) -> None:
    with _db_conn() as c:
        c.execute("INSERT INTO cache_meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        c.commit()

def _meta_del(key: str) -> None:
    with _db_conn() as c:
        c.execute("DELETE FROM cache_meta WHERE key=?", (key,))
        c.commit()

def _is_status_embed(msg, bot_user) -> bool:
    return msg.author == bot_user and not msg.attachments

async def _count_channel_files(chan, bot_user) -> int:
    """Count bot messages with audio attachments in the channel."""
    count = 0
    async for msg in chan.history(limit=None):
        if msg.author == bot_user and msg.attachments:
            count += len(msg.attachments)
    return count

async def _purge_stale_status(bot, chan) -> None:
    """Delete every bot status embed in the channel."""
    async for msg in chan.history(limit=200):
        if _is_status_embed(msg, bot.user):
            try:
                await msg.delete()
            except Exception:
                pass

async def _post_status(bot, chan, state: dict) -> None:
    """Delete any existing status embed(s) and repost at the bottom."""
    indexed = state.get("indexed", 0)
    cached = state.get("cached", 0)
    gap = indexed - cached
    errors = state.get("errors", [])
    reconcile_ts = state.get("last_reconcile_ts")
    orphans = state.get("orphans_deleted", 0)
    mbps = state.get("mbps")  # rolling avg MB/s upload speed
    remaining_mb = state.get("remaining_mb")  # MB not yet uploaded
    last_batch = state.get("last_batch")  # e.g. "3 files, 91MB in 25s"

    db_cached = state.get("db_cached", cached)  # what DB thinks is cached
    channel_cached = state.get("channel_cached")  # ground truth from channel

    pct = cached / indexed * 100 if indexed else 0
    bar_len = 14
    filled = int(bar_len * cached / indexed) if indexed else 0
    bar = "🟩" * filled + "⬛" * (bar_len - filled)

    accent = 0xe74c3c if errors else (0xf39c12 if cached < indexed else 0x57f287)

    body = f"{bar}  **{pct:.1f}%**\n{cached:,} / {indexed:,} songs cached"
    if gap:
        body += f"\n-# {gap:,} not yet uploaded"

    uploading = state.get("uploading")  # e.g. "3 files, 91MB" while batch in flight
    if uploading:
        body += f"\n-# 📤 uploading {uploading}..."
    if mbps and remaining_mb and remaining_mb > 0:
        eta_secs = int(remaining_mb / mbps)
        eta_ts = int(time.time()) + eta_secs
        body += f"\n-# ↑ {mbps:.1f} MB/s avg · Finishing <t:{eta_ts}:R>"
    if last_batch and not uploading:
        body += f"\n-# ✓ last: {last_batch}"

    # Three-way breakdown
    chan_str = f"`{channel_cached:,}`" if channel_cached is not None else "`—`"
    db_str = f"`{db_cached:,}`"
    mismatch = channel_cached is not None and channel_cached != db_cached
    sync_icon = "⚠" if mismatch else ("✓" if db_cached == indexed else "✗")
    body += f"\n\nIndexed `{indexed:,}` · DB {db_str} · Channel {chan_str}  {sync_icon}"
    if mismatch:
        body += f"\n-# DB/channel mismatch — {abs(db_cached - channel_cached)} file(s) differ"
    if orphans:
        body += f"  ·  {orphans} removed last sync"
    if errors:
        body += "\n" + "\n".join(f"-# ⚠ {e}" for e in errors[-3:])

    footer = f"synced <t:{reconcile_ts}:R>" if reconcile_ts else "not yet synced"

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"### Song Cache\n{body}"),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(f"-# {footer}"),
        accent_color=accent,
    ))

    old_id = _meta_get("status_msg_id")
    bot.logger.log(MODULE_NAME, f"_post_status: purging old embed (id={old_id}), cached={cached}")
    await _purge_stale_status(bot, chan)
    _meta_del("status_msg_id")
    try:
        msg = await chan.send(view=view)
        _meta_set("status_msg_id", str(msg.id))
        bot.logger.log(MODULE_NAME, f"_post_status: reposted as {msg.id}")
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Failed to post status embed: {e}", "WARNING")

async def _delete_cache_message(bot, entry: dict) -> None:
    try:
        chan = bot.get_channel(int(entry["channel_id"]))
        if chan:
            msg = await chan.fetch_message(int(entry["message_id"]))
            await msg.delete()
    except Exception:
        pass

async def _get_or_upload_cache(bot, file_path: str) -> Optional[str]:
    p = Path(file_path)
    cached = _cache_lookup(file_path)
    if cached:
        fresh = await _cache_refresh_url(bot, file_path, cached)
        if fresh:
            bot.logger.log(MODULE_NAME, f"Cache hit: {p.name}")
            return fresh
        bot.logger.log(MODULE_NAME, f"Cache miss, re-uploading: {p.name}", "WARNING")

    chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
    if not chan:
        bot.logger.log(MODULE_NAME, f"Missing channel {CACHE_CHANNEL_NAME}", "WARNING")
        return None
    if not p.exists():
        bot.logger.error(MODULE_NAME, f"File not found: {file_path}")
        return None
    size = p.stat().st_size
    max_bytes = 95 * 1024 * 1024
    if size > max_bytes:
        bot.logger.log(MODULE_NAME, f"File too large: {p.name} ({size // 1024 // 1024}MB)", "WARNING")
        return "FILE_TOO_LARGE"

    if cached:
        await _delete_cache_message(bot, cached)

    try:
        bot.logger.log(MODULE_NAME, f"Uploading {p.name} to {CACHE_CHANNEL_NAME}")
        mf = discord.File(p, filename=p.name)
        send_task = asyncio.ensure_future(chan.send(file=mf))
        done, _ = await asyncio.wait({send_task}, timeout=120)
        if not done:
            try:
                connector = bot.http._HTTPClient__session.connector
                if connector and not connector.closed:
                    for proto in list(getattr(connector, '_acquired', set())):
                        try:
                            proto.abort()
                        except Exception:
                            pass
            except Exception:
                pass
            await asyncio.wait({send_task}, timeout=3)
            if not send_task.done():
                send_task.cancel()
            raise asyncio.TimeoutError()
        msg = send_task.result()
        url = msg.attachments[0].url
        _cache_store(file_path, url, msg.id, chan.id, p.name,
                     p.stat().st_size if p.exists() else 0)
        bot.logger.log(MODULE_NAME, f"Cached: {p.name}")
        mgr = getattr(bot, 'ARCHIVE_manager', None)
        if mgr:
            with _db_conn() as c:
                mgr._status_state["cached"] = c.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
            asyncio.create_task(_post_status(bot, chan, mgr._status_state))
        return url
    except asyncio.TimeoutError:
        bot.logger.log(MODULE_NAME, f"Upload timed out: {p.name}", "WARNING")
        mgr = getattr(bot, 'ARCHIVE_manager', None)
        if mgr:
            mgr._status_state["errors"].append(f"Upload timeout: {p.name}")
        return None
    except discord.HTTPException as e:
        if e.status == 413:
            bot.logger.log(MODULE_NAME, f"File too large: {p.name}", "WARNING")
            return "FILE_TOO_LARGE"
        bot.logger.error(MODULE_NAME, f"Upload failed", e)
        mgr = getattr(bot, 'ARCHIVE_manager', None)
        if mgr:
            mgr._status_state["errors"].append(f"Upload failed: {p.name} ({e})")
        return None
    except Exception as e:
        bot.logger.error(MODULE_NAME, f"Upload error", e)
        mgr = getattr(bot, 'ARCHIVE_manager', None)
        if mgr:
            mgr._status_state["errors"].append(f"Upload error: {p.name} ({e})")
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
        ts = int(_now().timestamp())
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Archive Delivery\n**User**\n{user} ({user.id})\n\n**Source**\n{source}\n\n**Title**\n{md.get('title', 'Unknown')}\n\n**Format**\n{Path(candidate['path']).suffix.upper()}\n\n**File**\n`{Path(candidate['path']).name}`"),
            accent_color=0x3498db
        ))
        view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        view.add_item(discord.ui.TextDisplay(f"-# <t:{ts}>"))
        await channel.send(view=view)
    except Exception:
        pass

async def send_bot_log(bot, log_data):
    try:
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            return
        ts = int(_now().timestamp())
        parts = [f"# Command Execution Log"]
        parts.append(f"**User**\n{log_data['user']} ({log_data['user_id']})")
        if 'action' in log_data:
            parts.append(f"**Command**\n{log_data['action']}")
        if 'params' in log_data:
            params = "\n".join([f"- {k}: {v}" for k, v in log_data['params'].items()])
            parts.append(f"**Parameters**\n{params}")
        parts.append(f"**Status**\n{'SUCCESS' if log_data['success'] else 'FAILURE'}")
        if not log_data['success'] and log_data.get('error'):
            parts.append(f"**Error**\n```{log_data['error']}```")
        color = 0x3498db if log_data.get('success') else 0xe74c3c
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n\n".join(parts)),
            accent_color=color
        ))
        view.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        view.add_item(discord.ui.TextDisplay(f"-# <t:{ts}>"))
        await channel.send(view=view)
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to send log", e)


def _probe_audio(source_path: str):
    import subprocess, json as _json
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', source_path],
            capture_output=True, timeout=30)
        s = _json.loads(r.stdout)['streams'][0]
        rate = int(s.get('sample_rate', 44100))
        bits = int(s.get('bits_per_raw_sample') or s.get('bits_per_sample') or 16)
        return rate, bits
    except Exception:
        return 44100, 16


def _downsample_flac(source_path: str, max_bytes: int = 95 * 1024 * 1024):
    import subprocess
    p = Path(source_path)
    tmp_path = script_dir() / 'temp' / p.name

    def _run(args):
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', source_path,
             '-c:a', 'flac', '-compression_level', '8', '-map_metadata', '0']
            + args + [str(tmp_path)],
            capture_output=True, timeout=300)
        if result.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            return None, None
        sz = tmp_path.stat().st_size
        if sz > max_bytes:
            tmp_path.unlink(missing_ok=True)
            return None, None
        return str(tmp_path), sz

    try:
        src_rate, src_bits = _probe_audio(source_path)

        # pass 1: resample if above 44.1kHz
        if src_rate > 44100:
            path, sz = _run(['-af', 'aresample=resampler=soxr:precision=28', '-ar', '44100'])
            if path and sz:
                return path, sz, True

        # pass 2: reduce to 16-bit if still (or already) 44.1kHz but 24-bit+
        if src_bits > 16:
            path, sz = _run(['-sample_fmt', 's16'])
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
        self.backfill_active = False
        self._backfill_task = None
        self._shutdown_flag = False
        self._status_state = {
            "indexed": 0, "cached": 0,
            "last_reconcile_ts": None, "orphans_deleted": 0,
            "errors": [],
        }

    async def initialize(self):
        self.bot.logger.log(MODULE_NAME, "Starting song index initialization...")
        _db_init()
        self.initialization_task = asyncio.create_task(self._initialize_background())

    async def _initialize_background(self):
        try:
            await self.bot.wait_until_ready()
            self.song_index = load_song_index(self.bot)
            if not self.song_index:
                self.bot.logger.log(MODULE_NAME, "Building new song index in background...")
                self.song_index = await build_song_index(self.bot)
            self.song_index_ready.set()
            self.bot.logger.log(MODULE_NAME, "Song index ready")

            # count indexed unique file paths
            indexed = len({c['path'] for fmt in FORMATS for v in self.song_index.get(fmt, {}).values() for c in v})
            with _db_conn() as c:
                cached = c.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
            chan = discord.utils.get(self.bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
            channel_cached = await _count_channel_files(chan, self.bot.user) if chan else None
            if channel_cached is not None:
                _meta_set("channel_cached", str(channel_cached))
            self._status_state.update({
                "indexed": indexed, "cached": cached,
                "db_cached": cached, "channel_cached": channel_cached,
            })

            # Clear transient backfill_active flag; check persistent enabled flag
            _meta_del("backfill_active")
            if _meta_get("backfill_enabled") == "1":
                self.bot.logger.log(MODULE_NAME, "backfill_enabled flag set — resuming backfill")
                asyncio.create_task(self.backfill_cache())

            # Post status embed on startup
            self.bot.logger.log(MODULE_NAME, f"Startup status post — chan={'found' if chan else 'NOT FOUND'} status_msg_id={_meta_get('status_msg_id')}")
            if chan:
                await _purge_stale_status(self.bot, chan)
                await _post_status(self.bot, chan, self._status_state)
                self.bot.logger.log(MODULE_NAME, f"Startup status post complete — new status_msg_id={_meta_get('status_msg_id')}")

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(METADATA_EXECUTOR, self._migrate_checksums)
            asyncio.create_task(self._reconcile_loop())
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

    async def reconcile_channel(self):
        chan = discord.utils.get(self.bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
        if not chan:
            return
        status_id = _meta_get("status_msg_id")
        deleted = 0
        try:
            async for msg in chan.history(limit=None):
                if str(msg.id) == status_id:
                    continue
                if msg.author != self.bot.user:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Reconcile error: {e}", "WARNING")
        if deleted:
            self.bot.logger.log(MODULE_NAME, f"Reconcile: deleted {deleted} non-bot message(s)")
        ts = int(_now().timestamp())
        with _db_conn() as c:
            cached = c.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
        self._status_state.update({
            "cached": cached,
            "last_reconcile_ts": ts,
            "orphans_deleted": deleted,
        })
        await _post_status(self.bot, chan, self._status_state)

    async def _reconcile_loop(self):
        INTERVAL = 30 * 60  # 30 minutes
        while True:
            await asyncio.sleep(INTERVAL)
            await self.reconcile_channel()

    def shutdown(self):
        # Signal the backfill loop to stop at the next checkpoint — don't cancel mid-batch
        # or the in-flight send_task won't store its result to the DB.
        self._shutdown_flag = True
        self.backfill_active = False

    async def backfill_cache(self):
        if not self.song_index:
            self.bot.logger.log(MODULE_NAME, "No song index to backfill from", "WARNING")
            return
        chan = discord.utils.get(self.bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
        if not chan:
            self.bot.logger.log(MODULE_NAME, f"Cannot backfill — no #{CACHE_CHANNEL_NAME} channel", "WARNING")
            return
        self.backfill_active = True
        self._shutdown_flag = False
        _meta_set("backfill_active", "1")
        _meta_set("backfill_enabled", "1")
        try:
            await self._backfill_cache(chan)
        finally:
            self.backfill_active = False
            _meta_del("backfill_active")
            _meta_del("backfill_enabled")

    async def _backfill_cache(self, chan):
        max_bytes = 95 * 1024 * 1024  # guild supports 100MB; leave 5MB headroom
        per_file_limit = 50 * 1024 * 1024  # single-file limit; matches current boost tier

        # Recover any uploads that made it to Discord but weren't stored (e.g. mid-upload disconnect)
        status_id = _meta_get("status_msg_id")
        recovered = 0
        try:
            async for msg in chan.history(limit=None):
                if str(msg.id) == status_id:
                    continue
                if msg.author != self.bot.user or not msg.attachments:
                    continue
                for att in msg.attachments:
                    att_key = normalize_title(Path(att.filename).stem)
                    fp = next(
                        (entries[0]['path'] for fmt in FORMATS
                         for key, entries in self.song_index.get(fmt, {}).items()
                         if key == att_key),
                        None
                    )
                    if fp and not _cache_lookup(fp):
                        try:
                            disk_size = Path(fp).stat().st_size
                            was_transcoded = 1 if att.size < disk_size * 0.85 else 0
                        except Exception:
                            disk_size = att.size
                            was_transcoded = 0
                        _cache_store(fp, att.url, str(msg.id), str(chan.id),
                                     Path(fp).name, att.size, transcoded=was_transcoded)
                        recovered += 1
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Channel recovery scan error: {e}", "WARNING")
        if recovered:
            self.bot.logger.log(MODULE_NAME, f"Recovered {recovered} untracked upload(s) from channel")

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
        pending, cached, _, total_pending_bytes = await loop.run_in_executor(
            METADATA_EXECUTOR, self._scan_pending, list(seen))

        self.bot.logger.log(MODULE_NAME,
            f"Cache backfill: {len(pending)}/{total} files need upload "
            f"(max {max_bytes // 1024 // 1024}MB per message)")

        with _db_conn() as c:
            db_count = c.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
        channel_count = db_count  # post-recovery, DB and channel are in sync
        _meta_set("channel_cached", str(channel_count))
        self._status_state["cached"] = channel_count
        self._status_state["db_cached"] = db_count
        self._status_state["channel_cached"] = channel_count
        await _post_status(self.bot, chan, self._status_state)

        if not pending:
            return

        uploaded = 0
        errors = 0
        uploaded_bytes = 0
        total_bytes = total_pending_bytes  # pre-computed so ETA is available from batch 1
        start = time.time()
        speed_samples = []  # list of (mb, secs) per batch for rolling avg

        batch = []
        batch_size = 0
        sent_batches = 0

        for fp in pending:
            await asyncio.sleep(0)  # yield each iteration so event loop stays responsive
            if self._shutdown_flag:
                self.bot.logger.log(MODULE_NAME, "Backfill interrupted by shutdown")
                return
            if _cache_lookup(fp):
                cached += 1
                continue
            p = Path(fp)
            try:
                sz = await asyncio.wait_for(
                    loop.run_in_executor(METADATA_EXECUTOR, lambda: p.stat().st_size),
                    timeout=3.0
                )
            except (OSError, asyncio.TimeoutError):
                self.bot.logger.log(MODULE_NAME, f"Skipping {p.name} — stat timed out", "WARNING")
                continue
            if sz > per_file_limit:
                if p.suffix.lower() != '.flac':
                    self.bot.logger.log(MODULE_NAME, f"Skipping {p.name} — non-FLAC over {per_file_limit // (1024*1024)}MB, cannot transcode", "WARNING")
                    errors += 1
                    sent_batches += 1
                    await asyncio.sleep(3)
                    continue
                ds_path, ds_sz, transcoded = await loop.run_in_executor(
                    METADATA_EXECUTOR, _downsample_flac, fp, per_file_limit)
                if ds_path and ds_sz and ds_sz <= per_file_limit:
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
                await asyncio.sleep(3)
                if self._shutdown_flag:
                    self.bot.logger.log(MODULE_NAME, "Backfill interrupted by shutdown")
                    return
                continue
            if batch and batch_size + sz > max_bytes:
                _cur_batch = batch
                _batch_mb_pre = sum(s for _, _, s in _cur_batch) / 1024 / 1024
                self._status_state["uploading"] = f"{len(_cur_batch)} file(s), {_batch_mb_pre:.0f}MB"
                await _post_status(self.bot, chan, self._status_state)
                _t0 = time.time()
                ok = await self._send_batch(chan, _cur_batch)
                self._status_state["uploading"] = None
                if ok:
                    uploaded += len(_cur_batch)
                    uploaded_bytes += sum(s for _, _, s in _cur_batch)
                else:
                    uploaded, uploaded_bytes, errors = await self._fallback_batch(
                        chan, _cur_batch, uploaded, uploaded_bytes, errors)
                _t1 = time.time()
                _batch_mb = sum(s for _, _, s in _cur_batch) / 1024 / 1024
                _batch_secs = _t1 - _t0
                if _batch_secs > 0 and _batch_mb > 0:
                    speed_samples.append((_batch_mb, _batch_secs))
                    if len(speed_samples) > 10:
                        speed_samples.pop(0)
                _total_mb = sum(m for m, _ in speed_samples)
                _total_secs = sum(s for _, s in speed_samples)
                _mbps = _total_mb / _total_secs if _total_secs > 0 else None
                _remaining_mb = (total_bytes - uploaded_bytes) / 1024 / 1024
                with _db_conn() as c:
                    db_count = c.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
                _meta_set("channel_cached", str(db_count))
                self._status_state["cached"] = db_count
                self._status_state["db_cached"] = db_count
                self._status_state["channel_cached"] = db_count
                self._status_state["mbps"] = _mbps
                self._status_state["remaining_mb"] = _remaining_mb
                self._status_state["last_batch"] = f"{len(_cur_batch)} file(s), {_batch_mb:.0f}MB in {_batch_secs:.0f}s"
                await _post_status(self.bot, chan, self._status_state)
                sent_batches += 1
                batch = []
                batch_size = 0
                await asyncio.sleep(3)
                if self._shutdown_flag:
                    self.bot.logger.log(MODULE_NAME, "Backfill interrupted by shutdown")
                    return
            batch.append((fp, p, sz))
            batch_size += sz

        if batch:
            batch = [(fp, p, sz) for fp, p, sz in batch if not _cache_lookup(fp)]
            if batch:
                _cur_batch = batch
                _batch_mb_pre = sum(s for _, _, s in _cur_batch) / 1024 / 1024
                self._status_state["uploading"] = f"{len(_cur_batch)} file(s), {_batch_mb_pre:.0f}MB"
                await _post_status(self.bot, chan, self._status_state)
                _t0 = time.time()
                ok = await self._send_batch(chan, _cur_batch)
                self._status_state["uploading"] = None
                if ok:
                    uploaded += len(_cur_batch)
                    uploaded_bytes += sum(s for _, _, s in _cur_batch)
                else:
                    uploaded, uploaded_bytes, errors = await self._fallback_batch(
                        chan, _cur_batch, uploaded, uploaded_bytes, errors)
                _t1 = time.time()
                _batch_mb = sum(s for _, _, s in _cur_batch) / 1024 / 1024
                _batch_secs = _t1 - _t0
                if _batch_secs > 0 and _batch_mb > 0:
                    speed_samples.append((_batch_mb, _batch_secs))
                    if len(speed_samples) > 10:
                        speed_samples.pop(0)
                _total_mb = sum(m for m, _ in speed_samples)
                _total_secs = sum(s for _, s in speed_samples)
                _mbps = _total_mb / _total_secs if _total_secs > 0 else None
                _remaining_mb = (total_bytes - uploaded_bytes) / 1024 / 1024
                with _db_conn() as c:
                    db_count = c.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
                _meta_set("channel_cached", str(db_count))
                self._status_state["cached"] = db_count
                self._status_state["db_cached"] = db_count
                self._status_state["channel_cached"] = db_count
                self._status_state["mbps"] = _mbps
                self._status_state["remaining_mb"] = _remaining_mb
                self._status_state["last_batch"] = f"{len(_cur_batch)} file(s), {_batch_mb:.0f}MB in {_batch_secs:.0f}s"
                await _post_status(self.bot, chan, self._status_state)
                sent_batches += 1
        self.bot.logger.log(MODULE_NAME,
            f"Cache backfill complete: {uploaded} uploaded in {sent_batches} batch(es), "
            f"{cached} cached, {errors} errors")

    def _scan_pending(self, seen):
        pending = []
        cached = 0
        with _db_conn() as c:
            live_ids = {r[0] for r in c.execute("SELECT message_id FROM song_cache").fetchall()}
        for fp in sorted(seen):
            try:
                entry = _cache_lookup(fp)
                if entry and entry.get("message_id") in live_ids:
                    cached += 1
                else:
                    pending.append(fp)
            except Exception:
                pass
        # Stat pending files to get total bytes for ETA calculation
        total_pending_bytes = 0
        for fp in pending:
            try:
                total_pending_bytes += Path(fp).stat().st_size
            except Exception:
                pass
        return pending, cached, len(seen) - len(pending) - cached, total_pending_bytes

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


    async def _send_batch(self, chan, batch, source_path=None, transcoded=False) -> bool:
        read_start = time.time()
        loop = asyncio.get_running_loop()
        file_data = []
        for fp, p, sz in batch:
            data = await loop.run_in_executor(UPLOAD_EXECUTOR, p.read_bytes)
            await asyncio.sleep(0)  # yield between file reads
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
        # match attachments by normalized filename — Discord doesn't guarantee order
        att_map = {normalize_title(Path(a.filename).stem): a for a in msg.attachments}
        ok = True
        for i, (fp, name, data, sz) in enumerate(file_data):
            actual_path = source_path if source_path and len(batch) == 1 else fp
            att = att_map.get(normalize_title(Path(name).stem))
            if att is None:
                self.bot.logger.log(MODULE_NAME, f"No attachment match for {name}", "WARNING")
                ok = False
                continue
            self.bot.logger.log(MODULE_NAME, f"Storing {i+1}/{len(batch)}: {name} att={att.filename}")
            try:
                checksum = await loop.run_in_executor(UPLOAD_EXECUTOR, _file_checksum, actual_path)
                _cache_store(fp, att.url, msg.id, chan.id, name, sz, checksum, int(transcoded))
                self.bot.logger.log(MODULE_NAME, f"Stored {i+1}/{len(batch)}: {name}")
            except Exception as store_err:
                self.bot.logger.log(MODULE_NAME, f"Store failed {i+1}/{len(batch)} {name}: {store_err}", "WARNING")
                ok = False
        self.bot.logger.log(MODULE_NAME,
            f"Cached batch: {len(batch)} file(s), {total_mb}MB "
            f"(read: {read_elapsed:.1f}s, upload: {up_elapsed:.1f}s)")
        if len(msg.attachments) != len(batch):
            self.bot.logger.log(MODULE_NAME,
                f"Batch mismatch: sent {len(batch)} files, got {len(msg.attachments)} attachments", "WARNING")
            ok = False
        await asyncio.sleep(0)  # yield after upload so event loop can process other work
        return ok

def setup(bot):
    from mod_core import is_owner

    bot.logger.log(MODULE_NAME, "Setting up ARCHIVE module")

    old_mgr = getattr(bot, 'ARCHIVE_manager', None)
    if old_mgr:
        old_mgr.shutdown()

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

    @bot.tree.command(name="backfill_start", description="[Owner only] Start the song cache backfill (persists across restarts)")
    async def backfill_start(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        if ARCHIVE_manager.backfill_active:
            await interaction.response.send_message("Backfill is already running.", ephemeral=True)
            return
        await interaction.response.send_message("Backfill started — will resume automatically after any restart.", ephemeral=True)
        asyncio.create_task(ARCHIVE_manager.backfill_cache())

    @bot.tree.command(name="backfill_stop", description="[Owner only] Stop the song cache backfill and clear the resume flag")
    async def backfill_stop(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        _meta_del("backfill_enabled")
        ARCHIVE_manager._shutdown_flag = True
        if ARCHIVE_manager.backfill_active:
            await interaction.response.send_message("Backfill will stop after the current batch finishes.", ephemeral=True)
        else:
            await interaction.response.send_message("Backfill wasn't running — resume flag cleared.", ephemeral=True)

    @bot.tree.command(name="cache_list", description="[Owner only] List every file in the songcache channel")
    @app_commands.describe(search="Optional filter — returns only filenames containing this string")
    async def cache_list(interaction: discord.Interaction, search: str = ""):
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        chan = discord.utils.get(interaction.guild.text_channels, name=CACHE_CHANNEL_NAME)
        if not chan:
            await interaction.followup.send("songcache channel not found.", ephemeral=True)
            return
        names = []
        async for msg in chan.history(limit=None):
            for att in msg.attachments:
                names.append(att.filename)
        names.sort()
        if search:
            names = [n for n in names if search.lower() in n.lower()]
        if not names:
            await interaction.followup.send(
                f"No files found{f' matching `{search}`' if search else ''}.", ephemeral=True)
            return
        text = f"# Songcache — {len(names)} file(s){f' matching `{search}`' if search else ''}\n\n"
        text += "\n".join(names)
        buf = text.encode()
        import io
        await interaction.followup.send(
            f"**{len(names)} file(s)** in channel{f' matching `{search}`' if search else ''}.",
            file=discord.File(io.BytesIO(buf), filename="cache_list.txt"),
            ephemeral=True)
        bot.logger.log(MODULE_NAME, f"cache_list: {len(names)} files returned to {interaction.user}")

    class ClearCacheConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)

        @discord.ui.button(label="Yes, wipe everything", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.stop()
            chan = discord.utils.get(interaction.guild.text_channels, name=CACHE_CHANNEL_NAME)
            category = chan.category if chan else None
            position = chan.position if chan else None
            overwrites = chan.overwrites if chan else {}
            topic = chan.topic if chan else None
            if chan:
                await chan.delete(reason=f"Cache wipe by {interaction.user}")
            new_chan = await interaction.guild.create_text_channel(
                CACHE_CHANNEL_NAME, category=category, position=position,
                overwrites=overwrites, topic=topic,
                reason=f"Cache wipe by {interaction.user}"
            )
            with _db_conn() as c:
                c.execute("DELETE FROM song_cache")
                c.execute("DELETE FROM song_cache_fails")
                c.execute("DELETE FROM cache_meta")
            ARCHIVE_manager.backfill_active = False
            ARCHIVE_manager._shutdown_flag = False
            bot.logger.log(MODULE_NAME, f"Cache DB and #{CACHE_CHANNEL_NAME} wiped by {interaction.user}")
            await interaction.response.edit_message(
                content=f"Done — #{CACHE_CHANNEL_NAME} deleted and recreated, DB wiped.", view=None)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.stop()
            await interaction.response.edit_message(content="Cancelled.", view=None)

    @bot.tree.command(name="clear_cache", description="[Owner only] Wipe cache DB and delete/recreate the songcache channel")
    async def clear_cache(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.send_message(
            "⚠️ This will **wipe the entire cache DB** and **delete + recreate** the songcache channel. Are you sure?",
            view=ClearCacheConfirmView(), ephemeral=True)

    bot.logger.log(MODULE_NAME, "ARCHIVE module setup complete")
