import asyncio
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


async def _probe_once(host="1.1.1.1", port=53, timeout=3.0) -> bool:
    """Non-blocking TCP probe. Returns True if reachable."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def run_network_probe(logger, module="NETWORK", interval=5.0):
    """
    Proactively polls network reachability every `interval` seconds.
    Flips NetworkState before Discord's WebSocket notices an outage.
    Start as a long-lived task after bot is ready.
    """
    consecutive_fails = 0
    FAIL_THRESHOLD = 2  # mark offline after 2 consecutive misses (~10s)

    while True:
        reachable = await _probe_once()

        if reachable:
            if not NetworkState.is_online():
                dropped = NetworkState.set_online()
                msg = "Network restored (probe)"
                if dropped:
                    msg += f" — {dropped} error(s) suppressed during outage"
                logger.log(module, msg)
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            if NetworkState.is_online() and consecutive_fails >= FAIL_THRESHOLD:
                NetworkState.set_offline()
                logger.log(module, "Network unreachable — suppressing connectivity errors", "WARNING")

        await asyncio.sleep(interval)


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

_KS_FAMILIES = {
    "mod":   {"mod_core", "mod_suspicion", "mod_actions", "mod_appeals", "mod_oversight", "mod_rules", "mod_notes", "mod_logger"},
    "vms":   {"vms_core", "vms_transcribe", "vms_storage", "vms_playback"},
    "music": {"music_archive", "music_player", "music_browser"},
}

def is_killswitch_active(bot, module: str = None) -> bool:
    """
    Returns True if activity should be halted for the given module.
    Pass module=__name__ from any module. Omit for global check.
    Reads from embot.json so it survives restarts.
    """
    try:
        cfg_path = script_dir() / "config" / "embot.json"
        if not cfg_path.exists():
            return False
        import json as _json
        with open(cfg_path, "r", encoding="utf-8") as f:
            halted = _json.load(f).get("killswitch_halted", [])
        if not halted:
            return False
        if "all" in halted:
            return True
        if module is None:
            return True  # global check: something is halted
        # Check if module's family is halted
        for family, members in _KS_FAMILIES.items():
            if module in members and family in halted:
                return True
        # Check direct name
        return module in halted
    except Exception:
        return False
