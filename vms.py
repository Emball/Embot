# [file name]: vms.py
"""
VMS — Voice Message System for Embot
=====================================
• Detects & saves Discord voice messages to data/vms/
• Transcribes using OpenAI Whisper (CUDA if available, else CPU)
• Posts transcripts as blockquote replies
• Saves transcripts to SQLite DB (data/vms.db) for keyword playback
• Archives to data/cache/archive/ after 150 days, deletes after 365 days
• Archive job schedule is stored in DB — catches missed runs after crashes
• Periodic random playback in #general (every 40–80 messages, 50% chance)
• Contextual playback using keyword matching against transcripts
• Smart selection: 7-day cooldowns, long-VM penalties, recency weighting
• Responds to @mentions / replies with a random VM (10s cooldown)
• Retroactively transcribes manually placed .ogg files on startup
• Processing queue prevents memory issues
• Works as user app (transcribes VMs in DMs)
"""

import asyncio
import aiohttp
import discord
import os
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from discord.ext import tasks

MODULE_NAME = "VMS"

# ==================== CONFIGURATION ====================

GENERAL_CHANNEL_NAME = "general"
PING_COOLDOWN_SECONDS = 10
VM_COOLDOWN_DAYS = 7
LONG_VM_THRESHOLD_SECS = 60          # VMs longer than this receive a score penalty
ARCHIVE_AFTER_DAYS = 150
DELETE_AFTER_DAYS = 365
ARCHIVE_JOB_INTERVAL_HOURS = 24
RANDOM_PLAYBACK_MIN = 40
RANDOM_PLAYBACK_MAX = 80
PLAYBACK_CHANCE = 0.50               # 50% chance to trigger playback when threshold reached
WHISPER_MODEL_SIZE = "base"          # tiny / base / small / medium / large

# Waveform preview (from Discord spec example)
SAMPLE_WAVEFORM = (
    "acU6Va9UcSVZzsVw7IU/80s0Kh/pbrTcwmpR9da4mvQejIMykkgo9F2FfeCd235K/"
    "atHZtSAmxKeTUgKxAdNVO8PAoZq1cHNQXT/PHthL2sfPZGSdxNgLH0AuJwVeI7QZJ02"
    "ke40+HkUcBoDdqGDZeUvPqoIRbE23Kr+sexYYe4dVq+zyCe3ci/6zkMWbVBpCjq8D8Z"
    "ZEFo/lmPJTkgjwqnqHuf6XT4mJyLNphQjvFH9aRqIZpPoQz1sGwAY2vssQ5mTy5J5mu"
    "Go+n82b0xFROZwsJpumDsFi4Da/85uWS/YzjY5BdxGac8rgUqm9IKh7E6GHzOGOy0LQ"
    "Iz3O4ntTg=="
)

# Common words to filter out before keyword matching
STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be',
    'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'on',
    'at', 'by', 'for', 'with', 'about', 'into', 'through', 'during', 'before',
    'after', 'above', 'below', 'from', 'up', 'down', 'out', 'off', 'over',
    'under', 'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how',
    'all', 'each', 'every', 'both', 'few', 'more', 'most', 'other', 'some',
    'such', 'no', 'not', 'only', 'own', 'same', 'than', 'too', 'very', 's',
    'just', 'because', 'as', 'until', 'while', 'i', 'me', 'my', 'myself', 'we',
    'our', 'ours', 'you', 'your', 'yours', 'he', 'him', 'his', 'she', 'her',
    'hers', 'it', 'its', 'they', 'them', 'their', 'theirs', 'what', 'which',
    'who', 'whom', 'this', 'that', 'these', 'those', 'so', 'if', 'like', 'get',
    'got', 'im', 'yeah', 'okay', 'ok', 'also', 'lol', 'um', 'uh', 'oh', 'yes',
}

# ==================== PATH HELPERS ====================

def _script_dir() -> Path:
    return Path(__file__).parent.absolute()

