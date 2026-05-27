import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class NetworkState:
    """Shared connectivity state. Modules check this before logging network errors."""
    _online = True
    _suppressed = 0  # errors silently dropped while offline

    @classmethod
    def is_online(cls) -> bool:
        return cls._online

    @classmethod
    def set_offline(cls):
        cls._online = False
        cls._suppressed = 0

    @classmethod
    def set_online(cls) -> int:
        """Marks online. Returns count of suppressed errors since last outage."""
        dropped = cls._suppressed
        cls._online = True
        cls._suppressed = 0
        return dropped

    @classmethod
    def suppress(cls):
        """Call instead of logging a network error while offline."""
        cls._suppressed += 1

def atomic_json_write(filepath, data, indent=2, ensure_ascii=False):
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
    p = Path(path)
    existing: dict = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            existing = json.load(f)

    merged = {k: existing.get(k, v) for k, v in defaults.items()}

    atomic_json_write(p, merged)
    return merged

def script_dir() -> Path:
    return Path(__file__).parent.parent.absolute()

def _now() -> datetime:
    return datetime.now(timezone.utc)
