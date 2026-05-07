import json
import os
import tempfile
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
