"""
VMS — Voice Message System for Embot
• Saves/transcribes Discord voice messages via OpenAI Whisper
• Stores transcripts in SQLite (db/vms.db), audio in cache/vms/
• Archives after 150 days, deletes after 365 days
• Periodic random/contextual playback in #general, replies to @mentions
• User-installable app: right-click any VM → Apps → Transcribe VM
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
WHISPER_MODEL_SIZE        = "base"    # tiny / base / small / medium / large
BACKFILL_DAYS             = 365

# Emball guild ID — VMs from here are stored persistently; all others are ephemeral
EMBALL_GUILD_ID: Optional[int] = int(os.getenv("EMBALL_GUILD_ID", "0")) or None

# Whisper is NOT thread-safe — always 1 worker regardless of CPU/GPU count
BULK_GPU_WORKERS  = 1
BULK_CPU_WORKERS  = 1
BULK_BATCH_SIZE   = 16    # progress log interval
SCAN_BATCH_SIZE   = 256   # rows per commit during startup scan
WAVEFORM_SAMPLES  = 256   # Discord expects 256-byte waveform

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
    """Root Embot/ directory (two levels up from modules/)."""
    return Path(__file__).parent.parent.absolute()

def _vms_dir() -> Path:
    """Voice message audio files live here."""
    p = _script_dir() / "cache" / "vms"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _archive_dir() -> Path:
    """Archived voice messages live here."""
    p = _script_dir() / "cache" / "vms" / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _db_path() -> str:
    """SQLite database path."""
    p = _script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "vms.db")

def _broken_dir() -> Path:
    """Corrupt or unprocessable voice message files are moved here."""
    p = _script_dir() / "cache" / "vms" / "broken"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _whisper_model_dir() -> Path:
    """Whisper model cache directory."""
    p = _script_dir() / "cache" / "whisper_models"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _temp_vms_dir() -> Path:
    """Temp dir for external (non-Emball) VM transcriptions — files deleted after use."""
    p = _script_dir() / "cache" / "vms" / "temp"
    p.mkdir(parents=True, exist_ok=True)
    return p

# ==================== NAMING CONVENTION ====================

def _vm_canonical_name(
    vm_id: int,
    username: Optional[str] = None,
    message_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> str:
    """
    Returns vm_{username}_{message_id}_{MM-DD-YY}.ogg, or vm_{vm_id}.ogg
    if metadata is unavailable.
    """
    if username and message_id and created_at:
        safe_user = re.sub(r'[\\/:*?"<>|]', '', username)[:32].strip() or "unknown"
        date_str  = datetime.fromtimestamp(created_at).strftime("%m-%d-%y")
        return f"vm_{safe_user}_{message_id}_{date_str}.ogg"
    return f"vm_{vm_id}.ogg"


def _parse_vm_filename(filename: str) -> dict:
    """Parse metadata out of a canonical VM filename. Returns dict with username, message_id, created_at, vm_id."""
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
    """Rename current_path to its canonical name in the same directory. No-ops if already canonical or file missing."""
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

CREATE TABLE IF NOT EXISTS vms_kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
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
    """Generate base64 waveform bytes for Discord. Tries soundfile, then pydub, then fallback."""
    # Try soundfile
    try:
        import soundfile as sf
        data, _ = sf.read(filepath, dtype="float32", always_2d=True)
        samples = _downsample_to_waveform(data[:, 0], num_samples)
        return base64.b64encode(bytes(samples)).decode()
    except Exception:
        pass

    # Try pydub
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(filepath)
        raw = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32)
        raw /= 32768.0
        samples = _downsample_to_waveform(raw, num_samples)
        return base64.b64encode(bytes(samples)).decode()
    except Exception:
        pass

    return _fallback_waveform(num_samples)


def _downsample_to_waveform(pcm: np.ndarray, num_samples: int) -> list:
    """Compress float32 PCM to num_samples amplitude bytes using RMS per chunk."""
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
            result.append(int(min(255, rms * 364)))

    smoothed = result[:]
    for i in range(1, len(result) - 1):
        smoothed[i] = int((result[i - 1] + result[i] * 2 + result[i + 1]) / 4)
    return smoothed


def _fallback_waveform(num_samples: int) -> str:
    """Bell-curve waveform fallback when audio libraries are unavailable."""
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
    """Load Whisper once (lazy, thread-safe). Prefers CUDA, falls back to CPU."""
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
    """Move a corrupt OGG to the broken dir. Returns the filename."""
    src = Path(filepath)
    dst = _broken_dir() / src.name
    try:
        if src.exists():
            try:
                src.rename(dst)
            except (FileExistsError, OSError):
                src.unlink(missing_ok=True)  # dst already exists, drop src
            print(f"[{MODULE_NAME}] Quarantined corrupt file → {dst}")
        return src.name
    except Exception as exc:
        print(f"[{MODULE_NAME}] Failed to quarantine {src}: {exc}")
        return src.name


def _is_audio_valid(audio) -> bool:
    """Return False if the audio array is None, empty, or all-zero (silent)."""
    if audio is None or len(audio) == 0:
        return False
    if len(audio) < 1600:          # < 0.1 s at 16 kHz
        return False
    return True


def _process_file_sync(filepath: str) -> Tuple[Optional[str], float, str, bool, str]:
    """
    Transcribe one OGG file synchronously.
    Returns (transcript, duration_secs, waveform_b64, is_broken, actual_filename).
    Never raises — all errors result in is_broken=True.
    """
    try:
        duration = _get_ogg_duration(filepath)
        waveform = _generate_waveform(filepath)
        model    = _load_whisper()

        if model is None:
            return None, duration, waveform, False, Path(filepath).name

        import whisper as _whisper

        try:
            audio = _whisper.load_audio(filepath)
        except Exception as exc:
            print(f"[{MODULE_NAME}] Cannot read audio ({filepath}): {exc} — quarantining")
            return None, 0.0, waveform, True, _quarantine_file(filepath)

        if not _is_audio_valid(audio):
            print(f"[{MODULE_NAME}] Audio too short/empty ({filepath}) — quarantining")
            return None, 0.0, waveform, True, _quarantine_file(filepath)

        try:
            audio = _whisper.pad_or_trim(audio)
            mel   = _whisper.log_mel_spectrogram(audio).to(model.device)
        except Exception as exc:
            print(f"[{MODULE_NAME}] Mel failed ({filepath}): {exc} — quarantining")
            return None, 0.0, waveform, True, _quarantine_file(filepath)

        try:
            _, probs = model.detect_language(mel)
            options  = _whisper.DecodingOptions(fp16=False)
            result   = _whisper.decode(model, mel, options)
            transcript = (result.text or "").strip() or None
            return transcript, duration, waveform, False, Path(filepath).name

        except (RuntimeError, ValueError) as exc:
            print(f"[{MODULE_NAME}] Decode failed ({filepath}): {exc} — quarantining")
            return None, duration, waveform, True, _quarantine_file(filepath)

        except Exception as exc:
            exc_str = str(exc)
            if "Linear(" in exc_str or "in_features" in exc_str:
                print(f"[{MODULE_NAME}] Whisper internal error ({filepath}) — quarantining")
            else:
                print(f"[{MODULE_NAME}] Transcription error ({filepath}): {exc_str} — quarantining")
            return None, duration, waveform, True, _quarantine_file(filepath)

    except Exception as exc:
        print(f"[{MODULE_NAME}] Fatal processing error ({filepath}): {exc} — quarantining")
        broken_name = Path(filepath).name
        try:
            broken_name = _quarantine_file(filepath)
        except Exception:
            pass
        return None, 0.0, "", True, broken_name


def _transcribe_with_model(filepath: str, model_size: str) -> Tuple[Optional[str], float, bool]:
    """Transcribe with a specific model size. Returns (transcript, duration, is_broken). Does not touch the DB."""
    global _whisper_model, _whisper_device, _whisper_load_failed

    try:
        duration = _get_ogg_duration(filepath)
        import whisper as _whisper
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"

        model_dir = str(_whisper_model_dir())
        with _whisper_lock:
            if _whisper_model is None or getattr(_whisper_model, '_vms_size', None) != model_size:
                try:
                    m = _whisper.load_model(model_size, device=device, download_root=model_dir)
                    m._vms_size = model_size
                    _whisper_model  = m
                    _whisper_device = device
                    _whisper_load_failed = False
                except Exception:
                    return None, duration, True
            model = _whisper_model

        audio = _whisper.load_audio(filepath)
        if not _is_audio_valid(audio):
            return None, duration, True

        audio  = _whisper.pad_or_trim(audio)
        mel    = _whisper.log_mel_spectrogram(audio).to(model.device)
        _, _   = model.detect_language(mel)
        opts   = _whisper.DecodingOptions(fp16=False)
        result = _whisper.decode(model, mel, opts)
        return (result.text or "").strip() or None, duration, False

    except Exception:
        return None, 0.0, True


# ==================== DISCORD VOICE MESSAGE API ====================

async def _send_voice_message(
    bot_token: str,
    channel_id: int,
    ogg_path: str,
    duration_secs: float,
    waveform_b64: str,
    session: aiohttp.ClientSession,
) -> Optional[dict]:
    """Upload an OGG and post it as a Discord voice message (flags: 8192)."""
    ogg_bytes = Path(ogg_path).read_bytes()
    file_size = len(ogg_bytes)

    headers_json = {
        "Content-Type": "application/json",
        "Authorization": f"Bot {bot_token}",
    }

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
    Background thread processor for large OGG backlogs.
    Feed files via feed(vm_id, filepath), call done_feeding() when finished.
    Whisper runs sequentially (not thread-safe); results are committed per-file.
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
        """Start the worker thread, optionally pre-seeding with (vm_id, filepath) pairs."""
        # Drain stale sentinels from a previous run
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
        """Signal that no more files will be fed; worker drains and exits."""
        self._work_q.put(_BULK_SENTINEL)

    def stop(self):
        """Hard stop — signal the worker to exit after the current batch."""
        self.stop_event.set()
        self._work_q.put(_BULK_SENTINEL)   # unblock queue.get() if idle

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ────────────────────────────────────────────────────────────

    def _run(self):
        """Sequential worker loop. Blocks on queue, processes files, commits results one-by-one."""
        done   = 0
        errors = 0

        self.logger.log(MODULE_NAME, "BulkProcessor worker started — waiting for files…")

        try:
            while not self.stop_event.is_set():
                try:
                    item = self._work_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if item is _BULK_SENTINEL:
                    break

                if self.stop_event.is_set():
                    break

                vm_id, fp = item

                if not Path(fp).exists():
                    self.logger.log(MODULE_NAME,
                        f"BulkProcessor: file missing, skipping VM #{vm_id}", "WARNING")
                    done += 1
                    continue

                try:
                    transcript, duration, waveform, is_broken, actual_filename = \
                        _process_file_sync(fp)
                except Exception as exc:
                    self.logger.log(MODULE_NAME,
                        f"BulkProcessor: unexpected error on VM #{vm_id}: {exc}", "WARNING")
                    errors += 1
                    done   += 1
                    continue

                if is_broken:
                    self._commit_broken([(actual_filename, vm_id)])
                else:
                    self._commit_batch([(vm_id, transcript or "", duration, waveform, actual_filename)])

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
        """Write transcription results to SQLite. Archived files stay at processed=2; others → 1."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            archive_name_set = {p.name for p in _archive_dir().glob("*.ogg")}
            try:
                for vm_id, transcript, duration, waveform, filename in batch:
                    new_state = 2 if filename in archive_name_set else 1
                    conn.execute(
                        """UPDATE vms
                           SET transcript=?, duration_secs=?, waveform_b64=?,
                               filename=?, processed=?
                           WHERE id=? AND processed=0""",
                        (transcript, duration, waveform, filename, new_state, vm_id)
                    )
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
        """Mark broken VM rows as processed=4."""
        if not batch:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                conn.executemany(
                    "UPDATE vms SET processed=4, filename=? WHERE id=?",
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

_CUDA_AVAILABLE: bool = _is_cuda()


# ==================== VMS MANAGER ====================

class VMSManager:
    """Central manager: storage, transcription queuing, archiving, playback."""

    def __init__(self, bot):
        self.bot         = bot
        self.db_path     = _db_path()
        self.vms_dir     = _vms_dir()
        self.archive_dir = _archive_dir()
        self._token: str = os.getenv("DISCORD_BOT_TOKEN", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._executor      = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vms_live")
        self._scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vms_scan")
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: Optional[asyncio.Task] = None
        self._bulk_stop       = threading.Event()
        self._bulk_proc: Optional[BulkProcessor] = None
        self._backfill_running = False

        _init_db(self.db_path)
        self._migrate_filepath_column()

        self._db_exec(
            "INSERT INTO vms_startup_log (startup_time) VALUES (?)",
            (int(time.time()),)
        )
        self.bot.logger.log(MODULE_NAME, "VMSManager initialised")
        self.bot.logger.log(MODULE_NAME,
            f"Paths — vms: {self.vms_dir} | archive: {self.archive_dir} "
            f"| broken: {_broken_dir()} | db: {self.db_path}")

    # -------------------------------------------------------- Migration ------

    def _migrate_filepath_column(self):
        """Drop legacy filepath column from old DBs (paths are now resolved at runtime)."""
        conn = self._conn()
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(vms)").fetchall()]
            if "filepath" not in cols:
                return

            self.bot.logger.log(MODULE_NAME,
                "Old DB detected: dropping filepath column…")

            # Use table rebuild for SQLite <3.35 compatibility
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

    # ------------------------------------------------------- Path resolver ---

    def _resolve_path(self, filename: str) -> Optional[Path]:
        """Find a VM file across known dirs (vms/, archive/, broken/). Returns Path or None."""
        if not filename or filename == "__pending__":
            return None
        for d in (self.vms_dir, self.archive_dir, _broken_dir()):
            p = d / filename
            if p.exists():
                return p
        return None

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

    # -------------------------------------------------- Backfill Checkpoint --

    def _save_backfill_checkpoint(self, last_message_id: int):
        """Persist the last successfully scanned Discord message ID."""
        self._db_exec(
            "INSERT OR REPLACE INTO vms_kv (key, value) VALUES ('backfill_checkpoint', ?)",
            (str(last_message_id),)
        )

    def _load_backfill_checkpoint(self) -> Optional[int]:
        """Return the last checkpointed Discord message ID, or None if none exists."""
        row = self._db_one("SELECT value FROM vms_kv WHERE key='backfill_checkpoint'")
        return int(row[0]) if row else None

    def _clear_backfill_checkpoint(self):
        """Remove the checkpoint once backfill has completed successfully."""
        self._db_exec("DELETE FROM vms_kv WHERE key='backfill_checkpoint'")

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
        """Process live voice messages one at a time via run_in_executor."""
        while True:
            try:
                vm_id, filepath, reply_to = await self.queue.get()
                self.bot.logger.log(MODULE_NAME,
                    f"Transcribing VM #{vm_id}: {Path(filepath).name}")
                try:
                    transcript, duration, waveform, is_broken, actual_filename = await asyncio.get_running_loop().run_in_executor(
                        self._executor, _process_file_sync, filepath
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
                                    "> ⚠️ This voice message appears to be corrupt and could not be transcribed."
                                )
                            except Exception:
                                pass
                        continue

                    in_archive = (self._resolve_path(actual_filename) or Path("")).parent == self.archive_dir
                    new_state  = 2 if in_archive else 1
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
        """Add a live voice message to the transcription queue."""
        await self.queue.put((vm_id, filepath, reply_to))
        self.bot.logger.log(MODULE_NAME, f"VM #{vm_id} queued for transcription")

    # ------------------------------------------------------- Save Voice Msg --

    async def save_voice_message(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> Optional[int]:
        """Download and persist a voice message. Returns the DB row id or None on failure."""
        ts       = int(time.time())
        guild_id = str(message.guild.id) if message.guild else None

        try:
            conn = self._conn()
            try:
                cur = conn.execute(
                    """INSERT INTO vms
                       (filename, discord_message_id, discord_channel_id,
                        guild_id, duration_secs, processed, created_at)
                       VALUES ('__pending__', ?, ?, ?, 0.0, 0, ?)""",
                    (str(message.id), str(message.channel.id), guild_id, ts)
                )
                vm_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()

            username   = getattr(message.author, 'name', None)
            canon_name = _vm_canonical_name(vm_id, username, str(message.id), ts)
            canon_path = self.vms_dir / canon_name

            raw = await attachment.read()
            canon_path.write_bytes(raw)

            duration = await asyncio.get_running_loop().run_in_executor(
                self._executor, _get_ogg_duration, str(canon_path)
            )
            self._db_exec(
                "UPDATE vms SET filename=?, duration_secs=? WHERE id=?",
                (canon_name, duration, vm_id)
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
        Synchronous startup scan (run in executor).
        Phase 1: Conform existing filenames to canonical format.
        Phase 2: Register untracked OGG files.
        Phase 2b: Reset archived-but-untranscribed rows to pending.
        Returns list of (vm_id, filepath) pairs ready for BulkProcessor.
        """
        scan_dirs = [
            (self.vms_dir,     "vms",     0),
            (self.archive_dir, "archive", 2),
            (_broken_dir(),    "broken",  4),
        ]

        existing_canonical = {
            r[0]: r[1]
            for r in self._db_all("SELECT filename, id FROM vms")
        }

        non_canonical = self._db_all(
            """SELECT id, filename FROM vms
               WHERE processed IN (1, 2, 4)
                 AND filename LIKE 'vm-%'"""
        )
        conformed = 0
        dupes_removed = 0
        conn = self._conn()
        try:
            for vm_id, old_name in non_canonical:
                row = conn.execute(
                    "SELECT discord_message_id, created_at FROM vms WHERE id=?", (vm_id,)
                ).fetchone()
                msg_id   = row[0] if row else None
                ts       = row[1] if row else None
                parsed   = _parse_vm_filename(old_name)
                username = parsed.get("username")
                canon_name = _vm_canonical_name(vm_id, username, msg_id, ts)

                if canon_name in existing_canonical and existing_canonical[canon_name] != vm_id:
                    conn.execute("DELETE FROM vms WHERE id=?", (vm_id,))
                    dupes_removed += 1
                    continue

                old_path = None
                for d in (self.vms_dir, self.archive_dir, _broken_dir()):
                    candidate = d / old_name
                    if candidate.exists():
                        old_path = candidate
                        break

                if old_path:
                    try:
                        new_path = _rename_to_canonical(old_path, vm_id, username, msg_id, ts)
                        conn.execute(
                            "UPDATE vms SET filename=? WHERE id=?",
                            (new_path.name, vm_id)
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

        existing_canonical_names = {r[0] for r in self._db_all("SELECT filename FROM vms")}

        new_counts = {}
        conn = self._conn()
        try:
            for scan_dir, label, proc_state in scan_dirs:
                new = 0
                for ogg in sorted(scan_dir.glob("*.ogg")):
                    if ogg.name in existing_canonical_names:
                        continue

                    mtime = int(ogg.stat().st_mtime)
                    dur   = _get_ogg_duration(str(ogg))

                    cur = conn.execute(
                        """INSERT INTO vms (filename, processed, created_at, duration_secs)
                           VALUES ('__pending__', ?, ?, ?)""",
                        (proc_state, mtime, dur)
                    )
                    vm_id = cur.lastrowid

                    try:
                        new_path = _rename_to_canonical(ogg, vm_id)
                    except Exception as exc:
                        print(f"[{MODULE_NAME}] Rename failed during registration "
                              f"({ogg.name} → canonical): {exc} — keeping original name")
                        new_path = ogg

                    conn.execute(
                        "UPDATE vms SET filename=? WHERE id=?",
                        (new_path.name, vm_id)
                    )

                    existing_canonical_names.add(new_path.name)
                    new += 1

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

        pending = self._db_all(
            "SELECT id, filename FROM vms WHERE processed=0 AND filename != '__pending__'"
        )
        valid   = [(vid, str(self._resolve_path(fn))) for vid, fn in pending
                   if self._resolve_path(fn) is not None]
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
        """Scan/conform files then feed pending ones into BulkProcessor."""
        self.bot.logger.log(MODULE_NAME,
            "Startup scan: conforming names and registering files in all dirs…")

        valid = await asyncio.get_running_loop().run_in_executor(
            self._scan_executor, self._scan_and_conform
        )

        if not valid:
            self.bot.logger.log(MODULE_NAME, "No pending VMs to process")
            return

        self.bot.logger.log(MODULE_NAME,
            f"Scan found {len(valid)} pending VM(s) — feeding BulkProcessor")

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

        for vm_id, fn in self._db_all(
            "SELECT id, filename FROM vms WHERE created_at < ? AND processed != 3",
            (delete_cutoff,)
        ):
            try:
                p = self._resolve_path(fn)
                if p and p.exists():
                    p.unlink()
                self._db_exec(
                    "UPDATE vms SET processed=3, deleted_at=? WHERE id=?",
                    (now, vm_id)
                )
                deleted += 1
            except Exception as exc:
                self.bot.logger.error(MODULE_NAME, f"Failed to delete VM #{vm_id}", exc)

        for vm_id, fn in self._db_all(
            """SELECT id, filename FROM vms
               WHERE created_at < ? AND created_at >= ? AND processed=1""",
            (archive_cutoff, delete_cutoff)
        ):
            try:
                src = self._resolve_path(fn)
                dst = self.archive_dir / fn
                if src and src.exists() and not dst.exists():
                    src.rename(dst)
                self._db_exec(
                    "UPDATE vms SET processed=2, archived_at=? WHERE id=?",
                    (now, vm_id)
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
            """SELECT v.id, v.filename, v.transcript, v.duration_secs, v.created_at,
                      COALESCE(p.last_played, 0)
               FROM vms v
               LEFT JOIN vms_playback p ON v.id = p.vm_id
               WHERE v.processed IN (1, 2)
                 AND v.transcript IS NOT NULL
                 AND v.transcript != ''
                 AND COALESCE(p.last_played, 0) < ?""",
            (cutoff,)
        )
        result = []
        for r in rows:
            p = self._resolve_path(r[1])
            if p is not None:
                result.append((r[0], str(p), r[2], r[3], r[4], r[5]))
        return result

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
        """Send a VM to a channel as a Discord voice message."""
        try:
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

    async def _backfill_from_discord(self, resume_after_id: Optional[int] = None):
        """Scrape #general for the last BACKFILL_DAYS of VMs and download any not already in the DB."""
        self._backfill_running = True

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
            resume_dt = discord.utils.snowflake_time(resume_after_id)
            scan_after = resume_dt
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: RESUMING after message {resume_after_id} "
                f"({resume_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}) in #{general_channel.name}…")
        else:
            scan_after = cutoff_dt
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: scanning #{general_channel.name} in '{general_channel.guild.name}' "
                f"back to {cutoff_dt.strftime('%Y-%m-%d')}…")

        self.bot.logger.log(MODULE_NAME, "Backfill: pre-loading Whisper model…")
        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(self._executor, _load_whisper)
        if model is None:
            self.bot.logger.log(MODULE_NAME,
                "Backfill: Whisper failed to load — files will still be downloaded", "WARNING")
        else:
            self.bot.logger.log(MODULE_NAME, "Backfill: Whisper ready")

        if self._bulk_proc is None or not self._bulk_proc.is_running():
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start()

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
            async for message in general_channel.history(limit=None, after=scan_after, oldest_first=True):
                msgs_seen += 1

                if message.author.bot:
                    self._save_backfill_checkpoint(message.id)
                    continue

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

                        conn = self._conn()
                        try:
                            cur = conn.execute(
                                """INSERT INTO vms
                                   (filename, discord_message_id, discord_channel_id,
                                    guild_id, duration_secs, processed, created_at)
                                   VALUES ('__pending__', ?, ?, ?, 0.0, 0, ?)""",
                                (msg_id_str, str(message.channel.id), guild_id, ts)
                            )
                            vm_id = cur.lastrowid
                            conn.commit()
                        finally:
                            conn.close()

                        username   = getattr(message.author, 'name', None)
                        canon_name = _vm_canonical_name(vm_id, username, msg_id_str, ts)
                        canon_path = self.vms_dir / canon_name
                        raw        = await att.read()
                        canon_path.write_bytes(raw)

                        duration = await asyncio.get_running_loop().run_in_executor(
                            None, _get_ogg_duration, str(canon_path)
                        )
                        self._db_exec(
                            "UPDATE vms SET filename=?, duration_secs=? WHERE id=?",
                            (canon_name, duration, vm_id)
                        )

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

                self._save_backfill_checkpoint(message.id)

        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: no permission to read #{general_channel.name} — skipping", "WARNING")
        except Exception as exc:
            self.bot.logger.log(MODULE_NAME,
                f"Backfill: history scrape error — {exc}", "ERROR")
        finally:
            self._backfill_running = False
            if self._bulk_proc and self._bulk_proc.is_running():
                self._bulk_proc.done_feeding()

        self.bot.logger.log(MODULE_NAME,
            f"Backfill complete: {downloaded} downloaded, {skipped} already known, {errors} errors")
        self._clear_backfill_checkpoint()

    # --------------------------------------------------------- Date Backfill --

    async def _backfill_from_date(self, since_dt):
        """Like _backfill_from_discord but starts from an explicit datetime. Used by vms-resume."""
        self._backfill_running = True

        general_channel = None
        for guild in self.bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL_NAME)
            if ch is not None:
                general_channel = ch
                break

        if general_channel is None:
            self.bot.logger.log(MODULE_NAME,
                "vms-resume: could not find #general in any guild — aborting", "WARNING")
            self._backfill_running = False
            return

        self.bot.logger.log(MODULE_NAME,
            f"vms-resume: scanning #{general_channel.name} in '{general_channel.guild.name}' "
            f"from {since_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} onwards…")

        self.bot.logger.log(MODULE_NAME, "vms-resume: pre-loading Whisper model…")
        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(self._executor, _load_whisper)
        if model is None:
            self.bot.logger.log(MODULE_NAME,
                "vms-resume: Whisper failed to load — files will still be downloaded", "WARNING")
        else:
            self.bot.logger.log(MODULE_NAME, "vms-resume: Whisper ready")

        if self._bulk_proc is None or not self._bulk_proc.is_running():
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start()

        known_ids = {
            r[0]
            for r in self._db_all(
                "SELECT discord_message_id FROM vms WHERE discord_message_id IS NOT NULL"
            )
        }

        downloaded = 0
        skipped    = 0
        errors     = 0
        msgs_seen  = 0

        BACKFILL_SLEEP_EVERY = 200
        BACKFILL_SLEEP_SECS  = 2.0
        BACKFILL_DL_SLEEP    = 0.5

        try:
            async for message in general_channel.history(
                limit=None, after=since_dt, oldest_first=True
            ):
                msgs_seen += 1

                if message.author.bot:
                    self._save_backfill_checkpoint(message.id)
                    continue

                if msgs_seen % BACKFILL_SLEEP_EVERY == 0:
                    self.bot.logger.log(MODULE_NAME,
                        f"vms-resume: scanned {msgs_seen} messages "
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

                        conn = self._conn()
                        try:
                            cur = conn.execute(
                                """INSERT INTO vms
                                   (filename, discord_message_id, discord_channel_id,
                                    guild_id, duration_secs, processed, created_at)
                                   VALUES ('__pending__', ?, ?, ?, 0.0, 0, ?)""",
                                (msg_id_str, str(message.channel.id), guild_id, ts)
                            )
                            vm_id = cur.lastrowid
                            conn.commit()
                        finally:
                            conn.close()

                        username   = getattr(message.author, 'name', None)
                        canon_name = _vm_canonical_name(vm_id, username, msg_id_str, ts)
                        canon_path = self.vms_dir / canon_name
                        raw        = await att.read()
                        canon_path.write_bytes(raw)

                        duration = await asyncio.get_running_loop().run_in_executor(
                            None, _get_ogg_duration, str(canon_path)
                        )
                        self._db_exec(
                            "UPDATE vms SET filename=?, duration_secs=? WHERE id=?",
                            (canon_name, duration, vm_id)
                        )

                        self._bulk_proc.feed(vm_id, str(canon_path))
                        known_ids.add(msg_id_str)
                        downloaded += 1

                        if downloaded % 100 == 0:
                            self.bot.logger.log(MODULE_NAME,
                                f"vms-resume: {downloaded} downloaded so far…")

                        await asyncio.sleep(BACKFILL_DL_SLEEP)

                    except Exception as exc:
                        self.bot.logger.log(MODULE_NAME,
                            f"vms-resume: failed to download message {message.id}: {exc}",
                            "WARNING")
                        errors += 1

                self._save_backfill_checkpoint(message.id)

        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME,
                f"vms-resume: no permission to read #{general_channel.name} — aborting",
                "WARNING")
        except Exception as exc:
            self.bot.logger.log(MODULE_NAME,
                f"vms-resume: history scrape error — {exc}", "ERROR")
        finally:
            self._backfill_running = False
            if self._bulk_proc and self._bulk_proc.is_running():
                self._bulk_proc.done_feeding()

        self.bot.logger.log(MODULE_NAME,
            f"vms-resume complete: {downloaded} downloaded, "
            f"{skipped} already known, {errors} errors")
        self._clear_backfill_checkpoint()

    # -------------------------------------------------------- Bot-VM Purge ------

    def _purge_bot_vms(self):
        """Remove DB rows and files for VMs sent by the bot itself (vm_Embot_... filenames)."""
        bot_rows = self._db_all(
            "SELECT id, filename FROM vms WHERE filename LIKE 'vm\\_Embot\\_%' ESCAPE '\\'"
        )

        if not bot_rows:
            return

        self.bot.logger.log(MODULE_NAME,
            f"Purging {len(bot_rows)} DB entry/entries sent by the bot (Embot)…")

        purged_files = 0
        purged_rows  = 0
        for vm_id, filename in bot_rows:
            for search_dir in (self.vms_dir, self.archive_dir, _broken_dir()):
                candidate = search_dir / filename
                if candidate.exists():
                    try:
                        candidate.unlink()
                        purged_files += 1
                        self.bot.logger.log(MODULE_NAME,
                            f"Deleted bot VM file: {candidate}")
                    except Exception as exc:
                        self.bot.logger.log(MODULE_NAME,
                            f"Could not delete {candidate}: {exc}", "WARNING")

            try:
                conn = self._conn()
                try:
                    conn.execute("DELETE FROM vms_playback WHERE vm_id=?", (vm_id,))
                    conn.execute("DELETE FROM vms WHERE id=?", (vm_id,))
                    conn.commit()
                    purged_rows += 1
                finally:
                    conn.close()
            except Exception as exc:
                self.bot.logger.log(MODULE_NAME,
                    f"DB purge error for VM #{vm_id}: {exc}", "WARNING")

        self.bot.logger.log(MODULE_NAME,
            f"Bot-VM purge complete: {purged_rows} row(s) removed, "
            f"{purged_files} file(s) deleted")

    # ---------------------------------------------------------------- Startup --

    async def startup(self):
        """Start queue worker, run scan/backfill as needed, then archive check."""
        await self._start_queue_worker()
        await asyncio.sleep(0.5)

        self._purge_bot_vms()

        resume_checkpoint = self._load_backfill_checkpoint()

        if not any(self.vms_dir.glob("*.ogg")):
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
                self._backfill_from_discord(),
                self.process_unprocessed(),
            )
        elif resume_checkpoint is not None:
            self.bot.logger.log(MODULE_NAME,
                f"Interrupted backfill detected — resuming from checkpoint {resume_checkpoint}…")
            self._bulk_stop.clear()
            self._bulk_proc = BulkProcessor(
                db_path=self.db_path,
                vms_dir=self.vms_dir,
                logger=self.bot.logger,
                stop_event=self._bulk_stop,
            )
            self._bulk_proc.start()
            await asyncio.gather(
                self._backfill_from_discord(resume_after_id=resume_checkpoint),
                self.process_unprocessed(),
            )
        else:
            await self.process_unprocessed()

        await self.run_archive_if_due()
        self.bot.logger.log(MODULE_NAME, "VMS startup complete")


