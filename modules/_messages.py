import json
import sqlite3
import discord
from cryptography.fernet import Fernet
from collections import deque
from typing import Optional, Dict, List
from pathlib import Path
from _utils import _now, script_dir

# ---------------------------------------------------------------------------
# In-memory cache — unchanged, required by mod_core and vms_playback
# ---------------------------------------------------------------------------

_fernet = Fernet(Fernet.generate_key())

# text cache: {guild_id: {channel_id: [msg_data, ...]}}
message_cache: Dict[str, Dict[str, list]] = {}
_msg_cache_max_channels = 200
_msg_cache_max_per_channel = 100

# media cache: {message_id: {files, author_id, guild_id, cached_at}}
media_cache: Dict[int, Dict] = {}
_media_cache_ttl = 3600


def encrypt(data: bytes) -> bytes:
    return _fernet.encrypt(data)

def decrypt(data: bytes) -> bytes:
    return _fernet.decrypt(data)

def delete_media(message_id: int):
    media_cache.pop(message_id, None)

def evict_media_ttl() -> int:
    cutoff = _now().timestamp() - _media_cache_ttl
    expired = [mid for mid, e in list(media_cache.items()) if e.get('cached_at', 0) < cutoff]
    for mid in expired:
        delete_media(mid)
    return len(expired)


# ---------------------------------------------------------------------------
# SQLite archive — persistent, text-only, no media bytes ever stored
# ---------------------------------------------------------------------------

def _db_path() -> str:
    p = script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "messages.db")

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   INTEGER PRIMARY KEY,
    channel_id   INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    author_id    INTEGER NOT NULL,
    author_name  TEXT,
    content      TEXT,
    created_at   TEXT NOT NULL,
    edited_at    TEXT,
    deleted_at   TEXT,
    attachments  TEXT
);
CREATE TABLE IF NOT EXISTS edits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL,
    old_content  TEXT,
    new_content  TEXT,
    edited_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backfill_state (
    channel_id      INTEGER PRIMARY KEY,
    last_message_id INTEGER,
    completed       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_author   ON messages(author_id);
CREATE INDEX IF NOT EXISTS idx_messages_channel  ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_messages_created  ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_deleted  ON messages(deleted_at);
CREATE INDEX IF NOT EXISTS idx_edits_message     ON edits(message_id);
"""

def _init_db():
    with _conn() as c:
        c.executescript(_SCHEMA)

_init_db()


def _attachments_json(message: discord.Message) -> Optional[str]:
    items = [{"url": a.url, "filename": a.filename, "content_type": a.content_type}
             for a in message.attachments]
    return json.dumps(items) if items else None


def archive_message(message: discord.Message):
    """Insert or ignore a message into the archive. Never stores media bytes."""
    if message.guild is None or message.author.bot:
        return
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO messages
              (message_id, channel_id, guild_id, author_id, author_name,
               content, created_at, attachments)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            message.id,
            message.channel.id,
            message.guild.id,
            message.author.id,
            str(message.author),
            message.content or "",
            message.created_at.isoformat(),
            _attachments_json(message),
        ))


def archive_message_row(
    message_id: int, channel_id: int, guild_id: int,
    author_id: int, author_name: str,
    content: str, created_at: str,
    attachments_json: Optional[str] = None,
):
    """Insert a raw row — used by backfill which works without discord.Message objects."""
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO messages
              (message_id, channel_id, guild_id, author_id, author_name,
               content, created_at, attachments)
            VALUES (?,?,?,?,?,?,?,?)
        """, (message_id, channel_id, guild_id, author_id, author_name,
              content or "", created_at, attachments_json))


def archive_edit(message_id: int, old_content: str, new_content: str, edited_at: str):
    """Record an edit and update the live content in messages."""
    with _conn() as c:
        c.execute("""
            INSERT INTO edits (message_id, old_content, new_content, edited_at)
            VALUES (?,?,?,?)
        """, (message_id, old_content, new_content, edited_at))
        c.execute("""
            UPDATE messages SET content=?, edited_at=? WHERE message_id=?
        """, (new_content, edited_at, message_id))


def archive_delete(message_id: int, deleted_at: str):
    """Mark a message as deleted."""
    with _conn() as c:
        c.execute("UPDATE messages SET deleted_at=? WHERE message_id=?",
                  (deleted_at, message_id))


def archive_bulk_delete(message_ids: list, deleted_at: str):
    with _conn() as c:
        c.executemany("UPDATE messages SET deleted_at=? WHERE message_id=?",
                      [(deleted_at, mid) for mid in message_ids])


# Backfill state

def backfill_get_state(channel_id: int) -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM backfill_state WHERE channel_id=?",
                        (channel_id,)).fetchone()
        return dict(row) if row else {"channel_id": channel_id,
                                      "last_message_id": None, "completed": 0}

