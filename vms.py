# [file name]: vms.py
"""
VMS — Voice Message System for Embot
=====================================
• Detects & saves Discord voice messages to /data/vms/
• DB lives at                              /data/vms/vms.db
• Archives live at                         /data/vms/archive/
• Transcribes using OpenAI Whisper (auto-downloads model to /data/)
• Posts transcripts as plain blockquote replies (no embeds)
• Saves transcripts to SQLite DB for keyword playback
• Archives after 150 days, deletes after 365 days
• Archive job schedule stored in DB — catches missed runs after crashes
• Periodic random playback in #general (every 40–80 messages, 50% chance)
• Contextual playback using keyword matching against transcripts
• Smart selection: 7-day cooldowns, long-VM penalties, recency weighting
• Responds to @mentions / replies with a random VM (10s cooldown)
• Uniform filename convention: vm-{db_id}.ogg for every file
  - New VMs are renamed immediately after DB insert to get their ID
  - Retroactive/bulk VMs are renamed after processing
  - Existing non-conforming files are conformed on startup
  - Metadata unavailable for retroactive files (user/message IDs) is NULL
• Bulk-processes pre-populated folders at startup using a dedicated
  background thread pool (GPU-parallel or CPU-multi-threaded) that
  never blocks normal bot operation
• Generates real waveform data from OGG audio samples for every VM
"""

import asyncio
import aiohttp
import base64
import discord
import numpy as np
import os
import random
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from discord.ext import tasks

MODULE_NAME = "VMS"

# ==================== CONFIGURATION ====================

GENERAL_CHANNEL_NAME      = "general"
PING_COOLDOWN_SECONDS     = 10
VM_COOLDOWN_DAYS          = 7
LONG_VM_THRESHOLD_SECS    = 60        # VMs longer than this get a score penalty
ARCHIVE_AFTER_DAYS        = 150
DELETE_AFTER_DAYS         = 365
ARCHIVE_JOB_INTERVAL_HOURS = 24
RANDOM_PLAYBACK_MIN       = 40
RANDOM_PLAYBACK_MAX       = 80
PLAYBACK_CHANCE           = 0.50      # 50 % chance to trigger playback at threshold
WHISPER_MODEL_SIZE        = "base"    # tiny / base / small / medium / large

# Bulk-processing concurrency
# GPU path: Whisper itself parallelises across CUDA cores — keep 1 worker
# CPU path: use (cpu_count / 2) workers so bot stays responsive
BULK_GPU_WORKERS  = 1
BULK_CPU_WORKERS  = max(2, (os.cpu_count() or 4) // 2)
BULK_BATCH_SIZE   = 16               # files committed to DB per batch
WAVEFORM_SAMPLES  = 256              # Discord expects 256-byte waveform

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

def _data_dir() -> Path:
    """Root data directory — /data relative to script."""
    p = _script_dir() / "data"
    p.mkdir(exist_ok=True)
    return p

def _vms_dir() -> Path:
    """Voice message audio files live here."""
    p = _data_dir() / "vms"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _archive_dir() -> Path:
    """Archived voice messages live here."""
    p = _data_dir() / "vms" / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _db_path() -> str:
    """SQLite database path."""
    return str(_vms_dir() / "vms.db")

def _whisper_model_dir() -> Path:
    """Whisper model cache directory."""
    p = _data_dir()
    p.mkdir(exist_ok=True)
    return p

# ==================== NAMING CONVENTION ====================

def _vm_canonical_name(vm_id: int) -> str:
    """Return the canonical filename for a VM given its DB id: vm-{id}.ogg"""
    return f"vm-{vm_id}.ogg"


def _rename_to_canonical(current_path: Path, vm_id: int) -> Path:
    """
    Rename *current_path* to the canonical name inside the same directory.
    Returns the new Path.  No-ops safely if the file is already canonical,
    or if the source doesn't exist.
    """
    canonical = current_path.parent / _vm_canonical_name(vm_id)
    if current_path == canonical:
        return canonical
    if not current_path.exists():
        return canonical          # already moved or missing — return target path
    if canonical.exists():
        # Destination already exists (e.g. partial rename from a previous run)
        # Remove the stale source so we don't accumulate duplicates.
        try:
            current_path.unlink()
        except Exception:
            pass
        return canonical
    current_path.rename(canonical)
    return canonical


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
    waveform_b64        TEXT,
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

# ==================== WAVEFORM GENERATION ====================

def _generate_waveform(filepath: str, num_samples: int = WAVEFORM_SAMPLES) -> str:
    """
    Read an OGG/Opus audio file and produce a real waveform for Discord.

    Discord expects a base64-encoded byte string where each byte represents
    the normalised amplitude (0–255) at that point in the audio timeline.

    Falls back to a smooth pseudo-random waveform if audio decoding fails.
    """
    # ── Try soundfile (fastest, no subprocess) ──
    try:
        import soundfile as sf
        data, _ = sf.read(filepath, dtype="float32", always_2d=True)
        samples = _downsample_to_waveform(data[:, 0], num_samples)
        return base64.b64encode(bytes(samples)).decode()
    except Exception:
        pass

    # ── Try pydub (relies on ffmpeg or libav) ──
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(filepath)
        raw = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32)
        raw /= 32768.0
        samples = _downsample_to_waveform(raw, num_samples)
        return base64.b64encode(bytes(samples)).decode()
    except Exception:
        pass

    # ── Fallback: plausible envelope-shaped waveform ──
    return _fallback_waveform(num_samples)