# ==================== EXTERNAL TRANSCRIPTION QUEUE ====================

EXT_COOLDOWN_SECONDS = 30

_ext_queue:        Optional[asyncio.Queue] = None
_ext_pending:      list                    = []   # mirrors queue contents for position tracking
_ext_pending_lock: Optional[asyncio.Lock]  = None
_ext_cooldowns:    dict                    = {}   # user_id → last request timestamp
_ext_worker_task:  Optional[asyncio.Task]  = None
_ext_avg_secs:     float                   = 5.0  # EMA of actual transcription times
_EXT_AVG_ALPHA:    float                   = 0.3


def _ext_cooldown_remaining(user_id: str) -> float:
    last = _ext_cooldowns.get(user_id, 0.0)
    return max(0.0, EXT_COOLDOWN_SECONDS - (time.time() - last))


def _ext_queue_eta(position: int) -> float:
    """Estimated seconds until item at given 1-based queue position is processed."""
    return position * _ext_avg_secs


def _ext_status_embed(position: int, total: int, done: bool = False,
                      transcript: str = None, error: str = None) -> discord.Embed:
    """Build a status embed for an external transcription request."""
    if error:
        e = discord.Embed(color=0xe74c3c)
        e.description = f"⚠️ {error}"
        return e
    if done:
        e = discord.Embed(color=0x2ecc71)
        e.description = f"> {transcript}"
        return e
    eta = _ext_queue_eta(position)
    eta_str = f"~{int(eta)}s" if eta < 60 else f"~{int(eta // 60)}m {int(eta % 60)}s"
    e = discord.Embed(
        title="🎙️ Transcribing…",
        color=0x3498db,
    )
    if position == 1:
        e.description = "⏳ Processing now…"
    else:
        e.description = f"**Position {position}** of {total} in queue\nEstimated wait: {eta_str}"
    return e