def _db_path() -> str:
    p = _script_dir() / "data"
    p.mkdir(exist_ok=True)
    return str(p / "vms.db")

def _vms_dir() -> Path:
    p = _script_dir() / "data" / "vms"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _archive_dir() -> Path:
    p = _script_dir() / "data" / "cache" / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p

# ==================== DATABASE SETUP ====================

DB_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS vms (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT    NOT NULL UNIQUE,
    filepath            TEXT    NOT NULL,
    discord_message_id  TEXT,
    discord_channel_id  TEXT,
    guild_id            TEXT,
    duration_secs       REAL    DEFAULT 0.0,
    transcript          TEXT,
    processed           INTEGER DEFAULT 0,  -- 0=pending, 1=done, 2=archived, 3=deleted
    created_at          INTEGER NOT NULL,
    archived_at         INTEGER,
    deleted_at          INTEGER
);

CREATE TABLE IF NOT EXISTS vms_playback (
    vm_id       INTEGER PRIMARY KEY,
    last_played INTEGER DEFAULT 0,
    play_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vms_scheduled_jobs (
    job_name    TEXT    PRIMARY KEY,
    last_run    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vms_startup_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    startup_time INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vms_message_counter (
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    count       INTEGER DEFAULT 0,
    threshold   INTEGER DEFAULT 60,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS vms_ping_cooldown (
    user_id     TEXT    PRIMARY KEY,
    last_ping   INTEGER DEFAULT 0
);
"""


def _init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()

# ==================== WHISPER (lazy-loaded, thread-safe) ====================

_whisper_model = None
_whisper_lock = threading.Lock()
_whisper_device = "cpu"


def _load_whisper() -> Optional[object]:
    """Load OpenAI Whisper once. Prefer CUDA GPU, fall back to CPU."""
    global _whisper_model, _whisper_device
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            import whisper
            try:
                import torch
                _whisper_device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                _whisper_device = "cpu"
            print(f"[{MODULE_NAME}] Loading Whisper '{WHISPER_MODEL_SIZE}' on {_whisper_device}...")
            _whisper_model = whisper.load_model(WHISPER_MODEL_SIZE, device=_whisper_device)
            print(f"[{MODULE_NAME}] Whisper loaded on {_whisper_device}")
        except ImportError:
            print(f"[{MODULE_NAME}] openai-whisper not installed — transcription disabled")
        except Exception as exc:
            print(f"[{MODULE_NAME}] Whisper load error: {exc}")
    return _whisper_model


def _transcribe_sync(filepath: str) -> Tuple[Optional[str], float]:
    """
    Blocking transcription call (intended to run in an executor).
    Returns (transcript_text, duration_seconds).
    """
    model = _load_whisper()
    if model is None:
        return None, _get_ogg_duration(filepath)

    try:
        result = model.transcribe(filepath, fp16=(_whisper_device == "cuda"))
        text = (result.get("text") or "").strip()
    except Exception as exc:
        print(f"[{MODULE_NAME}] Transcription error for {filepath}: {exc}")
        text = None

    duration = _get_ogg_duration(filepath)
    return text, duration


def _get_ogg_duration(filepath: str) -> float:
    """Best-effort OGG duration extraction via mutagen."""
    try:
        from mutagen.oggopus import OggOpus
        return OggOpus(filepath).info.length
    except Exception:
        pass
    try:
        from mutagen import File
        audio = File(filepath)
        if audio and hasattr(audio.info, 'length'):
            return audio.info.length
    except Exception:
        pass
    return 0.0

# ==================== DISCORD VOICE MESSAGE API ====================

async def _send_voice_message(
    bot_token: str,
    channel_id: int,
    ogg_path: str,
    duration_secs: float,
    session: aiohttp.ClientSession,
) -> Optional[dict]:
    """
    Upload an OGG file and post it as a Discord voice message (flags: 8192).
    Returns the raw message dict on success, None on failure.

    Protocol:
      1. POST /channels/{id}/attachments  → get upload_url + upload_filename
      2. PUT  upload_url                  → upload raw OGG bytes to CDN
      3. POST /channels/{id}/messages     → send voice message referencing upload_filename
    """
    ogg_bytes = Path(ogg_path).read_bytes()
    file_size = len(ogg_bytes)

    headers_json = {
        "Content-Type": "application/json",
        "Authorization": f"Bot {bot_token}",
    }

    # ── Step 1: Request upload URL ──
    try:
        async with session.post(
            f"https://discord.com/api/v10/channels/{channel_id}/attachments",
            headers=headers_json,
            json={"files": [{"filename": "voice-message.ogg", "file_size": file_size, "id": "2"}]},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[{MODULE_NAME}] Upload-URL request failed ({resp.status}): {body[:200]}")
                return None
            data = await resp.json()
    except Exception as exc:
        print(f"[{MODULE_NAME}] Upload-URL request error: {exc}")
        return None

    attachment = data["attachments"][0]
    upload_url: str = attachment["upload_url"]
    upload_filename: str = attachment["upload_filename"]

    # ── Step 2: Upload file bytes to CDN ──
    try:
        async with session.put(
            upload_url,
            headers={"Content-Type": "audio/ogg", "Authorization": f"Bot {bot_token}"},
            data=ogg_bytes,
        ) as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                print(f"[{MODULE_NAME}] CDN upload failed ({resp.status}): {body[:200]}")
                return None
    except Exception as exc:
        print(f"[{MODULE_NAME}] CDN upload error: {exc}")
        return None

    # ── Step 3: Post voice message ──
    payload = {
        "flags": 8192,
        "attachments": [{
            "id": "0",
            "filename": "voice-message.ogg",
            "uploaded_filename": upload_filename,
            "duration_secs": max(duration_secs, 1.0),
            "waveform": SAMPLE_WAVEFORM,
        }],
    }
    try:
        async with session.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers_json,
            json=payload,
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            body = await resp.text()
            print(f"[{MODULE_NAME}] Message send failed ({resp.status}): {body[:200]}")
            return None
    except Exception as exc:
        print(f"[{MODULE_NAME}] Message send error: {exc}")
        return None


# ==================== VMS MANAGER ====================

class VMSManager:
    """
    Central manager for the VMS (Voice Message System) module.
    Handles storage, transcription queuing, archiving, and playback.
    """

    def __init__(self, bot):
        self.bot = bot
        self.db_path = _db_path()
        self.vms_dir = _vms_dir()
        self.archive_dir = _archive_dir()
        self._token: str = os.getenv("DISCORD_BOT_TOKEN", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vms_worker")
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: Optional[asyncio.Task] = None

        _init_db(self.db_path)

        # Log this startup so we can detect missed scheduled jobs
        self._db_exec(
            "INSERT INTO vms_startup_log (startup_time) VALUES (?)",
            (int(time.time()),)
        )
        self.bot.logger.log(MODULE_NAME, "VMSManager initialised")

    # ------------------------------------------------------------------ DB --

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _db_exec(self, query: str, params: tuple = ()):
        conn = self._conn()
        try:
            conn.execute(query, params)
            conn.commit()
        finally:
            conn.close()

    def _db_one(self, query: str, params: tuple = ()) -> Optional[tuple]:
        conn = self._conn()
        try:
            return conn.execute(query, params).fetchone()
        finally:
            conn.close()

    def _db_all(self, query: str, params: tuple = ()) -> List[tuple]:
        conn = self._conn()
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    # -------------------------------------------------------------- Session --

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # -------------------------------------------------- Transcription Queue --

    async def _start_queue_worker(self):
        self._queue_task = asyncio.create_task(self._queue_worker())
        self.bot.logger.log(MODULE_NAME, "Transcription queue worker started")

    async def _queue_worker(self):
        """
        Processes transcription jobs one at a time from the asyncio queue,
        preventing simultaneous Whisper runs that could OOM.
        """
        while True:
            try:
                vm_id, filepath, message, reply_to = await self.queue.get()

                self.bot.logger.log(MODULE_NAME,
                    f"Transcribing VM #{vm_id}: {Path(filepath).name}")
                try:
                    transcript, duration = await asyncio.get_event_loop().run_in_executor(
                        self._executor, _transcribe_sync, filepath
                    )

                    self._db_exec(
                        "UPDATE vms SET transcript=?, duration_secs=?, processed=1 WHERE id=?",
                        (transcript or "", duration, vm_id)
                    )
                    # Ensure playback row exists
                    self._db_exec(
                        "INSERT OR IGNORE INTO vms_playback (vm_id) VALUES (?)",
                        (vm_id,)
                    )

                    preview = (transcript or "")[:80]
                    self.bot.logger.log(MODULE_NAME,
                        f"VM #{vm_id} transcribed ({duration:.1f}s): {preview!r}")

                    # Post blockquote reply if we have context
                    if reply_to is not None and transcript:
                        try:
                            await reply_to.reply(f"> {transcript}")
                        except Exception as exc:
                            self.bot.logger.log(MODULE_NAME,
                                f"Failed to post transcript for VM #{vm_id}: {exc}", "WARNING")

                except Exception as exc:
                    self.bot.logger.error(MODULE_NAME,
                        f"Queue worker error on VM #{vm_id}", exc)
                finally:
                    self.queue.task_done()

            except asyncio.CancelledError:
                self.bot.logger.log(MODULE_NAME, "Queue worker cancelled")
                break
            except Exception as exc:
                self.bot.logger.error(MODULE_NAME, "Unexpected queue worker error", exc)
                await asyncio.sleep(1)

    async def enqueue(self, vm_id: int, filepath: str,
                      reply_to: Optional[discord.Message] = None):
        """Add a VM to the transcription queue."""
        await self.queue.put((vm_id, filepath, None, reply_to))
        self.bot.logger.log(MODULE_NAME, f"VM #{vm_id} queued for transcription")

    # ------------------------------------------------------- Save Voice Msg --

    async def save_voice_message(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> Optional[int]:
        """
        Download and persist a Discord voice message attachment.
        Stores discord_message_id and discord_channel_id in the DB.
        Returns the DB row id, or None on failure.
        """
        try:
            raw = await attachment.read()
            ts = int(time.time())
            filename = f"vm_{ts}_{message.id}.ogg"
            filepath = self.vms_dir / filename
            filepath.write_bytes(raw)

            duration = await asyncio.get_event_loop().run_in_executor(
                self._executor, _get_ogg_duration, str(filepath)
            )

            guild_id = str(message.guild.id) if message.guild else None

            self._db_exec(
                """INSERT OR IGNORE INTO vms
                   (filename, filepath, discord_message_id, discord_channel_id,
                    guild_id, duration_secs, processed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (filename, str(filepath), str(message.id),
                 str(message.channel.id), guild_id, duration, ts)
            )

            row = self._db_one("SELECT id FROM vms WHERE filename=?", (filename,))
            if row:
                self.bot.logger.log(MODULE_NAME,
                    f"Saved VM #{row[0]}: {filename} (guild={guild_id})")
                return row[0]

        except Exception as exc:
            self.bot.logger.error(MODULE_NAME, "Failed to save voice message", exc)
        return None

    # ----------------------------------------------- Retroactive Processing --

    async def process_unprocessed(self):
        """
        Scan data/vms/ for .ogg files not yet in the DB (manually placed files),
        register them using their filesystem mtime, then queue all pending VMs.
        """
        self.bot.logger.log(MODULE_NAME, "Scanning data/vms/ for unregistered files…")
        new = 0
        for ogg in self.vms_dir.glob("*.ogg"):
            if not self._db_one("SELECT id FROM vms WHERE filename=?", (ogg.name,)):
                mtime = int(ogg.stat().st_mtime)
                dur = await asyncio.get_event_loop().run_in_executor(
                    self._executor, _get_ogg_duration, str(ogg)
                )
                self._db_exec(
                    """INSERT OR IGNORE INTO vms (filename, filepath, processed, created_at, duration_secs)
                       VALUES (?, ?, 0, ?, ?)""",
                    (ogg.name, str(ogg), mtime, dur)
                )
                new += 1
                self.bot.logger.log(MODULE_NAME, f"Registered manual VM: {ogg.name}")

        if new:
            self.bot.logger.log(MODULE_NAME, f"Registered {new} manually-placed VM(s)")

        # Queue all unprocessed rows
        pending = self._db_all(
            "SELECT id, filepath FROM vms WHERE processed=0 AND filepath IS NOT NULL"
        )
        queued = 0
        for vm_id, fp in pending:
            if Path(fp).exists():
                await self.enqueue(vm_id, fp)
                queued += 1
            else:
                self.bot.logger.log(MODULE_NAME,
                    f"VM #{vm_id} file missing: {fp}", "WARNING")

        if queued:
            self.bot.logger.log(MODULE_NAME,
                f"Queued {queued} unprocessed VM(s) for transcription")

    # ---------------------------------------------------------- Archive Job --

    async def run_archive_if_due(self):
        """
        Check DB timestamp for when the archive job last ran.
        If overdue (e.g. bot crashed), run it immediately then reschedule normally.
        """
        row = self._db_one(
            "SELECT last_run FROM vms_scheduled_jobs WHERE job_name='archive'"
        )
        last_run = row[0] if row else 0
        now = int(time.time())
        due_after = last_run + (ARCHIVE_JOB_INTERVAL_HOURS * 3600)

        if now >= due_after:
            missed = (now - due_after) // 3600
            if missed > 0:
                self.bot.logger.log(MODULE_NAME,
                    f"Archive job missed by ~{missed}h — running now (crash recovery)")
            await self._do_archive()
        else:
            next_dt = datetime.fromtimestamp(due_after).strftime("%Y-%m-%d %H:%M")
            self.bot.logger.log(MODULE_NAME, f"Archive job not due yet (next: {next_dt})")

    async def _do_archive(self):
        """Move VMs ≥150 days old → archive dir; delete VMs ≥365 days old."""
        now = int(time.time())
        archive_cutoff = now - (ARCHIVE_AFTER_DAYS * 86400)
        delete_cutoff  = now - (DELETE_AFTER_DAYS  * 86400)
        archived = deleted = 0

        # ── Delete (≥365 days) ──
        for vm_id, fp, fn in self._db_all(
            "SELECT id, filepath, filename FROM vms WHERE created_at < ? AND processed != 3",
            (delete_cutoff,)
        ):
            try:
                for path in [Path(fp), self.archive_dir / fn]:
                    if path.exists():
                        path.unlink()
                self._db_exec(
                    "UPDATE vms SET processed=3, deleted_at=? WHERE id=?",
                    (now, vm_id)
                )
                deleted += 1
            except Exception as exc:
                self.bot.logger.error(MODULE_NAME, f"Failed to delete VM #{vm_id}", exc)

        # ── Archive (150–365 days) ──
        for vm_id, fp, fn in self._db_all(
            """SELECT id, filepath, filename FROM vms
               WHERE created_at < ? AND created_at >= ? AND processed=1""",
            (archive_cutoff, delete_cutoff)
        ):
            try:
                src = Path(fp)
                dst = self.archive_dir / fn
                if src.exists() and not dst.exists():
                    src.rename(dst)
                self._db_exec(
                    "UPDATE vms SET processed=2, filepath=?, archived_at=? WHERE id=?",
                    (str(dst), now, vm_id)
                )
                archived += 1
            except Exception as exc:
                self.bot.logger.error(MODULE_NAME, f"Failed to archive VM #{vm_id}", exc)

        self._db_exec(
            "INSERT OR REPLACE INTO vms_scheduled_jobs (job_name, last_run) VALUES ('archive', ?)",
            (now,)
        )
        self.bot.logger.log(MODULE_NAME,
            f"Archive job complete — archived: {archived}, deleted: {deleted}")

    # ------------------------------------------------------------ Playback --

    @staticmethod
    def _keywords(text: str) -> set:
        """Extract meaningful keywords, stripping stop words."""
        words = re.findall(r"\b[a-z]{3,}\b", text.lower())
        return {w for w in words if w not in STOP_WORDS}

    def _eligible_vms(self):
        """
        Return rows for VMs that are transcribed, have an accessible file,
        and are outside their 7-day cooldown.
        Columns: id, filepath, transcript, duration_secs, created_at, last_played
        """
        cutoff = int(time.time()) - (VM_COOLDOWN_DAYS * 86400)
        rows = self._db_all(
            """SELECT v.id, v.filepath, v.transcript, v.duration_secs, v.created_at,
                      COALESCE(p.last_played, 0)
               FROM vms v
               LEFT JOIN vms_playback p ON v.id = p.vm_id
               WHERE v.processed = 1
                 AND v.transcript IS NOT NULL
                 AND v.transcript != ''
                 AND COALESCE(p.last_played, 0) < ?""",
            (cutoff,)
        )
        return [(r[0], r[1], r[2], r[3], r[4], r[5])
                for r in rows if Path(r[1]).exists()]

    def select_contextual(self, recent_messages: List[str]) -> Optional[Tuple[int, str, float]]:
        """
        Score eligible VMs against recent chat keywords.
        Returns (vm_id, filepath, duration_secs) for the best match, or None.
        """
        chat_kw = self._keywords(" ".join(recent_messages))
        if not chat_kw:
            return None

        now = int(time.time())
        scored = []
        for vm_id, fp, transcript, duration, created_at, _ in self._eligible_vms():
            vm_kw = self._keywords(transcript)
            overlap = len(chat_kw & vm_kw)
            if overlap == 0:
                continue

            score = overlap * 10.0
            # Recency bonus (up to +3 for VMs <30 days old)
            age_days = (now - created_at) / 86400
            score += max(0.0, 30.0 - age_days) * 0.1
            # Penalty for long VMs
            if duration > LONG_VM_THRESHOLD_SECS:
                score -= (duration - LONG_VM_THRESHOLD_SECS) * 0.1

            scored.append((score, vm_id, fp, duration))

        if not scored:
            return None
        scored.sort(reverse=True)
        _, vm_id, fp, dur = scored[0]
        return vm_id, fp, dur

    def select_random(self) -> Optional[Tuple[int, str, float]]:
        """
        Weighted-random selection from eligible VMs.
        Shorter VMs are preferred; long VMs receive a weight penalty.
        Returns (vm_id, filepath, duration_secs) or None.
        """
        candidates = []
        for vm_id, fp, _, duration, *_ in self._eligible_vms():
            w = max(10, int(100 - max(0, duration - LONG_VM_THRESHOLD_SECS) * 0.5))
            candidates.append((w, vm_id, fp, duration))

        if not candidates:
            return None

        total = sum(w for w, *_ in candidates)
        pick  = random.uniform(0, total)
        cumulative = 0
        for w, vm_id, fp, dur in candidates:
            cumulative += w
            if pick <= cumulative:
                return vm_id, fp, dur
        _, vm_id, fp, dur = candidates[-1]
        return vm_id, fp, dur

    async def mark_played(self, vm_id: int):
        """Record that a VM was just played."""
        self._db_exec(
            """INSERT INTO vms_playback (vm_id, last_played, play_count) VALUES (?, ?, 1)
               ON CONFLICT(vm_id) DO UPDATE SET
                 last_played = excluded.last_played,
                 play_count  = play_count + 1""",
            (vm_id, int(time.time()))
        )

    async def send_vm(self, channel, vm_id: int, filepath: str, duration: float) -> bool:
        """
        Send a VM to a Discord channel (or DM) as a proper voice message.
        Uses the manual Discord API upload flow.
        """
        try:
            session = await self._get_session()
            result = await _send_voice_message(
                self._token, channel.id, filepath, duration, session
            )
            if result:
                await self.mark_played(vm_id)
                self.bot.logger.log(MODULE_NAME,
                    f"Sent VM #{vm_id} to channel {channel.id}")
                return True
            self.bot.logger.log(MODULE_NAME,
                f"Failed to send VM #{vm_id}", "WARNING")
            return False
        except Exception as exc:
            self.bot.logger.error(MODULE_NAME, f"send_vm error for #{vm_id}", exc)
            return False

    # -------------------------------------------------- Message Counter --

    def _get_counter(self, guild_id: str, channel_id: str) -> Tuple[int, int]:
        row = self._db_one(
            "SELECT count, threshold FROM vms_message_counter WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        )
        return (row[0], row[1]) if row else (0, random.randint(RANDOM_PLAYBACK_MIN, RANDOM_PLAYBACK_MAX))

    def _inc_counter(self, guild_id: str, channel_id: str) -> Tuple[int, int]:
        count, threshold = self._get_counter(guild_id, channel_id)
        count += 1
        self._db_exec(
            """INSERT INTO vms_message_counter (guild_id, channel_id, count, threshold)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, channel_id) DO UPDATE SET count=excluded.count""",
            (guild_id, channel_id, count, threshold)
        )
        return count, threshold

    def _reset_counter(self, guild_id: str, channel_id: str):
        new_thresh = random.randint(RANDOM_PLAYBACK_MIN, RANDOM_PLAYBACK_MAX)
        self._db_exec(
            """INSERT INTO vms_message_counter (guild_id, channel_id, count, threshold)
               VALUES (?, ?, 0, ?)
               ON CONFLICT(guild_id, channel_id) DO UPDATE SET count=0, threshold=excluded.threshold""",
            (guild_id, channel_id, new_thresh)
        )

    # -------------------------------------------------- Ping Cooldown --

    def ping_allowed(self, user_id: str) -> bool:
        row = self._db_one(
            "SELECT last_ping FROM vms_ping_cooldown WHERE user_id=?", (user_id,)
        )
        if not row:
            return True
        return (int(time.time()) - row[0]) >= PING_COOLDOWN_SECONDS

    def set_ping_cooldown(self, user_id: str):
        self._db_exec(
            """INSERT INTO vms_ping_cooldown (user_id, last_ping) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_ping=excluded.last_ping""",
            (user_id, int(time.time()))
        )

    # -------------------------------------------------- Context Helper --

    def recent_messages(self, guild_id: str, channel_id: str, limit: int = 20) -> List[str]:
        """Pull recent message text from the moderation module's message cache."""
        try:
            mod = getattr(self.bot, '_mod_system', None)
            if mod and hasattr(mod, 'message_cache'):
                msgs = mod.message_cache.get(guild_id, {}).get(channel_id, [])
                return [m.get('content', '') for m in msgs[-limit:] if m.get('content')]
        except Exception:
            pass
        return []

    # ---------------------------------------------------------------- Startup --

    async def startup(self):
        """Full startup sequence: queue worker → retroactive scan → archive check."""
        await self._start_queue_worker()
        await asyncio.sleep(0.5)                 # let event loop breathe
        await self.process_unprocessed()
        await self.run_archive_if_due()
        self.bot.logger.log(MODULE_NAME, "VMS startup complete")


# ==================== MODULE SETUP ====================

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up VMS module")

    manager = VMSManager(bot)
    bot.vms_manager = manager

    # ── Scheduled archive loop (DB-backed, catches missed runs) ──
    @tasks.loop(hours=ARCHIVE_JOB_INTERVAL_HOURS)
    async def _archive_loop():
        await manager.run_archive_if_due()

    @_archive_loop.before_loop
    async def _before_archive():
        await bot.wait_until_ready()

    _archive_loop.start()

    # ── One-time startup task ──
    @bot.listen("on_ready")
    async def _vms_on_ready():
        if not getattr(bot, '_vms_started', False):
            bot._vms_started = True
            # Small delay — let other modules finish their on_ready
            await asyncio.sleep(3)
            await manager.startup()

    # ================================================================
    # MESSAGE HANDLER
    # ================================================================

    @bot.listen()
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        # ────────────────────────────────────────────────
        # 1. Detect & handle incoming voice messages
        # ────────────────────────────────────────────────
        for att in message.attachments:
            # Voice messages carry IS_VOICE_MESSAGE flag (8192) and are .ogg
            raw_flags = getattr(message.flags, 'value', 0)
            is_vm = (
                bool(raw_flags & 8192) or
                att.filename.lower() == "voice-message.ogg" or
                (att.content_type and "ogg" in att.content_type.lower())
            )
            if not is_vm:
                continue

            source = (
                f"DM from {message.author}"
                if not message.guild
                else f"#{getattr(message.channel, 'name', message.channel.id)}"
                     f" in {message.guild.name}"
            )
            bot.logger.log(MODULE_NAME, f"Voice message from {message.author} in {source}")

            vm_id = await manager.save_voice_message(message, att)
            if vm_id:
                row = manager._db_one("SELECT filepath FROM vms WHERE id=?", (vm_id,))
                if row:
                    await manager.enqueue(vm_id, row[0], reply_to=message)
            return  # one VM per message is enough

        # ────────────────────────────────────────────────
        # 2. Ping / reply-to-bot  →  respond with a VM
        # ────────────────────────────────────────────────
        is_mention = bot.user in message.mentions
        is_reply_bot = (
            message.reference is not None and
            getattr(getattr(message.reference, 'resolved', None), 'author', None) == bot.user
        )

        if is_mention or is_reply_bot:
            uid = str(message.author.id)
            if manager.ping_allowed(uid):
                manager.set_ping_cooldown(uid)
                vm = manager.select_random()
                if vm:
                    vm_id, fp, dur = vm
                    bot.logger.log(MODULE_NAME,
                        f"Ping from {message.author} — replying with VM #{vm_id}")
                    await manager.send_vm(message.channel, vm_id, fp, dur)
                else:
                    bot.logger.log(MODULE_NAME, "Ping received but no eligible VMs available")
            else:
                bot.logger.log(MODULE_NAME,
                    f"Ping cooldown active for {message.author} — ignoring")
            return

        # ────────────────────────────────────────────────
        # 3. #general message counter  →  random / contextual playback
        # ────────────────────────────────────────────────
        if message.guild and getattr(message.channel, 'name', '') == GENERAL_CHANNEL_NAME:
            guild_id   = str(message.guild.id)
            channel_id = str(message.channel.id)

            count, threshold = manager._inc_counter(guild_id, channel_id)

            if count >= threshold:
                manager._reset_counter(guild_id, channel_id)

                if random.random() < PLAYBACK_CHANCE:
                    # Coin-flip: contextual vs random
                    if random.random() < 0.5:
                        recent = manager.recent_messages(guild_id, channel_id)
                        vm = manager.select_contextual(recent)
                        mode = "contextual"
                    else:
                        vm = manager.select_random()
                        mode = "random"

                    if vm:
                        vm_id, fp, dur = vm
                        bot.logger.log(MODULE_NAME,
                            f"Triggering {mode} VM playback (#{vm_id}) "
                            f"after {count} msgs in #general")
                        await manager.send_vm(message.channel, vm_id, fp, dur)
                    else:
                        bot.logger.log(MODULE_NAME,
                            f"Playback triggered ({mode}) but no eligible VMs found")
                else:
                    bot.logger.log(MODULE_NAME,
                        f"Message threshold hit ({count}) — playback skipped (50% roll missed)")

    bot.logger.log(MODULE_NAME, "VMS module setup complete")
