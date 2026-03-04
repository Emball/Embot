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
import queue
import random
import re
import shutil
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
BACKFILL_DAYS             = 365       # how far back to scrape on an empty cache

# Bulk-processing concurrency
# Whisper is NOT thread-safe and is compute-bound — running multiple instances
# simultaneously on CPU causes memory exhaustion and process crashes.
# Always use 1 worker regardless of CPU count; the single worker processes
# files sequentially but the event loop remains fully unblocked throughout.
# GPU path: same — Whisper handles its own internal CUDA parallelism.
BULK_GPU_WORKERS  = 1
BULK_CPU_WORKERS  = 1
BULK_BATCH_SIZE      = 16    # transcription results committed to DB per batch
SCAN_BATCH_SIZE      = 256   # rows inserted/updated per commit during startup scan
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

def _broken_dir() -> Path:
    """Corrupt or unprocessable voice message files are moved here."""
    p = _data_dir() / "vms" / "broken"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _whisper_model_dir() -> Path:
    """Whisper model cache directory."""
    p = _data_dir()
    p.mkdir(exist_ok=True)
    return p

# ==================== NAMING CONVENTION ====================

def _vm_canonical_name(
    vm_id: int,
    username: Optional[str] = None,
    message_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> str:
    """
    Return the canonical filename for a VM.

    Format:  vm_{username}_{message_id}_{MM-DD-YY}.ogg
    Example: vm_Embis_1234567890123456789_03-04-25.ogg

    Falls back to vm_{vm_id}.ogg when metadata is unavailable (files
    registered from disk without Discord metadata).

    The filename encodes enough information to reconstruct the DB from disk:
      - username   -> who sent it
      - message_id -> unique Discord snowflake, links back to the original message
      - date       -> when it was sent (for created_at reconstruction)
    """
    if username and message_id and created_at:
        safe_user = re.sub(r'[\\/:*?"<>|]', '', username)[:32].strip() or "unknown"
        date_str  = datetime.fromtimestamp(created_at).strftime("%m-%d-%y")
        return f"vm_{safe_user}_{message_id}_{date_str}.ogg"
    return f"vm_{vm_id}.ogg"


def _parse_vm_filename(filename: str) -> dict:
    """
    Parse metadata out of a canonical VM filename for DB reconstruction.

    Returns a dict with keys: username, message_id, created_at, vm_id.
    Any field that cannot be parsed is None.
    """
    # Rich format: vm_{username}_{message_id}_{MM-DD-YY}.ogg
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

    # Fallback format: vm_{id}.ogg
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
    """
    Rename *current_path* to the canonical name inside the same directory.
    Returns the new Path.  No-ops safely if the file is already canonical,
    or if the source does not exist.
    """
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
    processed           INTEGER DEFAULT 0,  -- 0=pending, 1=done, 2=archived, 3=deleted, 4=broken
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
_whisper_load_failed = False   # set True on first failure — stops retrying


def _load_whisper() -> Optional[object]:
    """
    Load OpenAI Whisper once, storing the model under /data/.
    Prefers CUDA GPU, falls back to CPU.
    Sets _whisper_load_failed=True on any error so workers stop retrying.
    """
    global _whisper_model, _whisper_device, _whisper_load_failed
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_load_failed:
        return None
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        if _whisper_load_failed:
            return None
        try:
            import whisper
            try:
                import torch
                _whisper_device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                _whisper_device = "cpu"
            model_dir = str(_whisper_model_dir())
            print(f"[{MODULE_NAME}] Loading Whisper '{WHISPER_MODEL_SIZE}' "
                  f"on {_whisper_device} (cache: {model_dir})\u2026")
            _whisper_model = whisper.load_model(
                WHISPER_MODEL_SIZE,
                device=_whisper_device,
                download_root=model_dir,
            )
            print(f"[{MODULE_NAME}] Whisper loaded on {_whisper_device}")
        except ImportError:
            print(f"[{MODULE_NAME}] openai-whisper not installed — transcription disabled")
            _whisper_load_failed = True
        except Exception as exc:
            print(f"[{MODULE_NAME}] Whisper load error: {exc}")
            _whisper_load_failed = True
    return _whisper_model

def _quarantine_file(filepath: str) -> str:
    """
    Move a corrupt/unprocessable OGG to /data/vms/broken/.
    Returns the new path as a string.
    """
    src = Path(filepath)
    dst = _broken_dir() / src.name
    try:
        if src.exists():
            try:
                src.rename(dst)
            except (FileExistsError, OSError):
                src.unlink(missing_ok=True)  # dst already exists, drop src
            print(f"[{MODULE_NAME}] Quarantined corrupt file → {dst}")
        return str(dst)
    except Exception as exc:
        print(f"[{MODULE_NAME}] Failed to quarantine {src}: {exc}")
        return filepath


def _is_audio_valid(audio) -> bool:
    """Return False if the audio array is None, empty, or all-zero (silent)."""
    if audio is None or len(audio) == 0:
        return False
    if len(audio) < 1600:          # < 0.1 s at 16 kHz
        return False
    return True


def _process_file_sync(filepath: str) -> Tuple[Optional[str], float, str, bool, str]:
    """
    Full synchronous processing for one OGG file.
    Returns (transcript, duration_secs, waveform_b64, is_broken, actual_filepath).
    actual_filepath is the file's current path on disk — equals filepath normally,
    or the quarantine destination when is_broken=True.
    NEVER raises — all exceptions are caught and result in is_broken=True
    so a worker thread dying can never crash the BulkProcessor or startup task.
    """
    try:
        duration = _get_ogg_duration(filepath)
        waveform = _generate_waveform(filepath)
        model    = _load_whisper()

        if model is None:
            return None, duration, waveform, False, filepath

        import whisper as _whisper

        # ── Load audio ──────────────────────────────────────────────────────
        try:
            audio = _whisper.load_audio(filepath)
        except Exception as exc:
            print(f"[{MODULE_NAME}] Cannot read audio ({filepath}): {exc} — quarantining")
            broken_path = _quarantine_file(filepath)
            return None, 0.0, waveform, True, broken_path

        if not _is_audio_valid(audio):
            print(f"[{MODULE_NAME}] Audio too short/empty ({filepath}) — quarantining")
            broken_path = _quarantine_file(filepath)
            return None, 0.0, waveform, True, broken_path

        # ── Mel spectrogram ─────────────────────────────────────────────────
        try:
            audio = _whisper.pad_or_trim(audio)
            mel   = _whisper.log_mel_spectrogram(audio).to(model.device)
        except Exception as exc:
            print(f"[{MODULE_NAME}] Mel failed ({filepath}): {exc} — quarantining")
            broken_path = _quarantine_file(filepath)
            return None, 0.0, waveform, True, broken_path


        # ── Language detection + decode ──────────────────────────────────────
        # Both calls run the model on the mel tensor — wrap them together so
        # any RuntimeError (reshape, tensor shape mismatch, etc.) is caught
        # regardless of which internal step triggers it.
        try:
            _, probs = model.detect_language(mel)
            options  = _whisper.DecodingOptions(fp16=False)
            result   = _whisper.decode(model, mel, options)
            transcript = (result.text or "").strip() or None
            return transcript, duration, waveform, False, filepath

        except (RuntimeError, ValueError) as exc:
            print(f"[{MODULE_NAME}] Decode failed ({filepath}): {exc} — quarantining")
            broken_path = _quarantine_file(filepath)
            return None, duration, waveform, True, broken_path

        except Exception as exc:
            exc_str = str(exc)
            if "Linear(" in exc_str or "in_features" in exc_str:
                print(f"[{MODULE_NAME}] Whisper internal error ({filepath}) — quarantining")
            else:
                print(f"[{MODULE_NAME}] Transcription error ({filepath}): {exc_str} — quarantining")
            broken_path = _quarantine_file(filepath)
            return None, duration, waveform, True, broken_path

    except Exception as exc:
        # Absolute last resort — should never reach here, but guarantees the
        # worker thread never raises and crashes the executor.
        print(f"[{MODULE_NAME}] Fatal processing error ({filepath}): {exc} — quarantining")
        broken_path = filepath
        try:
            broken_path = _quarantine_file(filepath)
        except Exception:
            pass
        return None, 0.0, "", True, broken_path


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

_BULK_SENTINEL = object()   # pushed into the work queue to signal "no more files"


class BulkProcessor:
    """
    Dedicated background processor for large backlogs of pre-placed OGG files.

    Architecture
    ─────────────
    • Runs entirely in a separate daemon thread — zero asyncio entanglement.
    • Accepts work via a thread-safe queue so producers (startup scan AND
      backfill downloader) can feed files concurrently while processing is
      already in flight — true pipeline parallelism.
    • Call feed(vm_id, filepath) from any thread/coroutine to add work at any time.
    • Call done_feeding() when ALL producers are finished; the worker drains
      whatever remains and exits cleanly.
    • Runs Whisper synchronously in the worker thread — no ThreadPoolExecutor.
      Whisper is not thread-safe; sequential processing is the correct design.
    • Results are committed to SQLite after every single file so a crash loses
      at most the one in-progress transcription, never a whole batch.
    • A threading.Event allows the main bot to signal a graceful shutdown.
    """

    def __init__(self, db_path: str, vms_dir: Path, logger, stop_event: threading.Event):
        self.db_path     = db_path
        self.vms_dir     = vms_dir
        self.logger      = logger
        self.stop_event  = stop_event
        self._work_q: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self, initial_files: Optional[List[Tuple[int, str]]] = None):
        """
        Start the worker thread.  Optionally pre-seed with (vm_id, filepath)
        pairs from the startup scan.  Call done_feeding() once all producers
        are done adding work.
        """
        # Drain any stale sentinels left from a previous run so a restarted
        # processor isn't killed immediately by a leftover _BULK_SENTINEL.
        while not self._work_q.empty():
            try:
                self._work_q.get_nowait()
            except queue.Empty:
                break

        if initial_files:
            for item in initial_files:
                self._work_q.put(item)

        self._thread = threading.Thread(
            target=self._run,
            name="vms_bulk_processor",
            daemon=True,
        )
        self._thread.start()
        self.logger.log(MODULE_NAME,
            f"BulkProcessor started "
            f"(workers={'GPU×' + str(BULK_GPU_WORKERS) if _CUDA_AVAILABLE else 'CPU×' + str(BULK_CPU_WORKERS)})")

    def feed(self, vm_id: int, filepath: str):
        """Push one file onto the work queue — safe to call from any thread."""
        self._work_q.put((vm_id, filepath))

    def done_feeding(self):
        """
        Signal that no more files will be fed.
        The worker will drain the remaining queue then exit.
        """
        self._work_q.put(_BULK_SENTINEL)

    def stop(self):
        """Hard stop — signal the worker to exit after the current batch."""
        self.stop_event.set()
        self._work_q.put(_BULK_SENTINEL)   # unblock queue.get() if idle

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ────────────────────────────────────────────────────────────

    def _run(self):
        """
        Simple sequential worker — correct for Whisper which is not thread-safe.

        Loop:
          1. Block on the queue (up to 0.5s) waiting for the next file.
          2. If the queue is temporarily empty (backfill still downloading),
             keep waiting — never spin-loop.
          3. Process the file synchronously in this thread (Whisper runs here).
          4. Commit the single result to the DB immediately — every transcription
             is persisted before the next one starts, so a crash loses at most
             the one in-progress file, never a whole batch.
          5. Repeat until sentinel or stop_event.
        """
        done   = 0
        errors = 0

        self.logger.log(MODULE_NAME, "BulkProcessor worker started — waiting for files…")

        try:
            while not self.stop_event.is_set():
                # Block waiting for the next item; use a timeout so we can
                # respond to stop_event even if the queue stays empty.
                try:
                    item = self._work_q.get(timeout=0.5)
                except queue.Empty:
                    continue   # queue temporarily dry — backfill still downloading

                if item is _BULK_SENTINEL:
                    break   # producer signalled done

                if self.stop_event.is_set():
                    break

                vm_id, fp = item

                if not Path(fp).exists():
                    self.logger.log(MODULE_NAME,
                        f"BulkProcessor: file missing, skipping VM #{vm_id}", "WARNING")
                    done += 1
                    continue

                # ── Process (blocking — Whisper runs here) ──────────────────
                try:
                    transcript, duration, waveform, is_broken, actual_fp = \
                        _process_file_sync(fp)
                except Exception as exc:
                    self.logger.log(MODULE_NAME,
                        f"BulkProcessor: unexpected error on VM #{vm_id}: {exc}", "WARNING")
                    errors += 1
                    done   += 1
                    continue

                # ── Commit immediately — one row per transcription ───────────
                if is_broken:
                    self._commit_broken([(actual_fp, vm_id)])
                else:
                    self._commit_batch([(vm_id, transcript or "", duration, waveform, actual_fp)])

                done += 1
                if done % BULK_BATCH_SIZE == 0:
                    self.logger.log(MODULE_NAME,
                        f"BulkProcessor: {done} processed ({errors} errors)")

        except Exception as exc:
            self.logger.log(MODULE_NAME,
                f"BulkProcessor: fatal error — {exc}", "ERROR")

        self.logger.log(MODULE_NAME,
            f"BulkProcessor complete: {done} processed, {errors} errors")

    def _commit_batch(self, batch: list):
        """
        Write a batch of transcription results to SQLite.
        Opens its own connection — safe to call from a worker thread.
        batch items: (vm_id, transcript, duration, waveform, filepath)

        Files are already canonically named before entering the queue —
        no rename is needed here.
        Files already in the archive dir are kept at processed=2;
        all others are set to processed=1.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            archive_path = str(_archive_dir())
            try:
                for vm_id, transcript, duration, waveform, filepath in batch:
                    canon_name = Path(filepath).name
                    canon_fp   = filepath

                    # Archived files stay processed=2; all others → 1
                    in_archive = str(Path(filepath).parent) == archive_path
                    new_state  = 2 if in_archive else 1

                    conn.execute(
                        """UPDATE vms
                           SET transcript=?, duration_secs=?, waveform_b64=?,
                               filename=?, filepath=?, processed=?
                           WHERE id=? AND processed=0""",
                        (transcript, duration, waveform,
                         canon_name, canon_fp, new_state, vm_id)
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

    def _commit_broken(self, batch: list):
        """
        Mark broken VM rows as processed=4 and update their filepath to /broken/.
        batch items: (broken_path, vm_id)
        """
        if not batch:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                conn.executemany(
                    "UPDATE vms SET processed=4, filepath=? WHERE id=?",
                    batch,
                )
                conn.commit()
                self.logger.log(MODULE_NAME,
                    f"BulkProcessor: {len(batch)} broken file(s) quarantined")
            finally:
                conn.close()
        except Exception as exc:
            self.logger.log(MODULE_NAME,
                f"BulkProcessor: broken-batch commit error — {exc}", "ERROR")


def _is_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False

# Cached at import time — avoids importing torch on the event loop thread
# when BulkProcessor.start() logs the worker count
_CUDA_AVAILABLE: bool = _is_cuda()


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
        # Executor for live (single-file) transcriptions.
        # Whisper is NOT thread-safe — one worker is correct regardless of CPU count.
        self._executor      = ThreadPoolExecutor(max_workers=1,
                                                 thread_name_prefix="vms_live")
        # Separate executor for the startup scan so it never queues behind
        # live transcription work and cannot block the event loop
        self._scan_executor = ThreadPoolExecutor(max_workers=1,
                                                 thread_name_prefix="vms_scan")
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: Optional[asyncio.Task] = None
        self._bulk_stop       = threading.Event()
        self._bulk_proc: Optional[BulkProcessor] = None
        self._backfill_running = False

        _init_db(self.db_path)

        self._db_exec(
            "INSERT INTO vms_startup_log (startup_time) VALUES (?)",
            (int(time.time()),)
        )
        self.bot.logger.log(MODULE_NAME, "VMSManager initialised")
        self.bot.logger.log(MODULE_NAME,
            f"Paths — vms: {self.vms_dir} | archive: {self.archive_dir} "
            f"| broken: {_broken_dir()} | db: {self.db_path}")

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
                vm_id, filepath, reply_to = await self.queue.get()
                self.bot.logger.log(MODULE_NAME,
                    f"Transcribing VM #{vm_id}: {Path(filepath).name}")
                try:
                    transcript, duration, waveform, is_broken, actual_fp = await asyncio.get_running_loop().run_in_executor(
                        self._executor, _process_file_sync, filepath
                    )

                    if is_broken:
                        # actual_fp is the quarantine destination returned by _process_file_sync
                        self._db_exec(
                            "UPDATE vms SET processed=4, filepath=? WHERE id=?",
                            (actual_fp, vm_id)
                        )
                        self.bot.logger.log(MODULE_NAME,
                            f"VM #{vm_id} marked broken and quarantined")
                        if reply_to is not None:
                            try:
                                await reply_to.reply(
                                    "> ⚠️ This voice message appears to be corrupt and could not be transcribed."
                                )
                            except Exception:
                                pass
                        continue

                    # File is already canonically named (save_voice_message names
                    # it correctly upfront). Just use the current path as-is.
                    canon_fp   = actual_fp
                    canon_name = Path(actual_fp).name

                    in_archive = str(Path(canon_fp).parent) == str(self.archive_dir)
                    new_state  = 2 if in_archive else 1
                    self._db_exec(
                        """UPDATE vms
                           SET transcript=?, duration_secs=?, waveform_b64=?,
                               filename=?, filepath=?, processed=?
                           WHERE id=?""",
                        (transcript or "", duration, waveform,
                         canon_name, canon_fp, new_state, vm_id)
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
        await self.queue.put((vm_id, filepath, reply_to))
        self.bot.logger.log(MODULE_NAME, f"VM #{vm_id} queued for transcription")

    # ------------------------------------------------------- Save Voice Msg --

    async def save_voice_message(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> Optional[int]:
        """
        Download and persist a Discord voice message attachment.

        Naming flow:
          1. Pre-insert a placeholder row to claim the auto-assigned DB id
          2. Derive the canonical filename (vm_{id}.ogg or rich format)
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
            username   = getattr(message.author, 'name', None)
            canon_name = _vm_canonical_name(vm_id, username, str(message.id), ts)
            canon_path = self.vms_dir / canon_name

            # ── Step 3: Download and write directly to canonical path ──────
            raw = await attachment.read()
            canon_path.write_bytes(raw)

            # ── Step 4: Get duration, then finalise the DB row ────────────
            duration = await asyncio.get_running_loop().run_in_executor(
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

    def _scan_and_conform(self) -> list:
        """
        Synchronous worker — safe to run in a thread executor.

        Phase 1: Conform already-processed files to canonical vm_{id}.ogg (or rich) format, batching
                 DB updates (SCAN_BATCH_SIZE rows per commit).
        Phase 2: Register untracked .ogg files from vms/, archive/, broken/.
                 Each file is inserted one at a time to obtain its auto-assigned
                 DB id, immediately renamed to canonical format on disk, and the row
                 updated — so every registered file is canonical from birth and
                 Phase 3 collision problems can never occur.
        Phase 2b: Reset archived-but-untranscribed rows back to pending.

        Returns list of (vm_id, filepath) pairs ready for dispatch.
        """
        scan_dirs = [
            (self.vms_dir,     "vms",     0),
            (self.archive_dir, "archive", 2),
            (_broken_dir(),    "broken",  4),
        ]

        # ── Phase 1: Conform processed/archived/broken filenames ─────────────
        # Build a canonical-name → id map first so we can detect collisions
        # (two rows claiming the same canonical filename) before touching the DB.
        # Collisions happen when duplicate registrations exist from old runs.
        existing_canonical = {
            r[0]: r[1]   # filename → id
            for r in self._db_all("SELECT filename, id FROM vms")
        }

        # Non-canonical = old vm-{id}.ogg format (dash separator, not underscore).
        # New format is vm_{username}_{msgid}_{date}.ogg or vm_{id}.ogg fallback —
        # both start with "vm_". Only files still in the old dash format need conforming.
        non_canonical = self._db_all(
            """SELECT id, filepath, filename FROM vms
               WHERE processed IN (1, 2, 4)
                 AND filename LIKE 'vm-%'"""
        )
        conformed = 0
        dupes_removed = 0
        conn = self._conn()
        try:
            for vm_id, fp, old_name in non_canonical:
                # Fetch metadata from the DB row to build a rich canonical name.
                row = conn.execute(
                    "SELECT discord_message_id, created_at FROM vms WHERE id=?", (vm_id,)
                ).fetchone()
                msg_id   = row[0] if row else None
                ts       = row[1] if row else None
                # Recover username from the existing filename if it's already in
                # rich format (vm_{username}_{msgid}_{date}.ogg); None for old
                # dash-format files, which fall back to vm_{id}.ogg.
                parsed   = _parse_vm_filename(old_name)
                username = parsed.get("username")
                canon_name = _vm_canonical_name(vm_id, username, msg_id, ts)

                if canon_name in existing_canonical and existing_canonical[canon_name] != vm_id:
                    conn.execute("DELETE FROM vms WHERE id=?", (vm_id,))
                    dupes_removed += 1
                    continue
                try:
                    new_path = _rename_to_canonical(Path(fp), vm_id, username, msg_id, ts)
                    conn.execute(
                        "UPDATE vms SET filename=?, filepath=? WHERE id=?",
                        (new_path.name, str(new_path), vm_id)
                    )
                    existing_canonical[canon_name] = vm_id
                    conformed += 1
                except Exception as exc:
                    print(f"[{MODULE_NAME}] Conform warning VM #{vm_id} ({old_name}): {exc}")
            conn.commit()
        finally:
            conn.close()

        if conformed:
            print(f"[{MODULE_NAME}] Conformed {conformed} filename(s) to canonical format")
        if dupes_removed:
            print(f"[{MODULE_NAME}] Removed {dupes_removed} duplicate DB row(s)")

        # ── Phase 2: Register untracked files ────────────────────────────────
        # Insert one row at a time so we get the auto-assigned id immediately,
        # rename the file to canonical format on disk, then update the row.
        # This means every registered file is canonical from the moment it's
        # written — no Phase 3 rename pass needed, no collision logic required.
        existing_paths = {r[0] for r in self._db_all("SELECT filepath FROM vms")}
        # Also track canonical names already in use (from Phase 1 survivors)
        existing_canonical_names = {r[0] for r in self._db_all("SELECT filename FROM vms")}

        new_counts = {}
        batch_update = []   # (canon_name, canon_fp, vm_id) — flushed per SCAN_BATCH_SIZE
        conn = self._conn()
        try:
            for scan_dir, label, proc_state in scan_dirs:
                new = 0
                for ogg in sorted(scan_dir.glob("*.ogg")):
                    # Skip already-registered files (by path or canonical name)
                    if str(ogg) in existing_paths or ogg.name in existing_canonical_names:
                        continue

                    mtime = int(ogg.stat().st_mtime)
                    dur   = _get_ogg_duration(str(ogg))

                    # Insert placeholder to claim an id
                    cur = conn.execute(
                        """INSERT INTO vms (filename, filepath, processed, created_at, duration_secs)
                           VALUES ('__pending__', ?, ?, ?, ?)""",
                        (str(ogg), proc_state, mtime, dur)
                    )
                    vm_id = cur.lastrowid

                    # Rename on disk to canonical immediately (vm_{id}.ogg fallback
                    # or vm_{username}_{msgid}_{date}.ogg if metadata is available)
                    try:
                        new_path = _rename_to_canonical(ogg, vm_id)
                    except Exception as exc:
                        print(f"[{MODULE_NAME}] Rename failed during registration "
                              f"({ogg.name} → canonical): {exc} — keeping original name")
                        new_path = ogg

                    canon_name = new_path.name
                    canon_fp   = str(new_path)

                    # Update the row with the real name/path
                    conn.execute(
                        "UPDATE vms SET filename=?, filepath=? WHERE id=?",
                        (canon_name, canon_fp, vm_id)
                    )

                    existing_paths.add(canon_fp)
                    existing_canonical_names.add(canon_name)
                    new += 1

                    # Commit in batches to avoid holding a huge transaction
                    if new % SCAN_BATCH_SIZE == 0:
                        conn.commit()

                if new:
                    new_counts[label] = new

            conn.commit()
        finally:
            conn.close()

        if new_counts:
            parts = ", ".join(f"{n} in /{l}" for l, n in new_counts.items())
            print(f"[{MODULE_NAME}] Registered untracked files: {parts} "
                  f"(discord metadata NULL for retroactive entries)")

        # ── Phase 2b: Reset archived-but-untranscribed to pending ────────────
        untranscribed = self._db_all(
            """SELECT id FROM vms
               WHERE processed = 2
                 AND (transcript IS NULL OR transcript = '')"""
        )
        reset_ids = [r[0] for r in untranscribed]
        if reset_ids:
            for i in range(0, len(reset_ids), SCAN_BATCH_SIZE):
                chunk = reset_ids[i:i + SCAN_BATCH_SIZE]
                placeholders = ",".join("?" * len(chunk))
                conn = self._conn()
                try:
                    conn.execute(
                        f"UPDATE vms SET processed=0 WHERE id IN ({placeholders})",
                        chunk)
                    conn.commit()
                finally:
                    conn.close()
            print(f"[{MODULE_NAME}] Reset {len(reset_ids)} archived-but-untranscribed VM(s) to pending")


        # ── Collect valid pending rows for Phase 4 ───────────────────────────
        pending = self._db_all(
            "SELECT id, filepath FROM vms WHERE processed=0 AND filepath IS NOT NULL"
        )
        valid   = [(vid, fp) for vid, fp in pending if Path(fp).exists()]
        missing = len(pending) - len(valid)
        if missing:
            print(f"[{MODULE_NAME}] {missing} pending VM(s) have missing files — skipping")
        return valid

    def _db_batch_update(self, query: str, params: list):
        """Execute an executemany update in a single connection/commit."""
        if not params:
            return
        conn = self._conn()
        try:
            conn.executemany(query, params)
            conn.commit()
        finally:
            conn.close()

    async def process_unprocessed(self):
        """
        Startup entry point — offloads all blocking scan/conform/rename work
        to a thread executor so the event loop never freezes, then feeds the
        resulting pending list into the shared BulkProcessor.

        If BulkProcessor isn't running yet (no backfill in progress) this
        method starts it.  Either way it feeds all pending files and signals
        done_feeding() so the worker knows this producer is finished.
        """
        self.bot.logger.log(MODULE_NAME,
            "Startup scan: conforming names and registering files in all dirs…")

        valid = await asyncio.get_running_loop().run_in_executor(
            self._scan_executor, self._scan_and_conform
        )

        if not valid:
            self.bot.logger.log(MODULE_NAME, "No pending VMs to process")
            # If backfill is running it will call done_feeding() when it's done.
            # If there's no backfill and BulkProcessor is somehow already running
            # (shouldn't happen) leave it alone — it'll drain whatever it has.
            return

        self.bot.logger.log(MODULE_NAME,
            f"Scan found {len(valid)} pending VM(s) — feeding BulkProcessor")

        # Start BulkProcessor only if it isn't already running (startup() starts
        # it when doing a concurrent backfill+scan; this covers the scan-only path).
        if self._bulk_proc is None or not self._bulk_proc.is_running():
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start()

        for vm_id, fp in valid:
            self._bulk_proc.feed(vm_id, fp)

        # Only signal done if backfill isn't also producing — if it is,
        # backfill's finally block will call done_feeding() when it finishes.
        if not self._backfill_running:
            self._bulk_proc.done_feeding()

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

    # ---------------------------------------------------------------- Backfill --

    async def _backfill_from_discord(self, resume_after_id: int = None):
        """
        Scrape #general for the last BACKFILL_DAYS of voice messages and
        download any that aren't already in the DB.

        Only runs when /vms is completely empty (fresh install / wiped cache).
        Starts BulkProcessor immediately so transcription begins in parallel
        with downloading — each file is fed to BulkProcessor as soon as it
        lands on disk.  Calls done_feeding() when the download loop finishes
        so the worker knows to drain and exit.
        """
        self._backfill_running = True

        # Find #general in any guild the bot is in
        general_channel = None
        for guild in self.bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL_NAME)
            if ch is not None:
                general_channel = ch
                break

        if general_channel is None:
            self.bot.logger.log(MODULE_NAME,
                "Backfill: could not find #general in any guild — skipping", "WARNING")
            self._backfill_running = False
            return

        from datetime import timezone, timedelta
        cutoff_dt = discord.utils.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=BACKFILL_DAYS)

        if resume_after_id:
            after_obj = discord.Object(id=resume_after_id)
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: resuming after message {resume_after_id} — "
                f"skipping already-known messages via known_ids set")
        else:
            after_obj = cutoff_dt
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: scanning #{general_channel.name} in '{general_channel.guild.name}' "
                f"back to {cutoff_dt.strftime('%Y-%m-%d')} — BulkProcessor will transcribe concurrently…")

        # Load Whisper fully into memory before we start downloading so the
        # BulkProcessor worker isn't sitting idle waiting on model load while
        # files are already piling up in the queue.
        self.bot.logger.log(MODULE_NAME, "Backfill: pre-loading Whisper model…")
        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(self._executor, _load_whisper)
        if model is None:
            self.bot.logger.log(MODULE_NAME,
                "Backfill: Whisper failed to load — transcription will be skipped, "
                "files will still be downloaded", "WARNING")
        else:
            self.bot.logger.log(MODULE_NAME, "Backfill: Whisper ready")

        # Start BulkProcessor now so it's ready to receive files immediately.
        # Skip if startup() already created it (concurrent backfill+scan path).
        if self._bulk_proc is None or not self._bulk_proc.is_running():
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start()

        # Build set of already-known message IDs so we skip them fast
        known_ids = {
            r[0]
            for r in self._db_all("SELECT discord_message_id FROM vms WHERE discord_message_id IS NOT NULL")
        }

        downloaded  = 0
        skipped     = 0
        errors      = 0
        msgs_seen   = 0

        BACKFILL_SLEEP_EVERY = 200
        BACKFILL_SLEEP_SECS  = 2.0
        BACKFILL_DL_SLEEP    = 0.5

        try:
            async for message in general_channel.history(limit=None, after=after_obj, oldest_first=True):
                msgs_seen += 1

                if msgs_seen % BACKFILL_SLEEP_EVERY == 0:
                    self.bot.logger.log(MODULE_NAME,
                        f"Backfill: scanned {msgs_seen} messages "
                        f"({downloaded} downloaded) — pausing {BACKFILL_SLEEP_SECS}s…")
                    await asyncio.sleep(BACKFILL_SLEEP_SECS)

                for att in message.attachments:
                    raw_flags = getattr(message.flags, 'value', 0)
                    is_vm = (
                        bool(raw_flags & 8192) or
                        att.filename.lower() == "voice-message.ogg" or
                        (att.content_type and "ogg" in att.content_type.lower())
                    )
                    if not is_vm:
                        continue

                    msg_id_str = str(message.id)
                    if msg_id_str in known_ids:
                        skipped += 1
                        continue

                    try:
                        ts       = int(message.created_at.timestamp())
                        guild_id = str(message.guild.id) if message.guild else None

                        # Claim a DB id with a placeholder row
                        conn = self._conn()
                        try:
                            cur = conn.execute(
                                """INSERT INTO vms
                                   (filename, filepath, discord_message_id, discord_channel_id,
                                    guild_id, duration_secs, processed, created_at)
                                   VALUES ('__pending__', '', ?, ?, ?, 0.0, 0, ?)""",
                                (msg_id_str, str(message.channel.id), guild_id, ts)
                            )
                            vm_id = cur.lastrowid
                            conn.commit()
                        finally:
                            conn.close()

                        # Write directly to canonical path
                        username   = getattr(message.author, 'name', None)
                        canon_name = _vm_canonical_name(vm_id, username, msg_id_str, ts)
                        canon_path = self.vms_dir / canon_name
                        raw        = await att.read()
                        canon_path.write_bytes(raw)

                        # Get duration (offloaded — mutagen is blocking file I/O)
                        # and finalise the DB row before feeding to BulkProcessor.
                        duration = await asyncio.get_running_loop().run_in_executor(
                            None, _get_ogg_duration, str(canon_path)
                        )
                        self._db_exec(
                            "UPDATE vms SET filename=?, filepath=?, duration_secs=? WHERE id=?",
                            (canon_name, str(canon_path), duration, vm_id)
                        )

                        # Feed immediately to BulkProcessor — transcription starts now
                        self._bulk_proc.feed(vm_id, str(canon_path))

                        known_ids.add(msg_id_str)
                        downloaded += 1

                        if downloaded % 100 == 0:
                            self.bot.logger.log(MODULE_NAME,
                                f"Backfill: {downloaded} downloaded so far…")

                        await asyncio.sleep(BACKFILL_DL_SLEEP)

                    except Exception as exc:
                        self.bot.logger.log(MODULE_NAME,
                            f"Backfill: failed to download message {message.id}: {exc}", "WARNING")
                        errors += 1

        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: no permission to read #{general_channel.name} — skipping", "WARNING")
        except Exception as exc:
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: history scrape error — {exc}", "ERROR")
        finally:
            self._backfill_running = False
            # Signal BulkProcessor that backfill is done producing.
            # process_unprocessed() may also call done_feeding() if it finishes
            # after us, but done_feeding() just pushes a sentinel — harmless to
            # push more than one since the worker exits on the first.
            if self._bulk_proc and self._bulk_proc.is_running():
                self._bulk_proc.done_feeding()

        self.bot.logger.log(MODULE_NAME,
            f"Backfill complete: {downloaded} downloaded, {skipped} already known, {errors} errors")

    # ---------------------------------------------------------------- Startup --

    async def startup(self):
        """
        Full startup sequence:
          1. Queue worker (live VMs)
          2a. If /vms is empty — backfill from Discord AND scan concurrently,
              both feeding a single BulkProcessor that transcribes as files arrive.
          2b. Otherwise — scan only, feed BulkProcessor as normal.
          3. Archive check.
        """
        await self._start_queue_worker()
        await asyncio.sleep(0.5)

        has_files    = any(self.vms_dir.glob("*.ogg"))
        last_msg_row = self._db_one(
            "SELECT MAX(CAST(discord_message_id AS INTEGER)) FROM vms WHERE discord_message_id IS NOT NULL"
        )
        last_msg_id = last_msg_row[0] if last_msg_row and last_msg_row[0] else None

        if not has_files or last_msg_id:
            if last_msg_id and has_files:
                self.bot.logger.log(MODULE_NAME,
                    f"Resuming backfill from last known message ID {last_msg_id}…")
            else:
                self.bot.logger.log(MODULE_NAME,
                    "Cache is empty — starting Discord backfill and scan concurrently…")
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start()
            await asyncio.gather(
                self._backfill_from_discord(resume_after_id=last_msg_id),
                self.process_unprocessed(),
            )
        else:
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

    # One-time startup task — runs after bot is ready, regardless of when
    # setup() was called relative to on_ready firing.
    @tasks.loop(count=1)
    async def _startup_task():
        await manager.startup()

    @_startup_task.before_loop
    async def _before_startup():
        await bot.wait_until_ready()
        await asyncio.sleep(3)   # let other modules finish their on_ready

    _startup_task.start()

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