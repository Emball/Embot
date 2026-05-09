import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def atomic_json_write(filepath, data, indent=2, ensure_ascii=False):
    """Atomically write JSON to a file using a temp file + rename."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def migrate_config(path, defaults):
    """Load a JSON config, merging with defaults and pruning retired keys.

    Keys still in *defaults* keep their current value from disk.
    Keys added to *defaults* since last run get the default value.
    Keys on disk that no longer exist in *defaults* are removed.
    The cleaned config is written back atomically.

    Returns the merged dict.
    """
    p = Path(path)
    existing: dict = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            existing = json.load(f)

    merged = {k: existing.get(k, v) for k, v in defaults.items()}

    atomic_json_write(p, merged)
    return merged


def script_dir() -> Path:
    """Root Embot/ directory (two levels up from modules/)."""
    return Path(__file__).parent.parent.absolute()


def _now() -> datetime:
    """Current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)