def _downsample_to_waveform(pcm: np.ndarray, num_samples: int) -> list:
    """
    Compress a float32 PCM array [-1, 1] down to `num_samples` amplitude bytes.
    Uses RMS per chunk so quiet passages look quiet and loud ones look loud.
    """
    if len(pcm) == 0:
        return [0] * num_samples

    chunk_size = max(1, len(pcm) // num_samples)
    result = []
    for i in range(num_samples):
        start = i * chunk_size
        chunk = pcm[start: start + chunk_size]
        if len(chunk) == 0:
            result.append(0)
        else:
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            # Map 0.0–0.7+ RMS → 0–255, clamp
            val = int(min(255, rms * 364))
            result.append(val)

    # Gentle smoothing pass so the waveform doesn't look like noise
    smoothed = result[:]
    for i in range(1, len(result) - 1):
        smoothed[i] = int((result[i - 1] + result[i] * 2 + result[i + 1]) / 4)
    return smoothed


def _fallback_waveform(num_samples: int) -> str:
    """
    Generate a smooth, realistic-looking bell-curve waveform as a fallback
    when audio decoding libraries are unavailable.
    """
    t = np.linspace(0, np.pi, num_samples)
    envelope = np.sin(t) ** 0.6             # nice bell shape
    noise = np.random.uniform(0.5, 1.0, num_samples)
    wave = (envelope * noise * 200).astype(np.uint8)
    return base64.b64encode(bytes(wave)).decode()

# ==================== AUDIO HELPERS ====================

def _get_ogg_duration(filepath: str) -> float:
    """Best-effort OGG duration extraction."""
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

# ==================== WHISPER (lazy-loaded, thread-safe, custom model dir) ==

_whisper_model  = None
_whisper_lock   = threading.Lock()
_whisper_device = "cpu"


def _load_whisper() -> Optional[object]:
    """
    Load OpenAI Whisper once, storing the model under /data/ so it doesn't
    re-download on every restart.  Prefers CUDA GPU, falls back to CPU.
    """
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

            model_dir = str(_whisper_model_dir())
            print(f"[{MODULE_NAME}] Loading Whisper '{WHISPER_MODEL_SIZE}' "
                  f"on {_whisper_device} (cache: {model_dir})…")
            # whisper.load_model accepts download_root to pin the cache location
            _whisper_model = whisper.load_model(
                WHISPER_MODEL_SIZE,
                device=_whisper_device,
                download_root=model_dir,
            )
            print(f"[{MODULE_NAME}] Whisper loaded on {_whisper_device}")
        except ImportError:
            print(f"[{MODULE_NAME}] openai-whisper not installed — transcription disabled")
        except Exception as exc:
            print(f"[{MODULE_NAME}] Whisper load error: {exc}")
    return _whisper_model


def _process_file_sync(filepath: str) -> Tuple[Optional[str], float, str]:
    """
    Full synchronous processing for one OGG file.
    Returns (transcript, duration_secs, waveform_b64).
    Safe to call from any thread — Whisper is loaded once and reused.
    """
    duration  = _get_ogg_duration(filepath)
    waveform  = _generate_waveform(filepath)
    model     = _load_whisper()

    transcript: Optional[str] = None
    if model is not None:
        try:
            result     = model.transcribe(filepath, fp16=(_whisper_device == "cuda"))
            transcript = (result.get("text") or "").strip() or None
        except Exception as exc:
            print(f"[{MODULE_NAME}] Transcription error ({filepath}): {exc}")

    return transcript, duration, waveform

# ==================== DISCORD VOICE MESSAGE API ====================

async def _send_voice_message(
    bot_token: str,
    channel_id: int,
    ogg_path: str,
    duration_secs: float,
    waveform_b64: str,
    session: aiohttp.ClientSession,
) -> Optional[dict]:
    """
    Upload an OGG file and post it as a Discord voice message (flags: 8192).
    Uses a real waveform generated from the audio file.

    Protocol:
      1. POST /channels/{id}/attachments  → upload_url + upload_filename
      2. PUT  upload_url                  → raw OGG bytes to CDN
      3. POST /channels/{id}/messages     → voice message referencing upload
    """
    ogg_bytes = Path(ogg_path).read_bytes()
    file_size = len(ogg_bytes)

    headers_json = {
        "Content-Type": "application/json",
        "Authorization": f"Bot {bot_token}",
    }

    # Step 1: Request upload URL
    try:
        async with session.post(
            f"https://discord.com/api/v10/channels/{channel_id}/attachments",
            headers=headers_json,
            json={"files": [{"filename": "voice-message.ogg",
                             "file_size": file_size, "id": "2"}]},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[{MODULE_NAME}] Upload-URL request failed ({resp.status}): {body[:200]}")
                return None
            data = await resp.json()
    except Exception as exc:
        print(f"[{MODULE_NAME}] Upload-URL request error: {exc}")
        return None

    attachment      = data["attachments"][0]
    upload_url: str = attachment["upload_url"]
    upload_filename: str = attachment["upload_filename"]

    # Step 2: Upload file bytes to CDN
    try:
        async with session.put(
            upload_url,
            headers={"Content-Type": "audio/ogg",
                     "Authorization": f"Bot {bot_token}"},
            data=ogg_bytes,
        ) as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                print(f"[{MODULE_NAME}] CDN upload failed ({resp.status}): {body[:200]}")
                return None
    except Exception as exc:
        print(f"[{MODULE_NAME}] CDN upload error: {exc}")
        return None

    # Step 3: Post voice message with real waveform
    payload = {
        "flags": 8192,
        "attachments": [{
            "id": "0",
            "filename": "voice-message.ogg",
            "uploaded_filename": upload_filename,
            "duration_secs": max(duration_secs, 1.0),
            "waveform": waveform_b64,
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


# ==================== BULK PROCESSOR ====================

class BulkProcessor:
    """
    Dedicated background processor for large backlogs of pre-placed OGG files.

    Architecture
    ─────────────
    • Runs entirely in a separate daemon thread — zero asyncio entanglement.
    • Uses a ThreadPoolExecutor sized for GPU (1 worker) or CPU (N/2 workers).
    • Each worker calls _process_file_sync() which loads Whisper once and
      reuses it across all files (no redundant model loads).
    • Results are committed to SQLite in batches (BULK_BATCH_SIZE) so a crash
      mid-run doesn't lose all progress — already-committed rows are skipped on
      the next startup.
    • A threading.Event allows the main bot to signal a graceful shutdown.
    • Progress is logged every BULK_BATCH_SIZE files so you can track a 4k+ queue.
    """

    def __init__(self, db_path: str, vms_dir: Path, logger, stop_event: threading.Event):
        self.db_path    = db_path
        self.vms_dir    = vms_dir
        self.logger     = logger          # bot.logger — thread-safe writes
        self.stop_event = stop_event
        self._thread: Optional[threading.Thread] = None

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self, files: List[Tuple[int, str]]):
        """
        Kick off bulk processing of (vm_id, filepath) pairs in a daemon thread.
        Returns immediately; progress is written to the DB in the background.
        """
        if not files:
            self.logger.log(MODULE_NAME, "BulkProcessor: no files to process")
            return

        self._thread = threading.Thread(
            target=self._run,
            args=(files,),
            name="vms_bulk_processor",
            daemon=True,
        )
        self._thread.start()
        self.logger.log(MODULE_NAME,
            f"BulkProcessor started: {len(files)} files, "
            f"workers={'GPU×' + str(BULK_GPU_WORKERS) if _is_cuda() else 'CPU×' + str(BULK_CPU_WORKERS)}")

    def stop(self):
        """Signal the processor to stop after finishing the current batch."""
        self.stop_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ────────────────────────────────────────────────────────────

    def _run(self, files: List[Tuple[int, str]]):
        n_workers = BULK_GPU_WORKERS if _is_cuda() else BULK_CPU_WORKERS
        total     = len(files)
        done      = 0
        errors    = 0
        batch_buf = []  # [(vm_id, transcript, duration, waveform)]

        self.logger.log(MODULE_NAME,
            f"BulkProcessor: processing {total} files with {n_workers} worker(s)…")

        try:
            with ThreadPoolExecutor(max_workers=n_workers,
                                    thread_name_prefix="vms_bulk") as pool:
                # Submit all futures up-front; as_completed() yields them as
                # they finish so we can commit incrementally.
                future_map = {
                    pool.submit(_process_file_sync, fp): (vm_id, fp)
                    for vm_id, fp in files
                    if Path(fp).exists()
                }

                for future in as_completed(future_map):
                    if self.stop_event.is_set():
                        self.logger.log(MODULE_NAME,
                            "BulkProcessor: stop requested — exiting early")
                        pool.shutdown(wait=False, cancel_futures=True)
                        break

                    vm_id, fp = future_map[future]
                    try:
                        transcript, duration, waveform = future.result()
                        batch_buf.append((vm_id, transcript or "", duration, waveform, fp))
                        done += 1
                    except Exception as exc:
                        self.logger.log(MODULE_NAME,
                            f"BulkProcessor: error on VM #{vm_id} ({fp}): {exc}", "WARNING")
                        errors += 1
                        done   += 1

                    # Commit batch
                    if len(batch_buf) >= BULK_BATCH_SIZE:
                        self._commit_batch(batch_buf)
                        batch_buf = []
                        self.logger.log(MODULE_NAME,
                            f"BulkProcessor: {done}/{total} processed "
                            f"({errors} errors) — batch committed")

            # Commit any remainder
            if batch_buf:
                self._commit_batch(batch_buf)

        except Exception as exc:
            self.logger.log(MODULE_NAME,
                f"BulkProcessor: fatal error — {exc}", "ERROR")

        self.logger.log(MODULE_NAME,
            f"BulkProcessor complete: {done}/{total} processed, {errors} errors")

    def _commit_batch(self, batch: list):
        """
        Write a batch of results to SQLite and rename each file to vm-{id}.ogg.
        Opens its own connection — safe to call from a worker thread.
        batch items: (vm_id, transcript, duration, waveform, filepath)
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                for vm_id, transcript, duration, waveform, filepath in batch:
                    # Rename file to canonical name now that processing is done
                    try:
                        new_path = _rename_to_canonical(Path(filepath), vm_id)
                        canon_name = new_path.name
                        canon_fp   = str(new_path)
                    except Exception as exc:
                        self.logger.log(MODULE_NAME,
                            f"BulkProcessor: rename failed for VM #{vm_id}: {exc}", "WARNING")
                        canon_name = Path(filepath).name
                        canon_fp   = filepath

                    conn.execute(
                        """UPDATE vms
                           SET transcript=?, duration_secs=?, waveform_b64=?,
                               filename=?, filepath=?, processed=1
                           WHERE id=? AND processed=0""",
                        (transcript, duration, waveform, canon_name, canon_fp, vm_id)
                    )

                # Ensure playback rows exist for all committed VMs
                conn.executemany(
                    "INSERT OR IGNORE INTO vms_playback (vm_id) VALUES (?)",
                    [(vid,) for vid, *_ in batch],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            self.logger.log(MODULE_NAME,
                f"BulkProcessor: DB commit error — {exc}", "ERROR")


def _is_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


# ==================== VMS MANAGER ====================

class VMSManager:
    """
    Central manager for the VMS (Voice Message System) module.
    Handles storage, transcription queuing, archiving, and playback.
    """

    def __init__(self, bot):
        self.bot         = bot
        self.db_path     = _db_path()
        self.vms_dir     = _vms_dir()
        self.archive_dir = _archive_dir()
        self._token: str = os.getenv("DISCORD_BOT_TOKEN", "")
        self._session: Optional[aiohttp.ClientSession] = None
        # Small executor for single-file live transcriptions (not bulk)
        self._executor   = ThreadPoolExecutor(max_workers=2,
                                              thread_name_prefix="vms_live")
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: Optional[asyncio.Task] = None
        self._bulk_stop  = threading.Event()
        self._bulk_proc: Optional[BulkProcessor] = None

        _init_db(self.db_path)

        self._db_exec(
            "INSERT INTO vms_startup_log (startup_time) VALUES (?)",
            (int(time.time()),)
        )
        self.bot.logger.log(MODULE_NAME, "VMSManager initialised")
        self.bot.logger.log(MODULE_NAME,
            f"Paths — vms: {self.vms_dir} | archive: {self.archive_dir} | db: {self.db_path}")

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

    # -------------------------------------------------- Live Transcription Queue --

    async def _start_queue_worker(self):
        self._queue_task = asyncio.create_task(self._queue_worker())
        self.bot.logger.log(MODULE_NAME, "Live transcription queue worker started")

    async def _queue_worker(self):
        """
        Processes live (just-received) voice messages one at a time.
        Runs in the event loop — uses run_in_executor for the blocking work.
        """
        while True:
            try:
                vm_id, filepath, _unused, reply_to = await self.queue.get()
                self.bot.logger.log(MODULE_NAME,
                    f"Transcribing VM #{vm_id}: {Path(filepath).name}")
                try:
                    transcript, duration, waveform = await asyncio.get_event_loop().run_in_executor(
                        self._executor, _process_file_sync, filepath
                    )

                    # Rename to canonical vm-{id}.ogg now that processing is done
                    try:
                        new_path = await asyncio.get_event_loop().run_in_executor(
                            self._executor, _rename_to_canonical, Path(filepath), vm_id
                        )
                        canon_fp   = str(new_path)
                        canon_name = new_path.name
                    except Exception as exc:
                        self.bot.logger.log(MODULE_NAME,
                            f"Rename failed for VM #{vm_id}: {exc}", "WARNING")
                        canon_fp   = filepath
                        canon_name = Path(filepath).name

                    self._db_exec(
                        """UPDATE vms
                           SET transcript=?, duration_secs=?, waveform_b64=?,
                               filename=?, filepath=?, processed=1
                           WHERE id=?""",
                        (transcript or "", duration, waveform,
                         canon_name, canon_fp, vm_id)
                    )
                    self._db_exec(
                        "INSERT OR IGNORE INTO vms_playback (vm_id) VALUES (?)",
                        (vm_id,)
                    )

                    preview = (transcript or "")[:80]
                    self.bot.logger.log(MODULE_NAME,
                        f"VM #{vm_id} transcribed ({duration:.1f}s): {preview!r}")

                    # Plain blockquote reply — no embed, no fancy formatting
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
        """Add a live voice message to the transcription queue."""
        await self.queue.put((vm_id, filepath, None, reply_to))
        self.bot.logger.log(MODULE_NAME, f"VM #{vm_id} queued for transcription")

    # ------------------------------------------------------- Save Voice Msg --

    async def save_voice_message(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> Optional[int]:
        """
        Download and persist a Discord voice message attachment.

        Naming flow:
          1. Pre-insert a placeholder row to claim the auto-assigned DB id
          2. Derive the canonical filename: vm-{id}.ogg
          3. Write bytes directly to that path — no temp file, no rename
          4. UPDATE the row with the real filepath, duration, and metadata

        Returns the DB row id, or None on failure.
        """
        ts       = int(time.time())
        guild_id = str(message.guild.id) if message.guild else None

        try:
            # ── Step 1: Claim a DB id with a placeholder row ──────────────
            conn = self._conn()
            try:
                cur = conn.execute(
                    """INSERT INTO vms
                       (filename, filepath, discord_message_id, discord_channel_id,
                        guild_id, duration_secs, processed, created_at)
                       VALUES ('__pending__', '', ?, ?, ?, 0.0, 0, ?)""",
                    (str(message.id), str(message.channel.id), guild_id, ts)
                )
                vm_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()

            # ── Step 2: Canonical path is now known ───────────────────────
            canon_name = _vm_canonical_name(vm_id)
            canon_path = self.vms_dir / canon_name

            # ── Step 3: Download and write directly to canonical path ──────
            raw = await attachment.read()
            canon_path.write_bytes(raw)

            # ── Step 4: Get duration, then finalise the DB row ────────────
            duration = await asyncio.get_event_loop().run_in_executor(
                self._executor, _get_ogg_duration, str(canon_path)
            )
            self._db_exec(
                "UPDATE vms SET filename=?, filepath=?, duration_secs=? WHERE id=?",
                (canon_name, str(canon_path), duration, vm_id)
            )

            self.bot.logger.log(MODULE_NAME,
                f"Saved VM #{vm_id}: {canon_name} (guild={guild_id})")
            return vm_id

        except Exception as exc:
            self.bot.logger.error(MODULE_NAME, "Failed to save voice message", exc)
        return None

    # ----------------------------------------------- Retroactive Processing --

    async def process_unprocessed(self):
        """
        Startup scan — four phases:

        1. CONFORM already-processed VMs whose filenames don't match vm-{id}.ogg
           (catches files processed in a previous version of the bot).
        2. REGISTER .ogg files in /data/vms/ not yet in the DB (retroactive
           files — user/message/guild IDs are left NULL, that's expected).
        3. RENAME newly-registered retroactive files to vm-{id}.ogg.
        4. DISPATCH all pending (processed=0) rows to BulkProcessor or the
           live queue depending on backlog size.
        """
        self.bot.logger.log(MODULE_NAME,
            "Startup scan: conforming names and registering new files…")

        # ── Phase 1: Conform already-processed files to canonical naming ─────
        non_canonical = self._db_all(
            """SELECT id, filepath, filename FROM vms
               WHERE processed IN (1, 2)
                 AND filename != ('vm-' || id || '.ogg')"""
        )
        conformed = 0
        for vm_id, fp, old_name in non_canonical:
            old_path = Path(fp)
            try:
                new_path = _rename_to_canonical(old_path, vm_id)
                self._db_exec(
                    "UPDATE vms SET filename=?, filepath=? WHERE id=?",
                    (new_path.name, str(new_path), vm_id)
                )
                conformed += 1
            except Exception as exc:
                self.bot.logger.log(MODULE_NAME,
                    f"Could not conform VM #{vm_id} ({old_name}): {exc}", "WARNING")
        if conformed:
            self.bot.logger.log(MODULE_NAME,
                f"Conformed {conformed} existing VM filename(s) to canonical format")

        # ── Phase 2: Register untracked .ogg files (retroactive, nulls OK) ───
        new = 0
        for ogg in sorted(self.vms_dir.glob("*.ogg")):
            if self._db_one("SELECT id FROM vms WHERE filename=?", (ogg.name,)):
                continue
            if self._db_one("SELECT id FROM vms WHERE filepath=?", (str(ogg),)):
                continue
            mtime = int(ogg.stat().st_mtime)
            dur   = await asyncio.get_event_loop().run_in_executor(
                self._executor, _get_ogg_duration, str(ogg)
            )
            # discord_message_id, discord_channel_id, guild_id intentionally NULL
            self._db_exec(
                """INSERT OR IGNORE INTO vms
                   (filename, filepath, processed, created_at, duration_secs)
                   VALUES (?, ?, 0, ?, ?)""",
                (ogg.name, str(ogg), mtime, dur)
            )
            new += 1
        if new:
            self.bot.logger.log(MODULE_NAME,
                f"Registered {new} retroactive VM(s) "
                f"(discord_message_id / channel_id / guild_id will be NULL)")

        # ── Phase 3: Rename pending files that aren't canonical yet ──────────
        needs_rename = self._db_all(
            """SELECT id, filepath FROM vms
               WHERE processed = 0
                 AND filename != ('vm-' || id || '.ogg')"""
        )
        for vm_id, fp in needs_rename:
            old_path = Path(fp)
            try:
                new_path = _rename_to_canonical(old_path, vm_id)
                self._db_exec(
                    "UPDATE vms SET filename=?, filepath=? WHERE id=?",
                    (new_path.name, str(new_path), vm_id)
                )
            except Exception as exc:
                self.bot.logger.log(MODULE_NAME,
                    f"Rename failed for VM #{vm_id}: {exc}", "WARNING")

        # ── Phase 4: Collect and dispatch all pending rows ────────────────────
        pending = self._db_all(
            "SELECT id, filepath FROM vms WHERE processed=0 AND filepath IS NOT NULL"
        )
        valid = [(vid, fp) for vid, fp in pending if Path(fp).exists()]
        missing = len(pending) - len(valid)
        if missing:
            self.bot.logger.log(MODULE_NAME,
                f"{missing} pending VM(s) have missing files — skipping", "WARNING")

        if not valid:
            self.bot.logger.log(MODULE_NAME, "No pending VMs to process")
            return

        if len(valid) > BULK_BATCH_SIZE:
            self.bot.logger.log(MODULE_NAME,
                f"Large backlog ({len(valid)} files) — launching BulkProcessor")
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start(valid)
        else:
            for vm_id, fp in valid:
                await self.enqueue(vm_id, fp)
            self.bot.logger.log(MODULE_NAME,
                f"Queued {len(valid)} pending VM(s) for transcription")

    # ---------------------------------------------------------- Archive Job --

    async def run_archive_if_due(self):
        row      = self._db_one(
            "SELECT last_run FROM vms_scheduled_jobs WHERE job_name='archive'"
        )
        last_run = row[0] if row else 0
        now      = int(time.time())
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
        """Move VMs ≥150 days → archive; delete VMs ≥365 days."""
        now            = int(time.time())
        archive_cutoff = now - (ARCHIVE_AFTER_DAYS * 86400)
        delete_cutoff  = now - (DELETE_AFTER_DAYS  * 86400)
        archived = deleted = 0

        # Delete (≥365 days)
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

        # Archive (150–365 days)
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
        words = re.findall(r"\b[a-z]{3,}\b", text.lower())
        return {w for w in words if w not in STOP_WORDS}

    def _eligible_vms(self):
        """
        VMs that are transcribed, have an accessible file, and are outside
        their 7-day cooldown.
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
        chat_kw = self._keywords(" ".join(recent_messages))
        if not chat_kw:
            return None

        now    = int(time.time())
        scored = []
        for vm_id, fp, transcript, duration, created_at, _ in self._eligible_vms():
            vm_kw   = self._keywords(transcript)
            overlap = len(chat_kw & vm_kw)
            if overlap == 0:
                continue
            score    = overlap * 10.0
            age_days = (now - created_at) / 86400
            score   += max(0.0, 30.0 - age_days) * 0.1
            if duration > LONG_VM_THRESHOLD_SECS:
                score -= (duration - LONG_VM_THRESHOLD_SECS) * 0.1
            scored.append((score, vm_id, fp, duration))

        if not scored:
            return None
        scored.sort(reverse=True)
        _, vm_id, fp, dur = scored[0]
        return vm_id, fp, dur

    def select_random(self) -> Optional[Tuple[int, str, float]]:
        candidates = []
        for vm_id, fp, _, duration, *_ in self._eligible_vms():
            w = max(10, int(100 - max(0, duration - LONG_VM_THRESHOLD_SECS) * 0.5))
            candidates.append((w, vm_id, fp, duration))

        if not candidates:
            return None

        total     = sum(w for w, *_ in candidates)
        pick      = random.uniform(0, total)
        cumulative = 0
        for w, vm_id, fp, dur in candidates:
            cumulative += w
            if pick <= cumulative:
                return vm_id, fp, dur
        _, vm_id, fp, dur = candidates[-1]
        return vm_id, fp, dur

    async def mark_played(self, vm_id: int):
        self._db_exec(
            """INSERT INTO vms_playback (vm_id, last_played, play_count) VALUES (?, ?, 1)
               ON CONFLICT(vm_id) DO UPDATE SET
                 last_played = excluded.last_played,
                 play_count  = play_count + 1""",
            (vm_id, int(time.time()))
        )

    async def send_vm(self, channel, vm_id: int, filepath: str, duration: float) -> bool:
        """
        Send a VM to a Discord channel as a proper voice message with real waveform.
        Fetches stored waveform from DB; falls back to generating one on the fly.
        """
        try:
            # Pull stored waveform from DB
            row      = self._db_one("SELECT waveform_b64 FROM vms WHERE id=?", (vm_id,))
            waveform = (row[0] if row and row[0] else None) or _generate_waveform(filepath)

            session  = await self._get_session()
            result   = await _send_voice_message(
                self._token, channel.id, filepath, duration, waveform, session
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
        return (row[0], row[1]) if row else (
            0, random.randint(RANDOM_PLAYBACK_MIN, RANDOM_PLAYBACK_MAX)
        )

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
        await asyncio.sleep(0.5)
        await self.process_unprocessed()
        await self.run_archive_if_due()
        self.bot.logger.log(MODULE_NAME, "VMS startup complete")


# ==================== MODULE SETUP ====================

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up VMS module")

    manager         = VMSManager(bot)
    bot.vms_manager = manager

    # Scheduled archive loop (DB-backed, catches missed runs)
    @tasks.loop(hours=ARCHIVE_JOB_INTERVAL_HOURS)
    async def _archive_loop():
        await manager.run_archive_if_due()

    @_archive_loop.before_loop
    async def _before_archive():
        await bot.wait_until_ready()

    _archive_loop.start()

    # One-time startup task
    @bot.listen("on_ready")
    async def _vms_on_ready():
        if not getattr(bot, '_vms_started', False):
            bot._vms_started = True
            await asyncio.sleep(3)   # let other modules finish their on_ready
            await manager.startup()

    # ================================================================
    # MESSAGE HANDLER
    # ================================================================

    @bot.listen()
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        # ── 1. Detect & handle incoming voice messages ──────────────────────
        for att in message.attachments:
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
            bot.logger.log(MODULE_NAME,
                f"Voice message from {message.author} in {source}")

            vm_id = await manager.save_voice_message(message, att)
            if vm_id:
                row = manager._db_one("SELECT filepath FROM vms WHERE id=?", (vm_id,))
                if row:
                    await manager.enqueue(vm_id, row[0], reply_to=message)
            return  # one VM per message is enough

        # ── 2. Ping / reply-to-bot → respond with a VM ──────────────────────
        is_mention  = bot.user in message.mentions
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
                    bot.logger.log(MODULE_NAME,
                        "Ping received but no eligible VMs available")
            else:
                bot.logger.log(MODULE_NAME,
                    f"Ping cooldown active for {message.author} — ignoring")
            return

        # ── 3. #general counter → random / contextual playback ──────────────
        if message.guild and getattr(message.channel, 'name', '') == GENERAL_CHANNEL_NAME:
            guild_id   = str(message.guild.id)
            channel_id = str(message.channel.id)
            count, threshold = manager._inc_counter(guild_id, channel_id)

            if count >= threshold:
                manager._reset_counter(guild_id, channel_id)

                if random.random() < PLAYBACK_CHANCE:
                    if random.random() < 0.5:
                        recent = manager.recent_messages(guild_id, channel_id)
                        vm     = manager.select_contextual(recent)
                        mode   = "contextual"
                    else:
                        vm   = manager.select_random()
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
