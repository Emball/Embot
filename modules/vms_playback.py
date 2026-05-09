import aiohttp
import asyncio
import base64
import discord
import os
import random
import re
import time
from pathlib import Path
from typing import Optional, Tuple, List
from _utils import script_dir, _now
from vms_core import (
    MODULE_NAME, _vms_dir, _archive_dir, _broken_dir, EMBALL_GUILD_ID,
)
from vms_transcribe import generate_waveform, STOP_WORDS, get_ogg_duration

GENERAL_CHANNEL_NAME = "general"
PING_COOLDOWN_SECONDS = 10
VM_COOLDOWN_DAYS = 7
LONG_VM_THRESHOLD_SECS = 60
RANDOM_PLAYBACK_MIN = 40
RANDOM_PLAYBACK_MAX = 80


def keywords(text: str) -> set:
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return {w for w in words if w not in STOP_WORDS}


def eligible_vms(manager):
    cutoff = int(time.time()) - (VM_COOLDOWN_DAYS * 86400)
    rows = manager._db_all(
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
        p = manager._resolve_path(r[1])
        if p is not None:
            result.append((r[0], str(p), r[2], r[3], r[4], r[5]))
    return result


def select_contextual(manager, recent_messages: List[str]) -> Optional[Tuple[int, str, float]]:
    chat_kw = keywords("".join(recent_messages))
    if not chat_kw:
        return None
    now = int(time.time())
    scored = []
    for vm_id, fp, transcript, duration, created_at, _ in eligible_vms(manager):
        vm_kw = keywords(transcript)
        overlap = len(chat_kw & vm_kw)
        if overlap == 0:
            continue
        score = overlap * 10.0
        age_days = (now - created_at) / 86400
        score += max(0.0, 30.0 - age_days) * 0.1
        if duration > LONG_VM_THRESHOLD_SECS:
            score -= (duration - LONG_VM_THRESHOLD_SECS) * 0.1
        scored.append((score, vm_id, fp, duration))
    if not scored:
        return None
    scored.sort(reverse=True)
    _, vm_id, fp, dur = scored[0]
    return vm_id, fp, dur


def select_random(manager) -> Optional[Tuple[int, str, float]]:
    candidates = []
    for vm_id, fp, _, duration, *_ in eligible_vms(manager):
        w = max(10, int(100 - max(0, duration - LONG_VM_THRESHOLD_SECS) * 0.5))
        candidates.append((w, vm_id, fp, duration))
    if not candidates:
        return None
    total = sum(w for w, *_ in candidates)
    pick = random.uniform(0, total)
    cumulative = 0
    for w, vm_id, fp, dur in candidates:
        cumulative += w
        if pick <= cumulative:
            return vm_id, fp, dur
    _, vm_id, fp, dur = candidates[-1]
    return vm_id, fp, dur


async def _get_or_create_session(manager) -> aiohttp.ClientSession:
    if manager._session is None or manager._session.closed:
        manager._session = aiohttp.ClientSession()
    return manager._session


async def send_voice_message_api(
    bot_token: str,
    channel_id: int,
    ogg_path: str,
    duration_secs: float,
    waveform_b64: str,
    session: aiohttp.ClientSession,
) -> Optional[dict]:
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

    attachment = data["attachments"][0]
    upload_url = attachment["upload_url"]
    upload_filename = attachment["upload_filename"]

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


async def send_vm(manager, channel, vm_id: int, filepath: str, duration: float) -> bool:
    try:
        row = manager._db_one("SELECT waveform_b64 FROM vms WHERE id=?", (vm_id,))
        waveform = (row[0] if row and row[0] else None) or generate_waveform(filepath)
        session = await _get_or_create_session(manager)
        result = await send_voice_message_api(
            manager._token, channel.id, filepath, duration, waveform, session
        )
        if result:
            await mark_played(manager, vm_id)
            manager.bot.logger.log(MODULE_NAME,
                f"Sent VM #{vm_id} to channel {channel.id}")
            return True
        manager.bot.logger.log(MODULE_NAME,
            f"Failed to send VM #{vm_id}", "WARNING")
        return False
    except Exception as exc:
        manager.bot.logger.error(MODULE_NAME, f"send_vm error for #{vm_id}", exc)
        return False


async def mark_played(manager, vm_id: int):
    manager._db_exec(
        """INSERT INTO vms_playback (vm_id, last_played, play_count) VALUES (?, ?, 1)
           ON CONFLICT(vm_id) DO UPDATE SET
             last_played = excluded.last_played,
             play_count  = play_count + 1""",
        (vm_id, int(time.time()))
    )


def get_counter(manager, guild_id: str, channel_id: str) -> Tuple[int, int]:
    row = manager._db_one(
        "SELECT count, threshold FROM vms_message_counter WHERE guild_id=? AND channel_id=?",
        (guild_id, channel_id)
    )
    return (row[0], row[1]) if row else (
        0, random.randint(RANDOM_PLAYBACK_MIN, RANDOM_PLAYBACK_MAX)
    )


def inc_counter(manager, guild_id: str, channel_id: str) -> Tuple[int, int]:
    count, threshold = get_counter(manager, guild_id, channel_id)
    count += 1
    manager._db_exec(
        """INSERT INTO vms_message_counter (guild_id, channel_id, count, threshold)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id, channel_id) DO UPDATE SET count=excluded.count""",
        (guild_id, channel_id, count, threshold)
    )
    return count, threshold


def reset_counter(manager, guild_id: str, channel_id: str):
    new_thresh = random.randint(RANDOM_PLAYBACK_MIN, RANDOM_PLAYBACK_MAX)
    manager._db_exec(
        """INSERT INTO vms_message_counter (guild_id, channel_id, count, threshold)
           VALUES (?, ?, 0, ?)
           ON CONFLICT(guild_id, channel_id) DO UPDATE SET count=0, threshold=excluded.threshold""",
        (guild_id, channel_id, new_thresh)
    )


def ping_allowed(manager, user_id: str) -> bool:
    row = manager._db_one(
        "SELECT last_ping FROM vms_ping_cooldown WHERE user_id=?", (user_id,)
    )
    if not row:
        return True
    return (int(time.time()) - row[0]) >= PING_COOLDOWN_SECONDS


def set_ping_cooldown(manager, user_id: str):
    manager._db_exec(
        """INSERT INTO vms_ping_cooldown (user_id, last_ping) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET last_ping=excluded.last_ping""",
        (user_id, int(time.time()))
    )


def recent_messages(manager, guild_id: str, channel_id: str, limit: int = 20) -> List[str]:
    try:
        mod = getattr(manager.bot, '_mod_system', None)
        if mod and hasattr(mod, 'message_cache'):
            msgs = mod.message_cache.get(guild_id, {}).get(channel_id, [])
            return [m.get('content', '') for m in msgs[-limit:] if m.get('content')]
    except Exception:
        pass
    return []
