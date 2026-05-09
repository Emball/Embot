import base64
import numpy as np
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Tuple, List
from vms_core import (
    MODULE_NAME, _whisper_model_dir, _broken_dir,
)

WAVEFORM_SAMPLES = 256
WHISPER_MODEL_SIZE = "base"
WHISPER_IDLE_UNLOAD_SECS = int(os.getenv("WHISPER_IDLE_UNLOAD_SECS", "300"))
BULK_GPU_WORKERS = 1
BULK_CPU_WORKERS = 1
BULK_BATCH_SIZE = 16
SCAN_BATCH_SIZE = 256

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

_BULK_SENTINEL = object()


def generate_waveform(filepath: str, num_samples: int = WAVEFORM_SAMPLES) -> str:
    try:
        import soundfile as sf
        data, _ = sf.read(filepath, dtype="float32", always_2d=True)
        samples = _downsample_to_waveform(data[:, 0], num_samples)
        return base64.b64encode(bytes(samples)).decode()
    except Exception:
        pass

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
    t = np.linspace(0, np.pi, num_samples)
    envelope = np.sin(t) ** 0.6
    noise = np.random.uniform(0.5, 1.0, num_samples)
    wave = (envelope * noise * 200).astype(np.uint8)
    return base64.b64encode(bytes(wave)).decode()


def get_ogg_duration(filepath: str) -> float:
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


def _is_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


class _WhisperManager:

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._device = "cpu"
        self._load_failed = False
        self._last_use = 0.0
        self._watchdog: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()

    def load(self, model_size: Optional[str] = None) -> Optional[object]:
        size = model_size or WHISPER_MODEL_SIZE
        with self._lock:
            if self._model is not None:
                if getattr(self._model, '_vms_size', None) != size:
                    self._unload_locked()
                else:
                    self._last_use = time.time()
                    return self._model
            if self._load_failed:
                return None
            self._load_locked(size)
            return self._model

    def touch(self):
        self._last_use = time.time()

    def unload(self):
        with self._lock:
            self._unload_locked()

    def start_watchdog(self):
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._stop_watchdog.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name="vms_whisper_watchdog",
            daemon=True,
        )
        self._watchdog.start()

    def stop_watchdog(self):
        self._stop_watchdog.set()

    def _load_locked(self, size: str):
        try:
            import whisper
            try:
                import torch
                self._device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                self._device = "cpu"
            model_dir = str(_whisper_model_dir)
            print(f"[{MODULE_NAME}] Loading Whisper '{size}' "
                  f"on {self._device} (cache: {model_dir})...")
            m = whisper.load_model(size, device=self._device, download_root=model_dir)
            m._vms_size = size
            self._model = m
            self._last_use = time.time()
            print(f"[{MODULE_NAME}] Whisper '{size}' loaded on {self._device}")
        except ImportError:
            print(f"[{MODULE_NAME}] openai-whisper not installed - transcription disabled")
            self._load_failed = True
        except Exception as exc:
            print(f"[{MODULE_NAME}] Whisper load error: {exc}")
            self._load_failed = True

    def _unload_locked(self):
        if self._model is None:
            return
        print(f"[{MODULE_NAME}] Unloading Whisper model (idle for "
              f"{int(time.time() - self._last_use)}s)...")
        try:
            if self._device == "cuda":
                try:
                    import torch
                    self._model.cpu()
                    torch.cuda.empty_cache()
                except Exception:
                    pass
        except Exception:
            pass
        self._model = None
        self._device = "cpu"
        print(f"[{MODULE_NAME}] Whisper model unloaded")

    def _watchdog_loop(self):
        while not self._stop_watchdog.is_set():
            self._stop_watchdog.wait(timeout=30)
            if self._stop_watchdog.is_set():
                break
            with self._lock:
                if self._model is None:
                    continue
                idle = time.time() - self._last_use
                if idle >= WHISPER_IDLE_UNLOAD_SECS:
                    self._unload_locked()


_whisper_mgr = _WhisperManager()


def load_whisper() -> Optional[object]:
    model = _whisper_mgr.load()
    if model is not None:
        _whisper_mgr.start_watchdog()
    return model


def quarantine_file(filepath: str) -> str:
    src = Path(filepath)
    dst = _broken_dir / src.name
    try:
        if src.exists():
            try:
                src.rename(dst)
            except (FileExistsError, OSError):
                src.unlink(missing_ok=True)
            print(f"[{MODULE_NAME}] Quarantined corrupt file -> {dst}")
        return src.name
    except Exception as exc:
        print(f"[{MODULE_NAME}] Failed to quarantine {src}: {exc}")
        return src.name


