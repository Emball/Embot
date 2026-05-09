import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from collections import Counter
import os
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
import threading
from typing import Optional, List
from _utils import script_dir, _now

MODULE_NAME = "VMS"
GENERAL_CHANNEL_NAME = "general"
EMBALL_GUILD_ID: Optional[int] = int(os.getenv("EMBALL_GUILD_ID", "0")) or None


def _cache_subdir(*parts: str) -> Path:
    p = script_dir() / "cache" / Path(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


_vms_dir = _cache_subdir("vms")
_archive_dir = _cache_subdir("vms", "archive")
_broken_dir = _cache_subdir("vms", "broken")
_whisper_model_dir = _cache_subdir("whisper_models")
_temp_vms_dir = _cache_subdir("vms", "temp")


def _db_path() -> str:
    p = script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "vms.db")


def _vm_canonical_name(
    vm_id: int,
    username: Optional[str] = None,
    message_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> str:
    if username and message_id and created_at:
        safe_user = re.sub(r'[\\/:*?"<>|]', '', username)[:32].strip() or "unknown"
        date_str = datetime.fromtimestamp(created_at).strftime("%m-%d-%y")
        return f"vm_{safe_user}_{message_id}_{date_str}.ogg"
    return f"vm_{vm_id}.ogg"


def _parse_vm_filename(filename: str) -> dict:
    rich = re.match(
        r'^vm_(.+)_(\d{15,20})_(\d{2}-\d{2}-\d{2})\.ogg$',
        filename
    )
    if rich:
        username, message_id, date_str = rich.groups()
        try:
            dt = datetime.strptime(date_str, "%m-%d-%y")
            created_at = int(dt.timestamp())
        except ValueError:
            created_at = None
        return {"username": username, "message_id": message_id,
                "created_at": created_at, "vm_id": None}
    fallback = re.match(r'^vm_(\d+)\.ogg$', filename)
    if fallback:
        return {"username": None, "message_id": None,
                "created_at": None, "vm_id": int(fallback.group(1))}
    return {"username": None, "message_id": None, "created_at": None, "vm_id": None}


def _rename_to_canonical(
    current_path: Path,
    vm_id: int,
    username: Optional[str] = None,
    message_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> Path:
    canonical = current_path.parent / _vm_canonical_name(vm_id, username, message_id, created_at)
    if current_path == canonical:
        return canonical
    if not current_path.exists():
        return canonical
    if canonical.exists():
        try:
            current_path.unlink()
        except Exception:
            pass
        return canonical
    current_path.rename(canonical)
    return canonical


DB_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS vms (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT    NOT NULL UNIQUE,
    discord_message_id  TEXT,
    discord_channel_id  TEXT,
    guild_id            TEXT,
    duration_secs       REAL    DEFAULT 0.0,
    waveform_b64        TEXT,
    transcript          TEXT,
    processed           INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS vms_kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vms_transcription_disabled (
    user_id     TEXT NOT NULL,
    guild_id    TEXT NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);
"""


def _init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()


class VMSManager:

    def __init__(self, bot):
        self.bot = bot
        self.db_path = _db_path()
        self.vms_dir = _vms_dir
        self.archive_dir = _archive_dir
        self._token: str = os.getenv("DISCORD_BOT_TOKEN", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vms_live")
        self._scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vms_scan")
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: Optional[asyncio.Task] = None
        self._bulk_stop = threading.Event()
        self._bulk_proc: Optional["BulkProcessor"] = None
        self._backfill_running = False

        _init_db(self.db_path)
        self._migrate_filepath_column()

        self._db_exec(
            "INSERT INTO vms_startup_log (startup_time) VALUES (?)",
            (int(time.time()),)
        )
        self.bot.logger.log(MODULE_NAME, "VMSManager initialised")
        self.bot.logger.log(MODULE_NAME,
            f"Paths - vms: {self.vms_dir} | archive: {self.archive_dir} "
            f"| broken: {_broken_dir} | db: {self.db_path}")

    def _migrate_filepath_column(self):
        conn = self._conn()
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(vms)").fetchall()]
            if "filepath" not in cols:
                return
            self.bot.logger.log(MODULE_NAME, "Old DB detected: dropping filepath column...")
            conn.executescript("""
                BEGIN;
                CREATE TABLE IF NOT EXISTS vms_new (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename            TEXT    NOT NULL UNIQUE,
                    discord_message_id  TEXT,
                    discord_channel_id  TEXT,
                    guild_id            TEXT,
                    duration_secs       REAL    DEFAULT 0.0,
                    waveform_b64        TEXT,
                    transcript          TEXT,
                    processed           INTEGER DEFAULT 0,
                    created_at          INTEGER NOT NULL,
                    archived_at         INTEGER,
                    deleted_at          INTEGER
                );
                INSERT INTO vms_new
                    SELECT id, filename, discord_message_id, discord_channel_id,
                           guild_id, duration_secs, waveform_b64, transcript,
                           processed, created_at, archived_at, deleted_at
                    FROM vms;
                DROP TABLE vms;
                ALTER TABLE vms_new RENAME TO vms;
                COMMIT;
            """)
            self.bot.logger.log(MODULE_NAME, "filepath column migration complete")
        except Exception as exc:
            self.bot.logger.log(MODULE_NAME,
                f"filepath migration error (non-fatal): {exc}", "WARNING")
        finally:
            conn.close()

    def _resolve_path(self, filename: str) -> Optional[Path]:
        if not filename or filename == "__pending__":
            return None
        for d in (self.vms_dir, self.archive_dir, _broken_dir):
            p = d / filename
            if p.exists():
                return p
        return None

    def _ensure_bulk_proc(self):
        from vms_transcribe import BulkProcessor
        if self._bulk_proc is None or not self._bulk_proc.is_running():
            self._bulk_proc = BulkProcessor(self.db_path, self.vms_dir, self.bot.logger, self._bulk_stop)
            self._bulk_proc.start()

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

    def _save_backfill_checkpoint(self, last_message_id: int):
        self._db_exec(
            "INSERT OR REPLACE INTO vms_kv (key, value) VALUES ('backfill_checkpoint', ?)",
            (str(last_message_id),)
        )

    def _load_backfill_checkpoint(self) -> Optional[int]:
        row = self._db_one("SELECT value FROM vms_kv WHERE key='backfill_checkpoint'")
        return int(row[0]) if row else None

    def _clear_backfill_checkpoint(self):
        self._db_exec("DELETE FROM vms_kv WHERE key='backfill_checkpoint'")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _start_queue_worker(self):
        self._queue_task = asyncio.create_task(self._queue_worker())
        self.bot.logger.log(MODULE_NAME, "Live transcription queue worker started")

    async def _queue_worker(self):
        while True:
            try:
                vm_id, filepath, reply_to = await self.queue.get()
                self.bot.logger.log(MODULE_NAME,
                    f"Transcribing VM #{vm_id}: {Path(filepath).name}")
                try:
                    from vms_transcribe import process_file_sync
                    transcript, duration, waveform, is_broken, actual_filename = await asyncio.get_running_loop().run_in_executor(
                        self._executor, process_file_sync, filepath
                    )
                    if is_broken:
                        self._db_exec(
                            "UPDATE vms SET processed=4, filename=? WHERE id=?",
                            (actual_filename, vm_id)
                        )
                        self.bot.logger.log(MODULE_NAME,
                            f"VM #{vm_id} marked broken and quarantined")
                        if reply_to is not None:
                            try:
                                await reply_to.reply(
                                    ">  This voice message appears to be corrupt and could not be transcribed."
                                )
                            except Exception:
                                pass
                        continue

                    in_archive = (self._resolve_path(actual_filename) or Path("")).parent == self.archive_dir
                    new_state = 2 if in_archive else 1
                    self._db_exec(
                        """UPDATE vms
                           SET transcript=?, duration_secs=?, waveform_b64=?,
                               filename=?, processed=?
                           WHERE id=?""",
                        (transcript or "", duration, waveform,
                         actual_filename, new_state, vm_id)
                    )
                    self._db_exec(
                        "INSERT OR IGNORE INTO vms_playback (vm_id) VALUES (?)",
                        (vm_id,)
                    )
                    preview = (transcript or "")[:80]
                    self.bot.logger.log(MODULE_NAME,
                        f"VM #{vm_id} transcribed ({duration:.1f}s): {preview!r}")
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
        await self.queue.put((vm_id, filepath, reply_to))
        self.bot.logger.log(MODULE_NAME, f"VM #{vm_id} queued for transcription")

    def is_transcription_disabled(self, user_id: str, guild_id: str) -> bool:
        row = self._db_one(
            "SELECT 1 FROM vms_transcription_disabled WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        )
        return row is not None

    def set_transcription_disabled(self, user_id: str, guild_id: str, disabled: bool):
        if disabled:
            self._db_exec(
                "INSERT OR IGNORE INTO vms_transcription_disabled (user_id, guild_id) VALUES (?, ?)",
                (user_id, guild_id)
            )
        else:
            self._db_exec(
                "DELETE FROM vms_transcription_disabled WHERE user_id=? AND guild_id=?",
                (user_id, guild_id)
            )

    async def startup(self):
        from vms_storage import process_unprocessed, backfill, purge_bot_vms, run_archive_if_due
        from vms_transcribe import BulkProcessor, load_whisper

        await self._start_queue_worker()
        await asyncio.sleep(0.5)

        purge_bot_vms(self)

        resume_checkpoint = self._load_backfill_checkpoint()

        cutoff_dt = discord.utils.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=365)

        if not any(self.vms_dir.glob("*.ogg")):
            self.bot.logger.log(MODULE_NAME,
                "Cache is empty - starting Discord backfill and scan concurrently...")
            self._ensure_bulk_proc()
            await asyncio.gather(
                backfill(self, cutoff_dt),
                process_unprocessed(self),
            )
        elif resume_checkpoint is not None:
            scan_after = discord.utils.snowflake_time(resume_checkpoint)
            self.bot.logger.log(MODULE_NAME,
                f"Interrupted backfill detected - resuming from checkpoint {resume_checkpoint}...")
            self._ensure_bulk_proc()
            await asyncio.gather(
                backfill(self, scan_after, label="Backfill (resume)"),
                process_unprocessed(self),
            )
        else:
            await process_unprocessed(self)

        await run_archive_if_due(self)
        self.bot.logger.log(MODULE_NAME, "VMS startup complete")

    async def shutdown(self):
        from vms_transcribe import _whisper_mgr
        self.bot.logger.log(MODULE_NAME, "VMS shutting down...")
        if self._queue_task is not None and not self._queue_task.done():
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass
            self._queue_task = None
        if self._bulk_proc is not None and self._bulk_proc.is_running():
            self._bulk_proc.stop()
        self._executor.shutdown(wait=True)
        self._scan_executor.shutdown(wait=True)
        _whisper_mgr.stop_watchdog()
        _whisper_mgr.unload()
        if self._session and not self._session.closed:
            await self._session.close()
        self.bot.logger.log(MODULE_NAME, "VMS shutdown complete")


EXT_COOLDOWN_SECONDS = 30
_ext_queue: Optional[asyncio.Queue] = None
_ext_pending: list = []
_ext_pending_lock: Optional[asyncio.Lock] = None
_ext_cooldowns:    dict                    = {}
_ext_cooldown_lock: asyncio.Lock           = asyncio.Lock()
_ext_worker_task:  Optional[asyncio.Task]  = None
_ext_avg_secs:     float                   = 5.0
_EXT_AVG_ALPHA:    float                   = 0.3


async def _ext_cooldown_remaining(user_id: str) -> float:
    async with _ext_cooldown_lock:
        last = _ext_cooldowns.get(user_id, 0.0)
    return max(0.0, EXT_COOLDOWN_SECONDS - (time.time() - last))


def _ext_queue_eta(position: int) -> float:
    return position * _ext_avg_secs


def _ext_status_embed(position: int, total: int, done: bool = False,
                      transcript: str = None, error: str = None) -> discord.Embed:
    if error:
        e = discord.Embed(color=0xe74c3c)
        e.description = error
        return e
    if done:
        e = discord.Embed(color=0x2ecc71)
        e.description = f"> {transcript}"
        return e
    eta = _ext_queue_eta(position)
    eta_str = f"~{int(eta)}s" if eta < 60 else f"~{int(eta // 60)}m {int(eta % 60)}s"
    e = discord.Embed(
        title="Transcribing...",
        color=0x3498db,
    )
    if position == 1:
        e.description = "Processing now..."
    else:
        e.description = f"**Position {position}** of {total} in queue\nEstimated wait: {eta_str}"
    return e


def _build_stats_embed(manager: "VMSManager") -> discord.Embed:
    rows = manager._db_all(
        """SELECT v.id, v.duration_secs, v.transcript, v.created_at,
                  v.discord_channel_id, v.filename,
                  COALESCE(p.play_count, 0)
           FROM vms v
           LEFT JOIN vms_playback p ON v.id = p.vm_id
           WHERE v.processed = 1
             AND v.guild_id  = ?""",
        (str(EMBALL_GUILD_ID),)
    )

    if not rows:
        embed = discord.Embed(
            title="VM Stats",
            description="No transcribed voice messages in the active cache yet.",
            color=0x5865f2,
        )
        return embed

    total_vms = len(rows)
    durations = [r[1] for r in rows if r[1]]
    transcripts = [r[2] for r in rows if r[2]]
    created_ats = [r[3] for r in rows]
    channel_ids = [r[4] for r in rows if r[4]]
    filenames = [r[5] for r in rows if r[5]]
    play_counts = [r[6] for r in rows]

    total_secs = sum(durations)
    avg_secs = total_secs / len(durations) if durations else 0
    longest_secs = max(durations) if durations else 0
    shortest_secs = min(d for d in durations if d > 0) if durations else 0

    all_words: list[str] = []
    word_counts_per_vm: list[int] = []
    from vms_transcribe import STOP_WORDS
    for t in transcripts:
        words = re.findall(r"\b[a-zA-Z']{2,}\b", t)
        all_words.extend(w.lower() for w in words)
        word_counts_per_vm.append(len(words))

    total_words = len(all_words)
    avg_words = total_words / len(word_counts_per_vm) if word_counts_per_vm else 0
    word_freq = Counter(w for w in all_words if w not in STOP_WORDS)
    top_words = word_freq.most_common(5)

    total_plays = sum(play_counts)
    most_played_id, most_played_count = None, 0
    for r in rows:
        if r[6] > most_played_count:
            most_played_count = r[6]
            most_played_id = r[0]

    channel_freq = Counter(channel_ids)
    top_channel_id = channel_freq.most_common(1)[0][0] if channel_freq else None

    user_counter: Counter = Counter()
    for fn in filenames:
        parsed = _parse_vm_filename(fn)
        if parsed.get("username"):
            user_counter[parsed["username"]] += 1
    top_user, top_user_count = user_counter.most_common(1)[0] if user_counter else (None, 0)

    now = int(time.time())
    newest_ts = max(created_ats)
    oldest_ts = min(created_ats)
    span_days = max(1, (newest_ts - oldest_ts) // 86400)
    vms_per_day = total_vms / span_days

    hour_counter: Counter = Counter()
    for ts in created_ats:
        hour_counter[datetime.fromtimestamp(ts).hour] += 1
    peak_hour, peak_hour_count = hour_counter.most_common(1)[0] if hour_counter else (0, 0)

    def fmt_dur(secs: float) -> str:
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        m, s = divmod(secs, 60)
        return f"{m}m {s}s"

    def fmt_big(secs: float) -> str:
        if secs < 3600:
            return f"{secs / 60:.1f} minutes"
        if secs < 86400:
            return f"{secs / 3600:.1f} hours"
        return f"{secs / 86400:.1f} days"

    embed = discord.Embed(
        title="Voice Message Stats",
        color=0x5865f2,
        timestamp=_now(),
    )

    embed.add_field(
        name="Overview",
        value=(
            f"**{total_vms:,}** voice messages transcribed\n"
            f"**{fmt_big(total_secs)}** of total audio\n"
            f"**{total_words:,}** words spoken in total"
        ),
        inline=False,
    )

    embed.add_field(
        name="Duration",
        value=(
            f"Average: **{fmt_dur(avg_secs)}**\n"
            f"Longest: **{fmt_dur(longest_secs)}**\n"
            f"Shortest: **{fmt_dur(shortest_secs)}**"
        ),
        inline=True,
    )

    embed.add_field(
        name="Words",
        value=(
            f"Avg per VM: **{avg_words:.0f}**\n"
            f"Top words:\n"
            + "\n".join(f"`{w}` x{c:,}" for w, c in top_words)
        ),
        inline=True,
    )

    embed.add_field(
        name="Playback",
        value=(
            f"Total plays: **{total_plays:,}**\n"
            + (f"Most played: VM **#{most_played_id}** ({most_played_count}x)" if most_played_id else "No plays yet")
        ),
        inline=True,
    )

    peak_hour_fmt = datetime.strptime(str(peak_hour), "%H").strftime("%I %p").lstrip("0")
    facts = [
        f"Chattiest sender: **{top_user}** with {top_user_count} VMs" if top_user else None,
        f"Busiest channel: <#{top_channel_id}>" if top_channel_id else None,
        f"Peak sending hour: **{peak_hour_fmt}** ({peak_hour_count} VMs)",
        f"Avg rate: **{vms_per_day:.1f}** VMs/day over the last {span_days:,} days",
        f"That's roughly **{total_words / max(total_vms, 1):.0f}** words per VM on average",
    ]
    embed.add_field(
        name="Fun Facts",
        value="\n".join(f for f in facts if f),
        inline=False,
    )

    embed.set_footer(text="Active cache only - archived & deleted VMs excluded")
    return embed


def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up VMS module")

    global _ext_queue, _ext_pending_lock, _ext_worker_task

    from vms_transcribe import (
        _whisper_mgr, WHISPER_MODEL_SIZE,
        transcribe_with_model, process_file_sync, BulkProcessor,
    )
    from vms_storage import (
        save_voice_message, process_unprocessed, backfill,
        run_archive_if_due, purge_bot_vms, ARCHIVE_JOB_INTERVAL_HOURS,
    )
    from vms_playback import (
        GENERAL_CHANNEL_NAME, send_vm, select_contextual, select_random,
        get_counter, inc_counter, reset_counter, ping_allowed,
        set_ping_cooldown, recent_messages,
    )

    async def _start_ext_worker():
        global _ext_queue, _ext_pending_lock, _ext_worker_task

        _ext_queue = asyncio.Queue()
        _ext_pending_lock = asyncio.Lock()

        async def _update_positions():
            async with _ext_pending_lock:
                total = len(_ext_pending)
                for i, entry in enumerate(_ext_pending, start=1):
                    try:
                        await entry['msg'].edit(embed=_ext_status_embed(i, total))
                    except Exception:
                        pass

        async def _ext_worker():
            global _ext_avg_secs
            while True:
                try:
                    item = await _ext_queue.get()
                    vm_att, temp_path, followup_msg = item['vm_att'], item['temp_path'], item['msg']

                    async with _ext_pending_lock:
                        total = len(_ext_pending)
                        snapshot = [e for e in _ext_pending if e['msg'] != followup_msg]
                    try:
                        await followup_msg.edit(embed=_ext_status_embed(1, total))
                    except Exception:
                        pass
                    for i, entry in enumerate(snapshot, start=2):
                        try:
                            await entry['msg'].edit(embed=_ext_status_embed(i, total))
                        except Exception:
                            pass

                    t_start = time.time()
                    transcript = None
                    broken = False
                    try:
                        raw = await vm_att.read()
                        temp_path.write_bytes(raw)
                        loop = asyncio.get_running_loop()
                        transcript, _, broken = await loop.run_in_executor(
                            manager._executor,
                            transcribe_with_model, str(temp_path), WHISPER_MODEL_SIZE
                        )
                    except Exception as exc:
                        bot.logger.log(MODULE_NAME, f"Ext worker transcribe error: {exc}", "ERROR")
                        broken = True
                    finally:
                        if temp_path.exists():
                            try:
                                temp_path.unlink()
                            except Exception:
                                pass

                    elapsed = time.time() - t_start
                    _ext_avg_secs = _EXT_AVG_ALPHA * elapsed + (1 - _EXT_AVG_ALPHA) * _ext_avg_secs

                    try:
                        if broken or not transcript:
                            await followup_msg.edit(
                                embed=_ext_status_embed(0, 0, error="Could not transcribe that voice message.")
                            )
                        else:
                            await followup_msg.edit(
                                embed=_ext_status_embed(0, 0, done=True, transcript=transcript)
                            )
                    except Exception as exc:
                        bot.logger.log(MODULE_NAME, f"Ext worker: failed to edit result: {exc}", "WARNING")

                    async with _ext_pending_lock:
                        try:
                            _ext_pending.remove(item)
                        except ValueError:
                            pass
                    await _update_positions()
                    _ext_queue.task_done()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    bot.logger.log(MODULE_NAME, f"Ext worker unexpected error: {exc}", "ERROR")
                    await asyncio.sleep(1)

        async def _watchdog():
            global _ext_worker_task
            while True:
                await asyncio.sleep(5)
                if _ext_worker_task is None or _ext_worker_task.done():
                    exc = _ext_worker_task.exception() if _ext_worker_task and not _ext_worker_task.cancelled() else None
                    bot.logger.log(MODULE_NAME,
                        f"Ext worker died ({exc}) - restarting", "WARNING")
                    _ext_worker_task = asyncio.create_task(_ext_worker())

        _ext_worker_task = asyncio.create_task(_ext_worker())
        asyncio.create_task(_watchdog())
        bot.logger.log(MODULE_NAME, "External transcription queue worker started")

    manager = VMSManager(bot)
    bot.vms_manager = manager

    @tasks.loop(hours=ARCHIVE_JOB_INTERVAL_HOURS)
    async def _archive_loop():
        await run_archive_if_due(manager)

    @_archive_loop.before_loop
    async def _before_archive():
        await bot.wait_until_ready()

    _archive_loop.start()

    @tasks.loop(count=1)
    async def _startup_task():
        await _start_ext_worker()
        await manager.startup()

    @_startup_task.before_loop
    async def _before_startup():
        await bot.wait_until_ready()
        await asyncio.sleep(3)

    _startup_task.start()

    if hasattr(bot, 'console_commands'):
        async def handle_vms_resume(args_str: str):
            date_str = args_str.strip()
            if not date_str:
                print("Usage: vms-resume <date>")
                print(" Examples:")
                print("   vms-resume 2024-11-15")
                print("   vms-resume 2024-11-15 14:30")
                print("   vms-resume 2024-11-15 14:30:00")
                return
            since_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    since_dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if since_dt is None:
                print(f"Could not parse date '{date_str}'.")
                return
            if getattr(manager, '_backfill_running', False):
                print("A backfill is already in progress - wait for it to finish before resuming.")
                return
            print(f"Starting vms-resume from {since_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} "
                  f"(downloading everything after this date that is not already stored)...")
            print("Progress will be logged to console - this runs in the background.")
            asyncio.run_coroutine_threadsafe(
                backfill(manager, since_dt, label="vms-resume"),
                bot.loop,
            )

        bot.console_commands['vms-resume'] = {
            'description': 'Re-download/process VMs from a date  (vms-resume YYYY-MM-DD)',
            'handler': handle_vms_resume,
        }
        bot.logger.log(MODULE_NAME, "Registered console command: vms-resume")

    @bot.listen()
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        for att in message.attachments:
            is_vm = att.filename.lower() == "voice-message.ogg"
            if not is_vm:
                continue

            source = (
                f"DM from {message.author}"
                if not message.guild
                else f"#{getattr(message.channel, 'name', message.channel.id)}"
                     f"in {message.guild.name}"
            )
            bot.logger.log(MODULE_NAME,
                f"Voice message from {message.author} in {source}")

            vm_id = await save_voice_message(manager, message, att)
            if vm_id:
                row = manager._db_one("SELECT filename FROM vms WHERE id=?", (vm_id,))
                if row:
                    resolved = manager._resolve_path(row[0])
                    if resolved:
                        guild_id = str(message.guild.id) if message.guild else None
                        opted_out = (
                            guild_id is not None and
                            manager.is_transcription_disabled(str(message.author.id), guild_id)
                        )
                        if not opted_out:
                            await manager.enqueue(vm_id, str(resolved), reply_to=message)
                        else:
                            bot.logger.log(MODULE_NAME,
                                f"Auto-transcription skipped for VM #{vm_id} "
                                f"(user {message.author} has opted out)")
            return

        is_mention = bot.user in message.mentions
        is_reply_bot = (
            message.reference is not None and
            getattr(getattr(message.reference, 'resolved', None), 'author', None) == bot.user
        )

        if is_mention or is_reply_bot:
            uid = str(message.author.id)
            if ping_allowed(manager, uid):
                set_ping_cooldown(manager, uid)
                vm = select_random(manager)
                if vm:
                    vm_id, fp, dur = vm
                    bot.logger.log(MODULE_NAME,
                        f"Ping from {message.author} - replying with VM #{vm_id}")
                    await send_vm(manager, message.channel, vm_id, fp, dur)
                else:
                    bot.logger.log(MODULE_NAME,
                        "Ping received but no eligible VMs available")
            else:
                bot.logger.log(MODULE_NAME,
                    f"Ping cooldown active for {message.author} - ignoring")
            return

        if message.guild and getattr(message.channel, 'name', '') == GENERAL_CHANNEL_NAME:
            guild_id = str(message.guild.id)
            channel_id = str(message.channel.id)
            count, threshold = inc_counter(manager, guild_id, channel_id)

            if count >= threshold:
                reset_counter(manager, guild_id, channel_id)

                if random.random() < 0.5:
                    recent = recent_messages(manager, guild_id, channel_id)
                    vm = select_contextual(manager, recent)
                    mode = "contextual"
                else:
                    vm = select_random(manager)
                    mode = "random"

                if vm:
                    vm_id, fp, dur = vm
                    bot.logger.log(MODULE_NAME,
                        f"Triggering {mode} VM playback (#{vm_id}) "
                        f"after {count} msgs in #general")
                    await send_vm(manager, message.channel, vm_id, fp, dur)
                else:
                    bot.logger.log(MODULE_NAME,
                        f"Playback triggered ({mode}) but no eligible VMs found")

    _original_close = bot.close
    async def _patched_close():
        await manager.shutdown()
        await _original_close()
    bot.close = _patched_close

    bot.logger.log(MODULE_NAME, "VMS module setup complete")

    async def _do_transcribe(
        interaction: discord.Interaction,
        message: discord.Message,
        ephemeral: bool,
    ):
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)

        vm_att = None
        for att in message.attachments:
            if att.filename.lower() == "voice-message.ogg":
                vm_att = att
                break

        if vm_att is None:
            await interaction.followup.send(
                "No voice message found on that message.", ephemeral=True
            )
            return

        is_emball = (
            message.guild is not None
            and EMBALL_GUILD_ID is not None
            and message.guild.id == EMBALL_GUILD_ID
        )

        if not is_emball:
            user_id = str(interaction.user.id)
            remaining_cd = await _ext_cooldown_remaining(user_id)
            if remaining_cd > 0:
                await interaction.followup.send(
                    f"You're on cooldown. Try again in **{int(remaining_cd) + 1}s**.",
                    ephemeral=True
                )
                return

            async with _ext_cooldown_lock:
                _ext_cooldowns[user_id] = time.time()

            temp_dir = _temp_vms_dir
            safe_name = re.sub(r'[^\w\-.]', '_', vm_att.filename)
            temp_path = temp_dir / f"ext_{message.id}_{safe_name}"

            async with _ext_pending_lock:
                position = len(_ext_pending) + 1
            total = position

            init_embed = _ext_status_embed(position, total)
            followup_msg = await interaction.followup.send(embed=init_embed, ephemeral=True, wait=True)

            item = {'vm_att': vm_att, 'temp_path': temp_path, 'msg': followup_msg}
            async with _ext_pending_lock:
                _ext_pending.append(item)
            await _ext_queue.put(item)
            return

        try:
            existing = manager._db_one(
                """SELECT transcript FROM vms
                   WHERE discord_message_id=? AND transcript IS NOT NULL AND transcript != ''""",
                (str(message.id),)
            )
            if existing:
                await interaction.followup.send(f"> {existing[0]}", ephemeral=ephemeral)
                return

            vm_id = await save_voice_message(manager, message, vm_att)
            if not vm_id:
                await interaction.followup.send(
                    "Failed to save voice message.", ephemeral=True
                )
                return

            row = manager._db_one("SELECT filename FROM vms WHERE id=?", (vm_id,))
            filepath = str(manager._resolve_path(row[0])) if row else None

            if not filepath:
                await interaction.followup.send(
                    "VM saved but file could not be located for transcription.", ephemeral=True
                )
                return

            loop = asyncio.get_running_loop()
            transcript, duration, broken = await loop.run_in_executor(
                manager._executor,
                transcribe_with_model, filepath, WHISPER_MODEL_SIZE
            )

            if broken or not transcript:
                await interaction.followup.send(
                    "Could not transcribe that voice message.", ephemeral=True
                )
                return

            manager._db_exec(
                "UPDATE vms SET transcript=?, processed=1 WHERE id=? AND processed=0",
                (transcript, vm_id)
            )
            manager._db_exec(
                "INSERT OR IGNORE INTO vms_playback (vm_id) VALUES (?)", (vm_id,)
            )
            await interaction.followup.send(f"> {transcript}", ephemeral=ephemeral)

        except Exception as exc:
            bot.logger.log(MODULE_NAME, f"App transcribe (Emball) error: {exc}", "ERROR")
            await interaction.followup.send(
                "An error occurred while transcribing.", ephemeral=True
            )

    def _has_vm(message: discord.Message) -> bool:
        for att in message.attachments:
            if att.filename.lower() == "voice-message.ogg":
                return True
        return False

    @bot.tree.context_menu(name="Transcribe VM (For Everyone)")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def transcribe_vm_public(interaction: discord.Interaction, message: discord.Message):
        if not _has_vm(message):
            await interaction.response.send_message(
                "That message doesn't contain a voice message.", ephemeral=True
            )
            return
        await _do_transcribe(interaction, message, ephemeral=False)

    @bot.tree.context_menu(name="Transcribe VM (Only Me)")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def transcribe_vm_private(interaction: discord.Interaction, message: discord.Message):
        if not _has_vm(message):
            await interaction.response.send_message(
                "That message doesn't contain a voice message.", ephemeral=True
            )
            return
        await _do_transcribe(interaction, message, ephemeral=True)

    bot.logger.log(MODULE_NAME, "Registered context menus: Transcribe VM (For Everyone) / Transcribe VM (Only Me)")

    @bot.tree.command(
        name="vmtranscribe",
        description="Enable or disable automatic transcription of your voice messages in this server."
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(setting="Whether to enable or disable auto-transcription of your VMs.")
    @discord.app_commands.choices(setting=[
        discord.app_commands.Choice(name="disable", value="disable"),
        discord.app_commands.Choice(name="enable", value="enable"),
    ])
    async def vm_transcribe_toggle(interaction: discord.Interaction, setting: str):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)

        if setting == "disable":
            if manager.is_transcription_disabled(user_id, guild_id):
                await interaction.response.send_message(
                    "Auto-transcription of your voice messages is already **disabled** in this server.",
                    ephemeral=True
                )
            else:
                manager.set_transcription_disabled(user_id, guild_id, disabled=True)
                bot.logger.log(MODULE_NAME,
                    f"Auto-transcription disabled for user {interaction.user} in guild {guild_id}")
                await interaction.response.send_message(
                    "Auto-transcription of your voice messages has been **disabled** in this server.\n"
                    "Your VMs will still be saved - they just won't be transcribed automatically.\n"
                    "You can re-enable at any time with `/vmtranscribe enable`.",
                    ephemeral=True
                )
        else:
            if not manager.is_transcription_disabled(user_id, guild_id):
                await interaction.response.send_message(
                    "Auto-transcription of your voice messages is already **enabled** in this server.",
                    ephemeral=True
                )
            else:
                manager.set_transcription_disabled(user_id, guild_id, disabled=False)
                bot.logger.log(MODULE_NAME,
                    f"Auto-transcription re-enabled for user {interaction.user} in guild {guild_id}")
                await interaction.response.send_message(
                    "Auto-transcription of your voice messages has been **enabled** in this server.",
                    ephemeral=True
                )

    bot.logger.log(MODULE_NAME, "Registered slash command: /vmtranscribe")

    @bot.tree.command(
        name="vmstats",
        description="Fun stats and facts about the voice message archive."
    )
    @discord.app_commands.guild_only()
    async def vm_stats(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            loop = asyncio.get_running_loop()
            embed = await loop.run_in_executor(manager._executor, _build_stats_embed, manager)
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            bot.logger.error(MODULE_NAME, "vmstats command error", exc)
            await interaction.followup.send("Failed to fetch stats.", ephemeral=True)

    bot.logger.log(MODULE_NAME, "Registered slash command: /vmstats")
