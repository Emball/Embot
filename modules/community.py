
import asyncio
import discord
from discord.ext import tasks
from discord import app_commands
import sqlite3
import hashlib
import re
import json
import random
import string
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple
from pathlib import Path
from _utils import script_dir, _now

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    import pytz
    CST = pytz.timezone("America/Chicago")
except ImportError:
    CST = None

MODULE_NAME = "COMMUNITY"

PROJECTS_CHANNEL_NAME  = "projects"
ARTWORK_CHANNEL_NAME   = "artwork"
ANNOUNCEMENTS_CHANNEL_NAME = "announcements"
GENERAL_CHANNEL_NAME   = "general"

VOTE_EMOJIS: dict[str, int] = {
    "🔥": 5,

    "😐": 0,
    "🗑️": -5,
}
SETUP_EMOJIS = ["🔥", "😐", "🗑️"]

MIN_DESCRIPTION_LENGTH = 10
VERSION_REENTRY_DAYS   = 30

def _now_str() -> str:
    return _now().isoformat()

def _parse_version(text: str) -> Optional[Tuple[int, int]]:
    m = re.search(r'\bv(\d+)(?:\.(\d+))?\b', text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2) or 0)
    return None

def _strip_version(text: str) -> str:
    return re.sub(r'\bv\d+(?:\.\d+)?\b', '', text, flags=re.IGNORECASE).strip()

def _extract_title(content: str) -> Optional[str]:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            return re.sub(r'^#+\s*', '', stripped)[:200]
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return None

def _extract_links(content: str) -> List[str]:
    return re.findall(r'https?://[^\s<>"]+', content)

def _normalize(content: str) -> str:
    text = _strip_version(content)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    return re.sub(r'\s+', '', text).strip().lower()

async def _hash_url(url: str, session: Optional["aiohttp.ClientSession"] = None) -> Optional[str]:
    if not HAS_AIOHTTP:
        return None
    try:
        async def _do_hash(sess):
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    return None
                return hashlib.sha256(await r.read()).hexdigest()

        if session is not None:
            return await _do_hash(session)
        else:
            async with aiohttp.ClientSession() as sess:
                return await _do_hash(sess)
    except Exception:
        return None

async def _hash_attachment(att: discord.Attachment, session: Optional["aiohttp.ClientSession"] = None) -> Optional[str]:
    return await _hash_url(att.url, session)

_SHORT_ID_CHARS = string.ascii_letters + string.digits

def _short_id(length: int = 9) -> str:
    return "".join(random.choices(_SHORT_ID_CHARS, k=length))

def _display_name(bot, guild_id: int, user_id: int) -> str:
    try:
        guild = bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                return member.display_name
    except Exception:
        pass
    return f"user:{user_id}"