def _is_audio_valid(audio) -> bool:
    if audio is None or len(audio) == 0:
        return False
    if len(audio) < 1600:
        return False
    return True


def transcribe_core(filepath: str, model: object) -> Tuple[Optional[str], Optional[str]]:
    import whisper as _whisper
    try:
        audio = _whisper.load_audio(filepath)
    except Exception as exc:
        return None, f"Cannot read audio: {exc}"
    if not _is_audio_valid(audio):
        return None, "Audio too short/empty"
    try:
        audio = _whisper.pad_or_trim(audio)
        mel = _whisper.log_mel_spectrogram(audio).to(model.device)
    except Exception as exc:
        return None, f"Mel failed: {exc}"
    try:
        _, _probs = model.detect_language(mel)
        options = _whisper.DecodingOptions(fp16=False)
        result = _whisper.decode(model, mel, options)
        _whisper_mgr.touch()
        return (result.text or "").strip() or None, None
    except (RuntimeError, ValueError) as exc:
        return None, f"Decode failed: {exc}"
    except Exception as exc:
        exc_str = str(exc)
        if "Linear(" in exc_str or "in_features" in exc_str:
            return None, "Whisper internal error"
        return None, f"Transcription error: {exc_str}"


def process_file_sync(filepath: str) -> Tuple[Optional[str], float, str, bool, str]:
    try:
        duration = get_ogg_duration(filepath)
        waveform = generate_waveform(filepath)
        model = load_whisper()
        if model is None:
            return None, duration, waveform, False, Path(filepath).name
        transcript, error = transcribe_core(filepath, model)
        if error is not None:
            print(f"[{MODULE_NAME}] {error} ({filepath}) - quarantining")
            return None, 0.0, waveform, True, quarantine_file(filepath)
        return transcript, duration, waveform, False, Path(filepath).name
    except Exception as exc:
        print(f"[{MODULE_NAME}] Fatal processing error ({filepath}): {exc} - quarantining")
        broken_name = Path(filepath).name
        try:
            broken_name = quarantine_file(filepath)
        except Exception:
            pass
        return None, 0.0, "", True, broken_name


def transcribe_with_model(filepath: str, model_size: str) -> Tuple[Optional[str], float, bool]:
    try:
        duration = get_ogg_duration(filepath)
        model = _whisper_mgr.load(model_size)
        if model is None:
            return None, duration, True
        transcript, error = transcribe_core(filepath, model)
        if error is not None:
            return None, duration, True
        return transcript, duration, False
    except Exception:
        return None, 0.0, True


class BulkProcessor:

    def __init__(self, db_path: str, vms_dir: Path, logger, stop_event: threading.Event):
        self.db_path = db_path
        self.vms_dir = vms_dir
        self.logger = logger
        self.stop_event = stop_event
        self._work_q: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def start(self, initial_files: Optional[List[Tuple[int, str]]] = None):
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
            f"(workers={'GPUx' + str(BULK_GPU_WORKERS) if _is_cuda() else 'CPUx' + str(BULK_CPU_WORKERS)})")

    def feed(self, vm_id: int, filepath: str):
        self._work_q.put((vm_id, filepath))

    def done_feeding(self):
        self._work_q.put(_BULK_SENTINEL)

    def stop(self):
        self.stop_event.set()
        self._work_q.put(_BULK_SENTINEL)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        done = 0
        errors = 0
        self.logger.log(MODULE_NAME, "BulkProcessor worker started - waiting for files...")
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
                        process_file_sync(fp)
                except Exception as exc:
                    self.logger.log(MODULE_NAME,
                        f"BulkProcessor: unexpected error on VM #{vm_id}: {exc}", "WARNING")
                    errors += 1
                    done += 1
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
                f"BulkProcessor: fatal error - {exc}", "ERROR")
        self.logger.log(MODULE_NAME,
            f"BulkProcessor complete: {done} processed, {errors} errors")

    def _commit_batch(self, batch: list):
        from vms_core import _archive_dir
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            archive_name_set = {p.name for p in _archive_dir.glob("*.ogg")}
            try:
                for vm_id, transcript, duration, waveform, filename in batch:
                    new_state = 2 if filename in archive_name_set else 1
                    conn.execute(
                        """UPDATE vms SET transcript=?, duration_secs=?, waveform_b64=?,
                           filename=?, processed=? WHERE id=? AND processed=0""",
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
                f"BulkProcessor: DB commit error - {exc}", "ERROR")

    def _commit_broken(self, batch: list):
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
                f"BulkProcessor: broken-batch commit error - {exc}", "ERROR")