# ==================== MODULE SETUP ====================

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up VMS module")

    global _ext_queue, _ext_pending_lock, _ext_worker_task

    async def _start_ext_worker():
        """Initialise queue/lock and launch worker — called after event loop is ready."""
        global _ext_queue, _ext_pending_lock, _ext_worker_task

        _ext_queue        = asyncio.Queue()
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

                    # Mark current item as "processing now" and update rest
                    async with _ext_pending_lock:
                        total = len(_ext_pending)
                    try:
                        await followup_msg.edit(embed=_ext_status_embed(1, total))
                    except Exception:
                        pass
                    async with _ext_pending_lock:
                        for i, entry in enumerate(
                            [e for e in _ext_pending if e['msg'] != followup_msg], start=2
                        ):
                            try:
                                await entry['msg'].edit(embed=_ext_status_embed(i, total))
                            except Exception:
                                pass

                    # Transcribe
                    t_start    = time.time()
                    transcript = None
                    broken     = False
                    try:
                        raw = await vm_att.read()
                        temp_path.write_bytes(raw)
                        loop = asyncio.get_running_loop()
                        transcript, _, broken = await loop.run_in_executor(
                            manager._executor,
                            _transcribe_with_model, str(temp_path), WHISPER_MODEL_SIZE
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

                    # Edit result
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

                    # Remove from tracking list and update remaining positions
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
            """Restart the worker if it ever dies unexpectedly."""
            global _ext_worker_task
            while True:
                await asyncio.sleep(5)
                if _ext_worker_task is None or _ext_worker_task.done():
                    exc = _ext_worker_task.exception() if _ext_worker_task and not _ext_worker_task.cancelled() else None
                    bot.logger.log(MODULE_NAME,
                        f"Ext worker died ({exc}) — restarting", "WARNING")
                    _ext_worker_task = asyncio.create_task(_ext_worker())

        _ext_worker_task = asyncio.create_task(_ext_worker())
        asyncio.create_task(_watchdog())
        bot.logger.log(MODULE_NAME, "External transcription queue worker started")

    manager         = VMSManager(bot)
    bot.vms_manager = manager

    # Scheduled archive loop
    @tasks.loop(hours=ARCHIVE_JOB_INTERVAL_HOURS)
    async def _archive_loop():
        await manager.run_archive_if_due()

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
            """vms-resume <date> — downloads VMs after the given date not already in the DB."""
            from datetime import timezone as _tz

            date_str = args_str.strip()
            if not date_str:
                print("Usage: vms-resume <date>")
                print("  Examples:")
                print("    vms-resume 2024-11-15")
                print("    vms-resume 2024-11-15 14:30")
                print("    vms-resume 2024-11-15 14:30:00")
                return

            since_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    since_dt = datetime.strptime(date_str, fmt).replace(tzinfo=_tz.utc)
                    break
                except ValueError:
                    continue

            if since_dt is None:
                print(f"Could not parse date '{date_str}'.")
                print("  Expected: YYYY-MM-DD  or  YYYY-MM-DD HH:MM  or  YYYY-MM-DD HH:MM:SS")
                return

            if getattr(manager, '_backfill_running', False):
                print("A backfill is already in progress — wait for it to finish before resuming.")
                return

            print(f"Starting vms-resume from {since_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} "
                  f"(downloading everything after this date that is not already stored)...")
            print("Progress will be logged to console — this runs in the background.")

            asyncio.run_coroutine_threadsafe(
                manager._backfill_from_date(since_dt),
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
                row = manager._db_one("SELECT filename FROM vms WHERE id=?", (vm_id,))
                if row:
                    resolved = manager._resolve_path(row[0])
                    if resolved:
                        await manager.enqueue(vm_id, str(resolved), reply_to=message)
            return

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

        if message.guild and getattr(message.channel, 'name', '') == GENERAL_CHANNEL_NAME:
            guild_id   = str(message.guild.id)
            channel_id = str(message.channel.id)
            count, threshold = manager._inc_counter(guild_id, channel_id)

            if count >= threshold:
                manager._reset_counter(guild_id, channel_id)

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

    bot.logger.log(MODULE_NAME, "VMS module setup complete")

    async def _do_transcribe(
        interaction: discord.Interaction,
        message: discord.Message,
        ephemeral: bool,
    ):
        """Shared handler for both Transcribe VM context menu commands."""
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)

        # Find the voice attachment
        vm_att = None
        for att in message.attachments:
            raw_flags = getattr(message.flags, 'value', 0)
            if (
                bool(raw_flags & 8192)
                or att.filename.lower() == "voice-message.ogg"
                or (att.content_type and "ogg" in att.content_type.lower())
            ):
                vm_att = att
                break

        if vm_att is None:
            await interaction.followup.send(
                "⚠️ No voice message found on that message.", ephemeral=True
            )
            return

        is_emball = (
            message.guild is not None
            and EMBALL_GUILD_ID is not None
            and message.guild.id == EMBALL_GUILD_ID
        )

        # DMs and external guilds — queue-based, ephemeral always, never touch the DB
        if not is_emball:
            user_id = str(interaction.user.id)
            remaining_cd = _ext_cooldown_remaining(user_id)
            if remaining_cd > 0:
                await interaction.followup.send(
                    f"⏱️ You're on cooldown. Try again in **{int(remaining_cd)+1}s**.",
                    ephemeral=True
                )
                return

            _ext_cooldowns[user_id] = time.time()

            temp_dir  = _temp_vms_dir()
            safe_name = re.sub(r'[^\w\-.]', '_', vm_att.filename)
            temp_path = temp_dir / f"ext_{message.id}_{safe_name}"

            async with _ext_pending_lock:
                position = len(_ext_pending) + 1
            total = position

            init_embed   = _ext_status_embed(position, total)
            followup_msg = await interaction.followup.send(embed=init_embed, ephemeral=True, wait=True)

            item = {'vm_att': vm_att, 'temp_path': temp_path, 'msg': followup_msg}
            async with _ext_pending_lock:
                _ext_pending.append(item)
            await _ext_queue.put(item)
            return

        # Emball guild — check if already cached first
        try:
            existing = manager._db_one(
                """SELECT transcript FROM vms
                   WHERE discord_message_id=? AND transcript IS NOT NULL AND transcript != ''""",
                (str(message.id),)
            )
            if existing:
                await interaction.followup.send(f"> {existing[0]}", ephemeral=ephemeral)
                return

            vm_id = await manager.save_voice_message(message, vm_att)
            if not vm_id:
                await interaction.followup.send(
                    "⚠️ Failed to save voice message.", ephemeral=True
                )
                return

            row      = manager._db_one("SELECT filename FROM vms WHERE id=?", (vm_id,))
            filepath = str(manager._resolve_path(row[0])) if row else None

            if not filepath:
                await interaction.followup.send(
                    "⚠️ VM saved but file could not be located for transcription.", ephemeral=True
                )
                return

            loop = asyncio.get_running_loop()
            transcript, duration, broken = await loop.run_in_executor(
                manager._executor,
                _transcribe_with_model, filepath, WHISPER_MODEL_SIZE
            )

            if broken or not transcript:
                await interaction.followup.send(
                    "⚠️ Could not transcribe that voice message.", ephemeral=True
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
                "⚠️ An error occurred while transcribing.", ephemeral=True
            )

    def _has_vm(message: discord.Message) -> bool:
        for att in message.attachments:
            raw_flags = getattr(message.flags, 'value', 0)
            if (
                bool(raw_flags & 8192)
                or att.filename.lower() == "voice-message.ogg"
                or (att.content_type and "ogg" in att.content_type.lower())
            ):
                return True
        return False

    @bot.tree.context_menu(name="Transcribe VM (For Everyone)")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def transcribe_vm_public(interaction: discord.Interaction, message: discord.Message):
        if not _has_vm(message):
            await interaction.response.send_message(
                "⚠️ That message doesn't contain a voice message.", ephemeral=True
            )
            return
        await _do_transcribe(interaction, message, ephemeral=False)

    @bot.tree.context_menu(name="Transcribe VM (Only Me)")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def transcribe_vm_private(interaction: discord.Interaction, message: discord.Message):
        if not _has_vm(message):
            await interaction.response.send_message(
                "⚠️ That message doesn't contain a voice message.", ephemeral=True
            )
            return
        await _do_transcribe(interaction, message, ephemeral=True)

    bot.logger.log(MODULE_NAME, "Registered context menus: Transcribe VM (For Everyone) / Transcribe VM (Only Me)")