def backfill_set_progress(channel_id: int, last_message_id: int):
    with _conn() as c:
        c.execute("""
            INSERT INTO backfill_state (channel_id, last_message_id, completed)
            VALUES (?,?,0)
            ON CONFLICT(channel_id) DO UPDATE SET last_message_id=excluded.last_message_id
        """, (channel_id, last_message_id))

def backfill_set_complete(channel_id: int):
    with _conn() as c:
        c.execute("""
            INSERT INTO backfill_state (channel_id, last_message_id, completed)
            VALUES (?,NULL,1)
            ON CONFLICT(channel_id) DO UPDATE SET completed=1
        """, (channel_id,))

def backfill_stats() -> dict:
    with _conn() as c:
        total    = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        deleted  = c.execute("SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL").fetchone()[0]
        edits    = c.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
        ch_done  = c.execute("SELECT COUNT(*) FROM backfill_state WHERE completed=1").fetchone()[0]
        ch_prog  = c.execute("SELECT COUNT(*) FROM backfill_state WHERE completed=0").fetchone()[0]
        return {"messages": total, "deleted": deleted, "edits": edits,
                "channels_complete": ch_done, "channels_in_progress": ch_prog}


# Search / query

def search_messages(
    guild_id: int,
    author_id: int = None,
    keyword: str = None,
    channel_id: int = None,
    include_deleted: bool = False,
    after: str = None,
    before: str = None,
    limit: int = 100,
) -> List[dict]:
    clauses = ["guild_id=?"]
    params  = [guild_id]
    if author_id:
        clauses.append("author_id=?"); params.append(author_id)
    if channel_id:
        clauses.append("channel_id=?"); params.append(channel_id)
    if keyword:
        clauses.append("content LIKE ?"); params.append(f"%{keyword}%")
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if after:
        clauses.append("created_at>=?"); params.append(after)
    if before:
        clauses.append("created_at<=?"); params.append(before)
    params.append(limit)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_deleted(guild_id: int, author_id: int, limit: int = 50) -> List[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM messages
            WHERE guild_id=? AND author_id=? AND deleted_at IS NOT NULL
            ORDER BY deleted_at DESC LIMIT ?
        """, (guild_id, author_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_user_edits(guild_id: int, author_id: int, limit: int = 50) -> List[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT e.*, m.channel_id FROM edits e
            JOIN messages m ON e.message_id=m.message_id
            WHERE m.guild_id=? AND m.author_id=?
            ORDER BY e.edited_at DESC LIMIT ?
        """, (guild_id, author_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# cache_message — writes to both in-memory cache AND archive
# ---------------------------------------------------------------------------

async def cache_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return

    guild_id   = str(message.guild.id)
    channel_id = str(message.channel.id)
    guild_cache = message_cache.setdefault(guild_id, {})

    if channel_id not in guild_cache and len(guild_cache) >= _msg_cache_max_channels:
        evict_ch = next(iter(guild_cache))
        for m in guild_cache.pop(evict_ch, []):
            mid = m.get('id')
            if mid is not None:
                delete_media(mid)

    guild_cache.setdefault(channel_id, [])

    # Download and encrypt media for in-memory cache only
    downloaded = []
    for att in message.attachments:
        try:
            data = await att.read()
            downloaded.append({
                'filename':     att.filename,
                'data':         encrypt(data),
                'content_type': att.content_type or 'application/octet-stream',
                'url':          att.url,
            })
        except Exception:
            pass

    if downloaded:
        media_cache[message.id] = {
            'files':     downloaded,
            'author_id': message.author.id,
            'guild_id':  message.guild.id,
            'cached_at': _now().timestamp(),
        }

    cache_list = guild_cache[channel_id]
    cache_list.append({
        'id':          message.id,
        'author':      str(message.author),
        'author_id':   message.author.id,
        'content':     message.content,
        'timestamp':   message.created_at.isoformat(),
        'attachments': [att.url for att in message.attachments],
        'embeds':      len(message.embeds),
    })
    if len(cache_list) > _msg_cache_max_per_channel:
        evicted = cache_list.pop(0)
        if evicted.get('id'):
            delete_media(evicted['id'])

    # Also write to persistent archive (CDN URLs only, no bytes)
    archive_message(message)


# ---------------------------------------------------------------------------
# Context/recent helpers — check archive DB if not in memory cache
# ---------------------------------------------------------------------------

def get_context_messages(
    guild_id: int, channel_id: int,
    around_message_id: int, count: int = 10
) -> List[Dict]:
    msgs = message_cache.get(str(guild_id), {}).get(str(channel_id), [])
    idx = next((i for i, m in enumerate(msgs) if m['id'] == around_message_id), None)
    if idx is None:
        return msgs[-count:]
    half = count // 2
    return msgs[max(0, idx - half):min(len(msgs), idx + half + 1)]

def get_recent_messages(guild_id: str, channel_id: str, limit: int = 20) -> List[str]:
    msgs = message_cache.get(guild_id, {}).get(channel_id, [])
    return [m.get('content', '') for m in msgs[-limit:] if m.get('content')]