class CommunityDB:
    _MIGRATIONS: list[tuple[int, str, str]] = [
        (1, "Initial schema", """
            CREATE TABLE IF NOT EXISTS community_config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id                 TEXT PRIMARY KEY,
                group_id           TEXT NOT NULL,
                user_id            INTEGER NOT NULL,
                channel_id         INTEGER NOT NULL,
                message_id         INTEGER NOT NULL,
                thread_id          INTEGER,
                title              TEXT,
                content            TEXT NOT NULL,
                content_normalized TEXT NOT NULL,
                file_hashes        TEXT NOT NULL DEFAULT '[]',
                links              TEXT NOT NULL DEFAULT '[]',
                version            TEXT NOT NULL DEFAULT 'v1.0',
                version_major      INTEGER NOT NULL DEFAULT 1,
                version_minor      INTEGER NOT NULL DEFAULT 0,
                is_deleted         INTEGER NOT NULL DEFAULT 0,
                is_current         INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                last_checked_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sub_group   ON submissions(group_id);
            CREATE INDEX IF NOT EXISTS idx_sub_user    ON submissions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sub_msg     ON submissions(message_id);
            CREATE INDEX IF NOT EXISTS idx_sub_created ON submissions(created_at);

            CREATE TABLE IF NOT EXISTS votes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id         TEXT NOT NULL,
                user_id          INTEGER NOT NULL,
                emoji            TEXT NOT NULL,
                xp_delta         INTEGER NOT NULL,
                voted_message_id INTEGER NOT NULL,
                created_at       TEXT NOT NULL,
                UNIQUE(group_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_vote_group   ON votes(group_id);
            CREATE INDEX IF NOT EXISTS idx_vote_created ON votes(created_at);

            CREATE TABLE IF NOT EXISTS xp_ledger (
                user_id    INTEGER PRIMARY KEY,
                xp         REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS thread_xp_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                submitter_id INTEGER NOT NULL,
                thread_id    INTEGER NOT NULL,
                message_id   INTEGER NOT NULL UNIQUE,
                xp_delta     REAL NOT NULL DEFAULT 0.1,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spotlight_history (
                week_key      TEXT PRIMARY KEY,
                group_id      TEXT,
                submission_id TEXT,
                posted_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS file_hash_registry (
                hash          TEXT NOT NULL,
                submission_id TEXT NOT NULL,
                user_id       INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                PRIMARY KEY (hash, submission_id)
            );
            CREATE INDEX IF NOT EXISTS idx_hash ON file_hash_registry(hash);
        """),

        (2, "Add reactions_blocked flag to submissions", lambda c, col_ok, tbl_ok: (
            c.execute("ALTER TABLE submissions ADD COLUMN reactions_blocked INTEGER NOT NULL DEFAULT 0")
            if not col_ok(c, "submissions", "reactions_blocked") else None
        )),
    ]

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def query_thread_submission(self, thread_id: int):
        with self._lock:
            with self._conn() as c:
                return c.execute(
                    "SELECT * FROM submissions WHERE thread_id=? AND is_deleted=0 LIMIT 1",
                    (thread_id,)
                ).fetchone()

    def _get_schema_version(self, c: sqlite3.Connection) -> int:
        try:
            row = c.execute(
                "SELECT MAX(version) AS v FROM schema_migration_log"
            ).fetchone()
            return int(row["v"]) if row and row["v"] is not None else 0
        except sqlite3.OperationalError:
            return 0

    def _column_exists(self, c: sqlite3.Connection, table: str, column: str) -> bool:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def _table_exists(self, c: sqlite3.Connection, table: str) -> bool:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS schema_migration_log (
                    version     INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at  TEXT NOT NULL
                )
            """)
            c.commit()

        with self._conn() as c:
            current_version = self._get_schema_version(c)
        pending = [m for m in self._MIGRATIONS
                   if m[0] > current_version]

        if not pending:
            return

        for version, description, sql in pending:
            print(f"[COMMUNITY] [INFO] Applying migration v{version}: {description}")
            try:
                with self._conn() as c:
                    if callable(sql):
                        sql(c, self._column_exists, self._table_exists)
                    else:
                        c.executescript(sql)
                    c.execute(
                        "INSERT OR IGNORE INTO schema_migration_log (version, description, applied_at) "
                        "VALUES (?, ?, ?)",
                        (version, description, _now_str())
                    )
                    c.commit()
                print(f"[COMMUNITY] [INFO] Migration v{version} applied successfully.")
            except Exception as e:
                print(
                    f"[COMMUNITY] [ERROR] Migration v{version} FAILED: {e}\n"
                    f" The database has NOT been modified for this migration."
                )
                raise

    def get_config(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM community_config WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except Exception:
                    return row["value"]
            return default

    def set_config(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO community_config (key, value) VALUES (?,?)",
                (key, json.dumps(value))
            )
            c.commit()

    def add_submission(self, sub: dict):
        with self._conn() as c:
            c.execute("""
                INSERT INTO submissions
                (id, group_id, user_id, channel_id, message_id, thread_id,
                 title, content, content_normalized, file_hashes, links,
                 version, version_major, version_minor,
                 is_deleted, is_current, created_at, updated_at, last_checked_at)
                VALUES
                (:id,:group_id,:user_id,:channel_id,:message_id,:thread_id,
                 :title,:content,:content_normalized,:file_hashes,:links,
                 :version,:version_major,:version_minor,
                 :is_deleted,:is_current,:created_at,:updated_at,:last_checked_at)
            """, sub)
            c.commit()

    def update_submission(self, sub_id: str, **kwargs):
        if not kwargs:
            return
        kwargs["updated_at"] = _now_str()
        set_clause = ", ".join(f"{k}=:{k}"for k in kwargs)
        kwargs["id"] = sub_id
        with self._conn() as c:
            c.execute(f"UPDATE submissions SET {set_clause} WHERE id=:id", kwargs)
            c.commit()

    def by_message(self, message_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM submissions WHERE message_id=? AND is_deleted=0", (message_id,)
            ).fetchone()

    def by_group(self, group_id: str) -> List[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM submissions WHERE group_id=? ORDER BY version_major, version_minor",
                (group_id,)
            ).fetchall()

    def find_existing(self, user_id: int, norm: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM submissions WHERE user_id=? AND content_normalized=? AND is_deleted=0 "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id, norm)
            ).fetchone()

    def get_vote(self, group_id: str, user_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM votes WHERE group_id=? AND user_id=?", (group_id, user_id)
            ).fetchone()

    def upsert_vote(self, group_id: str, user_id: int, emoji: str,
                    xp_delta: int, message_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            old = c.execute(
                "SELECT * FROM votes WHERE group_id=? AND user_id=?", (group_id, user_id)
            ).fetchone()
            c.execute("""
                INSERT OR REPLACE INTO votes
                (group_id, user_id, emoji, xp_delta, voted_message_id, created_at)
                VALUES (?,?,?,?,?,?)
            """, (group_id, user_id, emoji, xp_delta, message_id, _now_str()))
            c.commit()
            return old

    def remove_vote(self, group_id: str, user_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            old = c.execute(
                "SELECT * FROM votes WHERE group_id=? AND user_id=?", (group_id, user_id)
            ).fetchone()
            if old:
                c.execute("DELETE FROM votes WHERE group_id=? AND user_id=?", (group_id, user_id))
                c.commit()
            return old

    def add_xp(self, user_id: int, delta: float):
        with self._conn() as c:
            c.execute("""
                INSERT INTO xp_ledger (user_id, xp, updated_at) VALUES (?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET xp=xp+excluded.xp, updated_at=excluded.updated_at
            """, (user_id, delta, _now_str()))
            c.commit()

    def get_xp(self, user_id: int) -> float:
        with self._conn() as c:
            row = c.execute("SELECT xp FROM xp_ledger WHERE user_id=?", (user_id,)).fetchone()
            return float(row["xp"]) if row else 0.0

    def get_leaderboard(self, limit: int = 10) -> List[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT user_id, xp FROM xp_ledger ORDER BY xp DESC LIMIT ?", (limit,)
            ).fetchall()

    def log_thread_xp(self, submitter_id: int, thread_id: int, message_id: int):
        with self._conn() as c:
            try:
                c.execute("""
                    INSERT INTO thread_xp_log (submitter_id, thread_id, message_id, xp_delta, created_at)
                    VALUES (?,?,?,0.1,?)
                """, (submitter_id, thread_id, message_id, _now_str()))
                c.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def week_key(self) -> str:
        return _now().strftime("%Y-%W")

    def spotlight_ran_this_week(self) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM spotlight_history WHERE week_key=?", (self.week_key(),)
            ).fetchone() is not None

    def record_spotlight(self, group_id: Optional[str], submission_id: Optional[str]):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO spotlight_history (week_key, group_id, submission_id, posted_at) VALUES (?,?,?,?)",
                (self.week_key(), group_id, submission_id, _now_str())
            )
            c.commit()

    def top_submission_this_week(self, exclude_user: Optional[int] = None) -> Optional[sqlite3.Row]:
        since = (_now() - timedelta(days=7)).isoformat()
        q = """
            SELECT s.group_id, s.id AS submission_id, s.user_id, s.title,
                   s.content, s.channel_id, s.message_id, s.thread_id, s.version,
                   s.links,
                   COALESCE(SUM(v.xp_delta), 0) AS total_xp
            FROM submissions s
            LEFT JOIN votes v ON v.group_id = s.group_id AND v.created_at >= ?
            WHERE s.is_deleted=0 AND s.is_current=1
        """
        params: list = [since]
        if exclude_user:
            q += "AND s.user_id != ?"
            params.append(exclude_user)
        q += "GROUP BY s.group_id ORDER BY total_xp DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(q, params).fetchone()
            return row if (row and row["total_xp"] > 0) else None

    def get_vote_counts(self, group_id: str, since: str) -> dict:
        counts: dict[str, int] = {"🔥": 0, "😐": 0, "🗑️": 0}
        with self._conn() as c:
            rows = c.execute(
                "SELECT emoji, COUNT(*) AS cnt FROM votes WHERE group_id=? AND created_at>=? GROUP BY emoji",
                (group_id, since)
            ).fetchall()
        for row in rows:
            counts[row["emoji"]] = row["cnt"]
        return counts

    def register_hash(self, h: str, sub_id: str, user_id: int):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO file_hash_registry (hash, submission_id, user_id, created_at) VALUES (?,?,?,?)",
                (h, sub_id, user_id, _now_str())
            )
            c.commit()

    def hash_owner(self, h: str, exclude_user: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT fhr.*, s.user_id AS owner_id FROM file_hash_registry fhr "
                "JOIN submissions s ON s.id = fhr.submission_id "
                "WHERE fhr.hash=? AND fhr.user_id!=? AND s.is_deleted=0 LIMIT 1",
                (h, exclude_user)
            ).fetchone()

    def link_owner(self, link: str, exclude_user: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                """SELECT s.* FROM submissions s, json_each(s.links) AS l
                   WHERE l.value = ? AND s.user_id != ? AND s.is_deleted = 0
                   LIMIT 1""",
                (link, exclude_user)
            ).fetchone()

    def group_for_hash(self, h: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT s.group_id FROM file_hash_registry fhr "
                "JOIN submissions s ON s.id=fhr.submission_id "
                "WHERE fhr.hash=? LIMIT 1", (h,)
            ).fetchone()
            return row["group_id"] if row else None

    def group_for_link(self, link: str, user_id: int) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                """SELECT s.group_id FROM submissions s, json_each(s.links) AS l
                   WHERE l.value = ? AND s.user_id = ? AND s.is_deleted = 0
                   LIMIT 1""",
                (link, user_id)
            ).fetchone()
            return row["group_id"] if row else None

    def get_checkable_submissions(self, limit: int = 50) -> List[sqlite3.Row]:
        cutoff = (_now() - timedelta(days=VERSION_REENTRY_DAYS)).isoformat()
        with self._conn() as c:
            return c.execute("""
                SELECT * FROM submissions
                WHERE is_deleted=0
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()

class CommunitySystem:
    def __init__(self, bot):
        self.bot = bot
        db_path = script_dir() / "db"/ "community.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = CommunityDB(db_path)
        self._submission_channel_ids: set[int] = set()

    def clog(self, msg: str, level: str = "INFO"):
        self.bot.logger.log(MODULE_NAME, msg, level)

    def cerr(self, msg: str, exc: Exception = None):
        self.bot.logger.error(MODULE_NAME, msg, exc)

    async def _bot_log(self, guild: discord.Guild, embed: discord.Embed):
        try:
            el = getattr(self.bot, "_logger_event_logger", None)
            if el:
                ch = el.get_bot_logs_channel(guild)
                if ch:
                    await ch.send(embed=embed)
                    return
            ch = discord.utils.get(guild.text_channels, name="bot-logs")
            if ch:
                await ch.send(embed=embed)
        except Exception as e:
            self.cerr("Failed to send to bot-logs", e)

    async def _dm_or_ping(self, user: discord.Member, embed: discord.Embed):
        try:
            await user.send(embed=embed)
            return
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            guild = user.guild
            ch = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL_NAME)
            if ch:
                await ch.send(content=user.mention, embed=embed)
        except Exception as e:
            self.cerr("Failed to DM and failed to fallback to general", e)

    def _get_submission_channel_names(self) -> List[str]:
        names = self.db.get_config("submission_channels")
        if names:
            return names
        return [PROJECTS_CHANNEL_NAME, ARTWORK_CHANNEL_NAME]

    def _refresh_channel_ids(self, guild: discord.Guild):
        self._submission_channel_ids = {
            ch.id
            for ch in guild.text_channels
            if ch.name in self._get_submission_channel_names()
        }

    def _is_submission_channel(self, channel_id: int) -> bool:
        return channel_id in self._submission_channel_ids

    def _validate(self, message: discord.Message) -> Optional[str]:
        content = message.content or ""
        links   = _extract_links(content)
        has_file = bool(message.attachments)
        has_link = bool(links)

        if not has_file and not has_link:
            return "Your submission must include at least one **attached file** or **link**."

        is_artwork = message.channel.name == ARTWORK_CHANNEL_NAME
        if is_artwork:
            return None

        title = _extract_title(content)
        if not title or len(title.strip()) < MIN_DESCRIPTION_LENGTH:
            return (
                f"Your submission needs a title or description of at least "
                f"**{MIN_DESCRIPTION_LENGTH} characters**.\n"
                "Use a markdown header like `# My Project` or start with a descriptive sentence."
            )
        return None

    def _invalid_embed(self, reason: str, channel_name: str) -> discord.Embed:
        e = discord.Embed(
            title="Submission Not Accepted",
            description=(
                f"Your post in **#{channel_name}** was removed because it didn't meet "
                "the submission requirements.\n\n"
                f"**Reason:** {reason}\n\n"
                "Please fix the issue and repost. Need help? Ask in the server!"
            ),
            color=0xf39c12,
            timestamp=_now()
        )
        e.set_footer(text="Embot Community System")
        return e

    def _next_version(self, existing_row: sqlite3.Row, new_content: str
                      ) -> Tuple[str, int, int]:
        parsed = _parse_version(new_content)
        if parsed:
            maj, min_ = parsed
        else:
            maj = existing_row["version_major"] + 1
            min_ = 0
        return f"v{maj}.{min_}", maj, min_

    async def handle_submission(self, message: discord.Message):
        if message.author.bot:
            return

        guild = message.guild
        self._refresh_channel_ids(guild)

        err = self._validate(message)
        if err:
            try:
                await message.delete()
            except Exception:
                pass
            embed = self._invalid_embed(err, message.channel.name)
            await self._dm_or_ping(message.author, embed)
            self.clog(f"Rejected submission by {message.author} in #{message.channel.name}: {err}")
            return

        content   = message.content or ""
        norm      = _normalize(content)
        title     = _extract_title(content)
        links     = _extract_links(content)
        user_id   = message.author.id

        attachment_hashes: List[str] = []
        if message.attachments and HAS_AIOHTTP:
            async with aiohttp.ClientSession() as sess:
                for att in message.attachments:
                    h = await _hash_attachment(att, sess)
                    if h:
                        attachment_hashes.append(h)
                    else:
                        self.clog(
                            f"Could not hash attachment '{att.filename}' for {message.author} "
                            f"(network error or unsupported type) — skipping hash for this file.",
                            "WARNING"
                        )

        for h in attachment_hashes:
            owner = self.db.hash_owner(h, exclude_user=user_id)
            if owner:
                try:
                    await message.delete()
                except Exception:
                    pass
                guild = message.guild
                owner_member = guild.get_member(owner["user_id"]) if guild else None
                owner_name = owner_member.display_name if owner_member else f"another member"
                embed = discord.Embed(
                    title="Duplicate Submission Detected",
                    description=(
                        f"Your submission was removed because an attached file has already been "
                        f"submitted by **{owner_name}**. Please only submit your own original work."
                    ),
                    color=0xe74c3c,
                    timestamp=_now()
                )
                embed.set_footer(text="Embot Community System")
                await self._dm_or_ping(message.author, embed)
                self.clog(
                    f"Duplicate attachment from {message.author.display_name} "
                    f"— matches submission by {owner_name}."
                )
                return

        for link in links:
            owner_row = self.db.link_owner(link, exclude_user=user_id)
            if owner_row:
                try:
                    await message.delete()
                except Exception:
                    pass
                guild = message.guild
                owner_member = guild.get_member(owner_row["user_id"]) if guild else None
                owner_name = owner_member.display_name if owner_member else "another member"
                embed = discord.Embed(
                    title="Duplicate Link Detected",
                    description=(
                        f"Your submission was removed because that link has already been "
                        f"submitted by **{owner_name}**. Please only submit your own original work."
                    ),
                    color=0xe74c3c,
                    timestamp=_now()
                )
                embed.set_footer(text="Embot Community System")
                await self._dm_or_ping(message.author, embed)
                self.clog(f"Duplicate link from {message.author.display_name} — matches submission by {owner_name}.")
                return

        existing = self.db.find_existing(user_id, norm)
        is_new_version = False
        group_id: str
        version_str: str
        version_major: int
        version_minor: int
        dm_version_embed: Optional[discord.Embed] = None

        if existing:
            created = datetime.fromisoformat(existing["created_at"])
            age_days = (_now() - created.replace(tzinfo=timezone.utc)).days
            reenter_ok = age_days >= VERSION_REENTRY_DAYS

            group_id = existing["group_id"]
            version_str, version_major, version_minor = self._next_version(existing, content)
            is_new_version = True

            for sub in self.db.by_group(group_id):
                if sub["is_current"]:
                    self.db.update_submission(sub["id"], is_current=0)

            action = "re-entered the voting cycle as"if reenter_ok else "registered as"
            dm_version_embed = discord.Embed(
                title="New Version Detected",
                description=(
                    f"Your project **{title or 'Untitled'}** was {action} **{version_str}**.\n\n"
                    "Your previous vote history has been carried over, and voters who already "
                    "voted on an earlier version cannot vote again on this one."
                ),
                color=0x5865f2,
                timestamp=_now()
            )
            dm_version_embed.set_footer(text="Embot Community System")
            self.clog(f"New version {version_str} for submission {group_id} by {message.author.display_name}")
        else:
            group_id     = _short_id()
            version_str  = "v1.0"
            version_major = 1
            version_minor = 0
            linked_group: Optional[str] = None
            for h in attachment_hashes:
                linked_group = self.db.group_for_hash(h)
                if linked_group:
                    self.clog(
                        f"Submission by {message.author.display_name} linked to existing submission "
                        f"{linked_group} via file hash"
                    )
                    break
            if not linked_group:
                for link in links:
                    linked_group = self.db.group_for_link(link, user_id)
                    if linked_group:
                        self.clog(
                            f"Submission by {message.author.display_name} linked to existing submission "
                            f"{linked_group} via URL match"
                        )
                        break
            if linked_group and linked_group != group_id:
                group_id = linked_group

        sub_id = _short_id()
        sub_record = {
            "id":                 sub_id,
            "group_id":           group_id,
            "user_id":            user_id,
            "channel_id":         message.channel.id,
            "message_id":         message.id,
            "thread_id":          None,
            "title":              title,
            "content":            content,
            "content_normalized": norm,
            "file_hashes":        json.dumps(attachment_hashes),
            "links":              json.dumps(links),
            "version":            version_str,
            "version_major":      version_major,
            "version_minor":      version_minor,
            "is_deleted":         0,
            "is_current":         1,
            "created_at":         _now_str(),
            "updated_at":         _now_str(),
            "last_checked_at":    None,
        }
        self.db.add_submission(sub_record)

        for h in attachment_hashes:
            self.db.register_hash(h, sub_id, user_id)

        thread_name = (title or "Submission")[:100]
        try:
            thread = await message.create_thread(name=thread_name, auto_archive_duration=10080)
            self.db.update_submission(sub_id, thread_id=thread.id)
        except Exception as e:
            self.cerr("Failed to create submission thread", e)
            thread = None

        for emoji in SETUP_EMOJIS:
            try:
                await message.add_reaction(emoji)
            except Exception as e:
                self.cerr(f"Failed to add reaction {emoji}", e)

        if dm_version_embed:
            await self._dm_or_ping(message.author, dm_version_embed)

        log_embed = discord.Embed(
            title="New Submission",
            description=(
                f"**Author:** {message.author.mention}\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Title:** {title or 'Untitled'}\n"
                f"**Version:** {version_str}\n"
                f"**Group ID:** `{group_id}`\n"
                f"[Jump to Message]({message.jump_url})"
            ),
            color=0x2ecc71,
            timestamp=_now()
        )
        log_embed.set_footer(text=f"Submission ID: {sub_id}")
        await self._bot_log(guild, log_embed)
        self.clog(
            f"Submission registered: {title!r} by {message.author.display_name} "
            f"({version_str}, submission={group_id})"
        )

    async def handle_edit(self, payload: discord.RawMessageUpdateEvent):
        _embed_only_keys = {"embeds", "flags", "id", "channel_id", "guild_id"}
        if payload.data and set(payload.data.keys()) <= _embed_only_keys:
            return

        sub = self.db.by_message(payload.message_id)
        if not sub:
            return

        created = datetime.fromisoformat(sub["created_at"]).replace(tzinfo=timezone.utc)
        if (_now() - created).days >= VERSION_REENTRY_DAYS:
            return

        try:
            guild   = self.bot.get_guild(payload.guild_id)
            channel = guild.get_channel(payload.channel_id) if guild else None
            if not channel:
                return
            message = await channel.fetch_message(payload.message_id)
        except Exception as e:
            self.cerr("Could not fetch edited message", e)
            return

        new_content = message.content or ""
        new_links   = _extract_links(new_content)

        new_att_hashes: List[str] = []
        if message.attachments and HAS_AIOHTTP:
            async with aiohttp.ClientSession() as sess:
                for att in message.attachments:
                    h = await _hash_attachment(att, sess)
                    if h:
                        new_att_hashes.append(h)
                    else:
                        self.clog(
                            f"Edit: Could not hash attachment '{att.filename}' "
                            f"(submission {sub['id']}) — skipping.",
                            "WARNING"
                        )

        err = self._validate(message)
        if err:
            try:
                await message.delete()
            except Exception:
                pass
            self.db.update_submission(sub["id"], is_deleted=1, last_checked_at=_now_str())
            embed = self._invalid_embed(err, channel.name)
            member = guild.get_member(sub["user_id"])
            if member:
                await self._dm_or_ping(member, embed)
            self.clog(f"Edited submission {sub['id']} became invalid and was deleted.")
            return

        old_hashes = set(json.loads(sub["file_hashes"]))
        old_links  = set(json.loads(sub["links"]))
        changed = set(new_att_hashes) != old_hashes or set(new_links) != old_links

        new_ver = _parse_version(new_content)
        kwargs: dict = {
            "content":            new_content,
            "content_normalized": _normalize(new_content),
            "title":              _extract_title(new_content),
            "links":              json.dumps(new_links),
            "file_hashes":        json.dumps(new_att_hashes),
            "last_checked_at":    _now_str(),
        }

        if new_ver:
            maj, min_ = new_ver
            kwargs["version"]       = f"v{maj}.{min_}"
            kwargs["version_major"] = maj
            kwargs["version_minor"] = min_
            for old_sub in self.db.by_group(sub["group_id"]):
                if old_sub["is_current"] and old_sub["id"] != sub["id"]:
                    self.db.update_submission(old_sub["id"], is_current=0)
            try:
                member = guild.get_member(sub["user_id"])
                if member:
                    dm = discord.Embed(
                        title="Version Tag Detected in Edit",
                        description=(
                            f"Your submission **{_extract_title(new_content) or 'Untitled'}** "
                            f"has been updated to **v{maj}.{min_}** based on the version tag "
                            f"you added in your edit.\n\n"
                            "This version is now marked as the current one for your project group."
                        ),
                        color=0x5865f2,
                        timestamp=_now()
                    )
                    dm.set_footer(text="Embot Community System")
                    await self._dm_or_ping(member, dm)
            except Exception as e:
                self.cerr("Failed to DM version-edit notice", e)
            self.clog(
                f"Retroactive version tag in edit for submission {sub['id']} "
                f"→ v{maj}.{min_}"
            )
        elif changed:
            new_minor = sub["version_minor"] + 1
            kwargs["version"]       = f"v{sub['version_major']}.{new_minor}"
            kwargs["version_minor"] = new_minor

        self.db.update_submission(sub["id"], **kwargs)

        for h in new_att_hashes:
            if h not in old_hashes:
                self.db.register_hash(h, sub["id"], sub["user_id"])

        if changed:
            change_parts = []
            if set(new_att_hashes) != old_hashes:
                change_parts.append(
                    f"attachments {len(old_hashes)}→{len(new_att_hashes)}"
                )
            if set(new_links) != old_links:
                change_parts.append(
                    f"links {len(old_links)}→{len(new_links)}"
                )
            self.clog(
                f"Synced edit for submission {sub['id']} — "
                f"changed: {', '.join(change_parts) or 'content only'} "
                f"→ {kwargs.get('version', sub['version'])}"
            )
        else:
            self.clog(f"Synced edit for submission {sub['id']} (content updated)")

    async def handle_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        emoji = str(payload.emoji)
        if emoji not in VOTE_EMOJIS:
            return

        sub = self.db.by_message(payload.message_id)
        if not sub:
            return

        voter_id  = payload.user_id
        submitter = sub["user_id"]

        if voter_id == submitter:
            voter_name = _display_name(self.bot, payload.guild_id, voter_id)
            self.clog(
                f"Self-vote blocked: {voter_name} attempted to vote {emoji} "
                f"on their own submission ({sub['group_id']})",
                "WARNING"
            )
            try:
                guild   = self.bot.get_guild(payload.guild_id)
                channel = guild.get_channel(payload.channel_id)
                msg     = await channel.fetch_message(payload.message_id)
                user    = guild.get_member(voter_id)
                if user:
                    await msg.remove_reaction(payload.emoji, user)
            except Exception:
                pass
            return

        group_id  = sub["group_id"]
        xp_delta  = VOTE_EMOJIS[emoji]

        old_vote = self.db.upsert_vote(group_id, voter_id, emoji, xp_delta, payload.message_id)

        if old_vote:
            self.db.add_xp(submitter, -old_vote["xp_delta"])
            if old_vote["emoji"] != emoji:
                try:
                    guild   = self.bot.get_guild(payload.guild_id)
                    channel = guild.get_channel(payload.channel_id)
                    old_msg_id = old_vote["voted_message_id"]
                    old_msg_ch = guild.get_channel(sub["channel_id"])
                    old_msg    = await old_msg_ch.fetch_message(old_msg_id)
                    user       = guild.get_member(voter_id)
                    if user:
                        await old_msg.remove_reaction(old_vote["emoji"], user)
                except Exception:
                    pass

        self.db.add_xp(submitter, xp_delta)
        voter_name = _display_name(self.bot, payload.guild_id, voter_id)
        submitter_name = _display_name(self.bot, payload.guild_id, submitter)
        vote_action = "changed"if old_vote else "cast"
        change_detail = (
            f"(was {old_vote['emoji']} {old_vote['xp_delta']:+d} XP)"if old_vote else ""
        )
        self.clog(
            f"Vote {vote_action}: {emoji} ({xp_delta:+d} XP) by {voter_name} "
            f"on submission {group_id} — submitter {submitter_name}{change_detail}"
        )

    async def handle_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        emoji = str(payload.emoji)
        if emoji not in VOTE_EMOJIS:
            return

        sub = self.db.by_message(payload.message_id)
        if not sub:
            return

        group_id = sub["group_id"]
        old_vote = self.db.get_vote(group_id, payload.user_id)
        if not old_vote or old_vote["emoji"] != emoji:
            return

        self.db.remove_vote(group_id, payload.user_id)
        self.db.add_xp(sub["user_id"], -old_vote["xp_delta"])
        voter_name = _display_name(self.bot, payload.guild_id, payload.user_id)
        self.clog(
            f"Vote removed: {emoji} by {voter_name} "
            f"on submission {group_id}"
        )

    async def handle_delete(self, payload: discord.RawMessageDeleteEvent):
        sub = self.db.by_message(payload.message_id)
        if not sub:
            return

        self.db.update_submission(sub["id"], is_deleted=1)
        self.clog(
            f"Submission {sub['id']} marked deleted "
            f"(message {payload.message_id} removed from channel {payload.channel_id})"
        )

        if sub["is_current"]:
            remaining = [
                s for s in self.db.by_group(sub["group_id"])
                if not s["is_deleted"] and s["id"] != sub["id"]
            ]
            if remaining:
                latest = remaining[-1]
                self.db.update_submission(latest["id"], is_current=1)
                self.clog(
                    f"Promoted submission {latest['id']} (v{latest['version']}) "
                    f"as current for group {sub['group_id']} after deletion of previous current"
                )

    async def handle_thread_message(self, message: discord.Message):
        if message.author.bot:
            return
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return

        row = self.db.query_thread_submission(channel.id)
        if not row:
            return

        submitter_id = row["user_id"]
        if message.author.id == submitter_id:
            return

        if self.db.log_thread_xp(submitter_id, channel.id, message.id):
            self.db.add_xp(submitter_id, 0.1)

    async def check_submission_integrity(self, guild: discord.Guild):
        subs = self.db.get_checkable_submissions(limit=50)
        for sub in subs:
            channel = guild.get_channel(sub["channel_id"])
            if not channel:
                continue
            try:
                message = await channel.fetch_message(sub["message_id"])
            except (discord.NotFound, discord.Forbidden):
                if not sub["is_deleted"]:
                    self.db.update_submission(sub["id"], is_deleted=1)
                    self.clog(
                        f"Integrity: submission {sub['id']} message no longer exists — marked deleted.",
                        "WARNING"
                    )
                continue
            except Exception:
                continue

            if not sub["reactions_blocked"]:
                existing_emojis = {str(r.emoji) for r in message.reactions}
                for emoji in SETUP_EMOJIS:
                    if emoji not in existing_emojis:
                        try:
                            await message.add_reaction(emoji)
                            self.clog(
                                f"Integrity: restored missing reaction {emoji} on submission {sub['id']}.",
                                "WARNING"
                            )
                        except discord.Forbidden as e:
                            if getattr(e, 'code', None) == 90001:
                                self.db.update_submission(sub["id"], reactions_blocked=1)
                                self.clog(
                                    f"Integrity: reaction blocked on submission {sub['id']} (author blocked bot) — skipping future reaction checks.",
                                    "WARNING"
                                )
                                break
                            self.cerr(f"Integrity: failed to restore reaction {emoji} on {sub['id']}", e)
                        except Exception as e:
                            self.cerr(f"Integrity: failed to restore reaction {emoji} on {sub['id']}", e)

            if sub["thread_id"]:
                thread = guild.get_thread(sub["thread_id"])
                if thread is None:
                    try:
                        thread = await guild.fetch_channel(sub["thread_id"])
                    except Exception:
                        thread = None
                if thread is None:
                    self.clog(
                        f"Integrity: thread for submission {sub['id']} is missing or archived.",
                        "WARNING"
                    )

    async def run_spotlight(self, guild: discord.Guild):
        if self.db.spotlight_ran_this_week():
            return

        exclude_user = self.db.get_config("spotlight_exclude_user_id")
        top = self.db.top_submission_this_week(exclude_user=exclude_user)

        self.db.record_spotlight(
            top["group_id"] if top else None,
            top["submission_id"] if top else None
        )

        announcements = discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CHANNEL_NAME)
        if not announcements:
            self.clog("Spotlight: #announcements channel not found.", "WARNING")
            return

        if not top:
            self.clog("Spotlight Friday: no qualifying submission this week.")
            return

        member = guild.get_member(top["user_id"])
        name   = member.display_name if member else f"User {top['user_id']}"

        image_url = None
        if top["message_id"]:
            ch = guild.get_channel(top["channel_id"])
            if ch:
                try:
                    msg = await ch.fetch_message(top["message_id"])
                    for att in msg.attachments:
                        if att.content_type and att.content_type.startswith("image/"):
                            image_url = att.url
                            break
                except Exception:
                    pass

        links = json.loads(top["links"]) if top["links"] else []
        jump_url = f"https://discord.com/channels/{guild.id}/{top['channel_id']}/{top['message_id']}"
        link_line = f"[Download Link]({links[0]})" if links else f"[Jump to Submission]({jump_url})"

        xp_total = int(top["total_xp"])
        mention = member.mention if member else name

        body = f"## 🌟 Spotlight Friday\nThis week's featured submission is **{top['title'] or 'Untitled'}** by {mention}!\n\n**Version** • {top['version']}\n**XP Score** • {xp_total} XP\n\n**Original Post**\n{link_line}"
        if image_url:
            body += f"\n\n![]({image_url})"

        items = [discord.ui.Container(discord.ui.TextDisplay(body), accent_color=0xf1c40f)]

        items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        items.append(discord.ui.TextDisplay(f"-# Embot Spotlight Friday • {_now().strftime('%m/%d/%Y %-I:%M %p')}"))

        layout = discord.ui.LayoutView(timeout=None)
        for item in items:
            layout.add_item(item)

        await announcements.send(view=layout)
        self.clog(
            f"Spotlight Friday posted: '{top['title']}' by user {top['user_id']} "
            f"({int(top['total_xp'])} XP)"
        )

def setup(bot):

    from mod_core import is_owner

    cs = CommunitySystem(bot)
    bot._community_system = cs

    def _is_spotlight_time() -> bool:
        now = _now()
        if CST:
            local = now.astimezone(CST)
        else:
            try:
                from zoneinfo import ZoneInfo
                local = now.astimezone(ZoneInfo("America/Chicago"))
            except Exception:
                local = now - timedelta(hours=6)
        return local.weekday() == 4 and local.hour == 15

    @bot.listen()
    async def on_message(message: discord.Message):
        if not message.guild or message.author.bot:
            return

        cs._refresh_channel_ids(message.guild)

        if cs._is_submission_channel(message.channel.id):
            await cs.handle_submission(message)
            return

        await cs.handle_thread_message(message)

    @bot.listen()
    async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
        if not payload.guild_id:
            return
        await cs.handle_edit(payload)

    @bot.listen()
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        await cs.handle_reaction_add(payload)

    @bot.listen()
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        await cs.handle_reaction_remove(payload)

    @bot.listen()
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        await cs.handle_delete(payload)

    async def _run_spotlight_if_due():
        try:
            if not _is_spotlight_time():
                return
            if cs.db.spotlight_ran_this_week():
                return
            for guild in bot.guilds:
                await cs.run_spotlight(guild)
        except Exception as e:
            cs.cerr("Spotlight task error", e)

    async def _spotlight_loop():
        await bot.wait_until_ready()
        while not bot.is_closed():
            now = _now()
            if CST:
                local = now.astimezone(CST)
            else:
                try:
                    from zoneinfo import ZoneInfo
                    local = now.astimezone(ZoneInfo("America/Chicago"))
                except Exception:
                    local = now - timedelta(hours=6)
            days_until_friday = (4 - local.weekday()) % 7
            target = local.replace(hour=15, minute=0, second=0, microsecond=0)
            if days_until_friday == 0 and local.hour >= 15:
                await _run_spotlight_if_due()
                target += timedelta(days=7)
            elif days_until_friday > 0:
                target += timedelta(days=days_until_friday)
            else:
                target += timedelta(days=7)
            wait_seconds = (target - local).total_seconds()
            await asyncio.sleep(min(wait_seconds, 3600))

    asyncio.create_task(_spotlight_loop())

    @tasks.loop(minutes=10)
    async def integrity_task():
        try:
            for guild in bot.guilds:
                await cs.check_submission_integrity(guild)
        except Exception as e:
            cs.cerr("Integrity check error", e)

    @integrity_task.before_loop
    async def before_integrity():
        await bot.wait_until_ready()

    integrity_task.start()

    @bot.tree.command(name="community_setup", description="[Owner only] Configure community submission channels")
    @app_commands.describe(
        projects_channel="The #projects channel",
        artwork_channel="The #artwork channel",
        announcements_channel="The #announcements channel",
        spotlight_exclude_user="User ID to exclude from Spotlight Friday (server owner)",
    )
    async def community_setup(
        interaction: discord.Interaction,
        projects_channel: Optional[discord.TextChannel] = None,
        artwork_channel: Optional[discord.TextChannel] = None,
        announcements_channel: Optional[discord.TextChannel] = None,
        spotlight_exclude_user: Optional[str] = None,
    ):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        changed = []
        if projects_channel:
            names = cs._get_submission_channel_names()
            if projects_channel.name not in names:
                names.append(projects_channel.name)
            cs.db.set_config("submission_channels", names)
            changed.append(f"Projects: {projects_channel.mention}")

        if artwork_channel:
            cs.db.set_config("artwork_channel_id", artwork_channel.id)
            changed.append(f"Artwork: {artwork_channel.mention}")

        if announcements_channel:
            cs.db.set_config("announcements_channel_id", announcements_channel.id)
            changed.append(f"Announcements: {announcements_channel.mention}")

        if spotlight_exclude_user:
            try:
                uid = int(spotlight_exclude_user)
                cs.db.set_config("spotlight_exclude_user_id", uid)
                changed.append(f"Spotlight excluded user ID: `{uid}`")
            except ValueError:
                await interaction.response.send_message("Invalid user ID.", ephemeral=True)
                return

        cs._refresh_channel_ids(interaction.guild)

        embed = discord.Embed(
            title="Community Configuration Updated",
            description="\n".join(changed) if changed else "No changes made.",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        cs.clog(f"Community config updated by {interaction.user}")

    @bot.tree.command(name="xp", description="Check your XP or another user's XP")
    @app_commands.describe(member="Member to check (leave blank for yourself)")
    async def xp_command(interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        xp_val = cs.db.get_xp(target.id)
        embed = discord.Embed(
            title=f"⭐ XP — {target.display_name}",
            description=f"**{xp_val:.1f} XP**",
            color=0xf1c40f,
            timestamp=_now()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="leaderboard", description="Show the community XP leaderboard")
    async def leaderboard_command(interaction: discord.Interaction):
        rows = cs.db.get_leaderboard(limit=10)
        if not rows:
            await interaction.response.send_message("No XP data yet!", ephemeral=True)
            return

        embed = discord.Embed(
            title="Community XP Leaderboard",
            color=0xf1c40f,
            timestamp=_now()
        )
        guild = interaction.guild
        lines = []
        medals = ["", "", ""]
        for i, row in enumerate(rows):
            member = guild.get_member(row["user_id"])
            name   = member.display_name if member else f"User {row['user_id']}"
            prefix = medals[i] if i < 3 else f"**{i+1}.**"
            lines.append(f"{prefix} {name} — **{float(row['xp']):.1f} XP**")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Embot Community System")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="submission_info", description="Look up a submission by its Discord message link or ID")
    @app_commands.describe(message_id="The ID of the submission message")
    async def submission_info(interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True)
            return

        sub = cs.db.by_message(mid)
        if not sub:
            await interaction.response.send_message("No submission found for that message ID.", ephemeral=True)
            return

        versions = cs.db.by_group(sub["group_id"])
        member   = interaction.guild.get_member(sub["user_id"])
        name     = member.display_name if member else f"User {sub['user_id']}"

        with cs.db._conn() as c:
            xp_row = c.execute(
                "SELECT COALESCE(SUM(xp_delta),0) AS total FROM votes WHERE group_id=?",
                (sub["group_id"],)
            ).fetchone()
        total_xp = int(xp_row["total"]) if xp_row else 0

        embed = discord.Embed(
            title=f"Submission: {sub['title'] or 'Untitled'}",
            color=0x5865f2,
            timestamp=_now()
        )
        embed.add_field(name="Author",    value=member.mention if member else name, inline=True)
        embed.add_field(name="Version",   value=sub["version"],                     inline=True)
        embed.add_field(name="Group XP",  value=f"{total_xp} XP",                  inline=True)
        embed.add_field(name="Versions",  value=str(len(versions)),                 inline=True)
        embed.add_field(name="Group ID",  value=f"`{sub['group_id']}`",             inline=False)
        embed.set_footer(text=f"Submission ID: {sub['id']}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="spotlight_preview", description="[Owner only] Preview this week's Spotlight Friday winner")
    async def spotlight_preview(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            exclude = cs.db.get_config("spotlight_exclude_user_id")
            top = cs.db.top_submission_this_week(exclude_user=exclude)
            if not top:
                await interaction.followup.send("No qualifying submission this week (all scores are zero or negative).", ephemeral=True)
                return
            member = interaction.guild.get_member(top["user_id"])
            name   = member.display_name if member else f"User {top['user_id']}"

            image_url = None
            if top["message_id"]:
                ch = interaction.guild.get_channel(top["channel_id"])
                if ch:
                    try:
                        msg = await ch.fetch_message(top["message_id"])
                        for att in msg.attachments:
                            if att.content_type and att.content_type.startswith("image/"):
                                image_url = att.url
                                break
                    except Exception:
                        pass

            links = json.loads(top["links"]) if top["links"] else []
            jump_url = f"https://discord.com/channels/{interaction.guild.id}/{top['channel_id']}/{top['message_id']}"
            link_line = f"[Download Link]({links[0]})" if links else f"[Jump to Submission]({jump_url})"
            mention = member.mention if member else name
            xp_total = int(top["total_xp"])

            body = f"## 🌟 Spotlight Friday\nThis week's featured submission is **{top['title'] or 'Untitled'}** by {mention}!\n\n**Version** • {top['version']}\n**XP Score** • {xp_total} XP\n\n**Original Post**\n{link_line}"
            if image_url:
                body += f"\n\n![]({image_url})"

            items = [discord.ui.Container(discord.ui.TextDisplay(body), accent_color=0xf1c40f)]
            items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            items.append(discord.ui.TextDisplay(f"-# Embot Spotlight Friday • {_now().strftime('%m/%d/%Y %-I:%M %p')}"))

            layout = discord.ui.LayoutView(timeout=None)
            for item in items:
                layout.add_item(item)

            await interaction.followup.send(view=layout, ephemeral=True)
        except Exception as e:
            cs.cerr("spotlight_preview error", e)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @bot.tree.command(name="spotlight_run", description="[Owner only] Force-run Spotlight Friday now")
    async def spotlight_run(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("This command is restricted to owners.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await cs.run_spotlight(interaction.guild)
        await interaction.followup.send("Spotlight task executed.", ephemeral=True)

    cs.clog("Community module setup complete")
