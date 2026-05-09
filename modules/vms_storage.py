import asyncio
import discord
import time
from datetime import datetime
from typing import Optional
from vms_core import (
    MODULE_NAME, GENERAL_CHANNEL_NAME,
    _vms_dir, _archive_dir, _broken_dir,
    _vm_canonical_name, _rename_to_canonical, _parse_vm_filename,
)
from vms_transcribe import (
    get_ogg_duration, load_whisper, SCAN_BATCH_SIZE,
)

ARCHIVE_AFTER_DAYS = 150
DELETE_AFTER_DAYS = 365
ARCHIVE_JOB_INTERVAL_HOURS = 24
BACKFILL_DAYS = 365


def scan_and_conform(manager) -> list:
    scan_dirs = [
        (_vms_dir,     "vms",     0),
        (_archive_dir, "archive", 2),
        (_broken_dir,  "broken",  4),
    ]

    existing_canonical = {
        r[0]: r[1]
        for r in manager._db_all("SELECT filename, id FROM vms")
    }

    non_canonical = manager._db_all(
        """SELECT id, filename FROM vms
           WHERE processed IN (1, 2, 4)
             AND filename LIKE 'vm-%'"""
    )
    conformed = 0
    dupes_removed = 0
    conn = manager._conn()
    try:
        for vm_id, old_name in non_canonical:
            row = conn.execute(
                "SELECT discord_message_id, created_at FROM vms WHERE id=?", (vm_id,)
            ).fetchone()
            msg_id = row[0] if row else None
            ts = row[1] if row else None
            parsed = _parse_vm_filename(old_name)
            username = parsed.get("username")
            canon_name = _vm_canonical_name(vm_id, username, msg_id, ts)

            if canon_name in existing_canonical and existing_canonical[canon_name] != vm_id:
                conn.execute("DELETE FROM vms WHERE id=?", (vm_id,))
                dupes_removed += 1
                continue

            old_path = None
            for d in (_vms_dir, _archive_dir, _broken_dir):
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

    existing_canonical_names = {r[0] for r in manager._db_all("SELECT filename FROM vms")}

    new_counts = {}
    conn = manager._conn()
    try:
        for scan_dir, label, proc_state in scan_dirs:
            new = 0
            for ogg in sorted(scan_dir.glob("*.ogg")):
                if ogg.name in existing_canonical_names:
                    continue
                mtime = int(ogg.stat().st_mtime)
                dur = get_ogg_duration(str(ogg))
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
                          f"({ogg.name} -> canonical): {exc} - keeping original name")
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

    untranscribed = manager._db_all(
        """SELECT id FROM vms
           WHERE processed = 2
             AND (transcript IS NULL OR transcript = '')"""
    )
    reset_ids = [r[0] for r in untranscribed]
    if reset_ids:
        for i in range(0, len(reset_ids), SCAN_BATCH_SIZE):
            chunk = reset_ids[i:i + SCAN_BATCH_SIZE]
            placeholders = ",".join("?" * len(chunk))
            conn = manager._conn()
            try:
                conn.execute(
                    f"UPDATE vms SET processed=0 WHERE id IN ({placeholders})",
                    chunk)
                conn.commit()
            finally:
                conn.close()
        print(f"[{MODULE_NAME}] Reset {len(reset_ids)} archived-but-untranscribed VM(s) to pending")

    pending = manager._db_all(
        "SELECT id, filename FROM vms WHERE processed=0 AND filename != '__pending__'"
    )
    valid = [(vid, str(manager._resolve_path(fn))) for vid, fn in pending
             if manager._resolve_path(fn) is not None]
    missing = len(pending) - len(valid)
    if missing:
        print(f"[{MODULE_NAME}] {missing} pending VM(s) have missing files - skipping")
    return valid


async def process_unprocessed(manager):
    manager.bot.logger.log(MODULE_NAME,
        "Startup scan: conforming names and registering files in all dirs...")
    valid = await asyncio.get_running_loop().run_in_executor(
        manager._scan_executor, scan_and_conform, manager
    )
    if not valid:
        manager.bot.logger.log(MODULE_NAME, "No pending VMs to process")
        return
    manager.bot.logger.log(MODULE_NAME,
        f"Scan found {len(valid)} pending VM(s) - feeding BulkProcessor")
    manager._ensure_bulk_proc()
    for vm_id, fp in valid:
        manager._bulk_proc.feed(vm_id, fp)
    if not manager._backfill_running:
        manager._bulk_proc.done_feeding()


async def save_voice_message(manager, message: discord.Message, attachment: discord.Attachment) -> Optional[int]:
    ts = int(time.time())
    guild_id = str(message.guild.id) if message.guild else None
    try:
        conn = manager._conn()
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

        username = getattr(message.author, 'name', None)
        canon_name = _vm_canonical_name(vm_id, username, str(message.id), ts)
        canon_path = _vms_dir / canon_name

        raw = await attachment.read()
        canon_path.write_bytes(raw)

        duration = await asyncio.get_running_loop().run_in_executor(
            manager._executor, get_ogg_duration, str(canon_path)
        )
        manager._db_exec(
            "UPDATE vms SET filename=?, duration_secs=? WHERE id=?",
            (canon_name, duration, vm_id)
        )
        manager.bot.logger.log(MODULE_NAME,
            f"Saved VM #{vm_id}: {canon_name} (guild={guild_id})")
        return vm_id
    except Exception as exc:
        manager.bot.logger.error(MODULE_NAME, "Failed to save voice message", exc)
    return None


async def run_archive_if_due(manager):
    row = manager._db_one(
        "SELECT last_run FROM vms_scheduled_jobs WHERE job_name='archive'"
    )
    last_run = row[0] if row else 0
    now = int(time.time())
    due_after = last_run + (ARCHIVE_JOB_INTERVAL_HOURS * 3600)
    if now >= due_after:
        missed = (now - due_after) // 3600
        if missed > 0:
            manager.bot.logger.log(MODULE_NAME,
                f"Archive job missed by ~{missed}h - running now (crash recovery)")
        await do_archive(manager)
    else:
        next_dt = datetime.fromtimestamp(due_after).strftime("%Y-%m-%d %H:%M")
        manager.bot.logger.log(MODULE_NAME, f"Archive job not due yet (next: {next_dt})")


async def do_archive(manager):
    now = int(time.time())
    archive_cutoff = now - (ARCHIVE_AFTER_DAYS * 86400)
    delete_cutoff = now - (DELETE_AFTER_DAYS * 86400)
    archived = deleted = 0

    for vm_id, fn in manager._db_all(
        "SELECT id, filename FROM vms WHERE created_at < ? AND processed != 3",
        (delete_cutoff,)
    ):
        try:
            p = manager._resolve_path(fn)
            if p and p.exists():
                p.unlink()
            manager._db_exec(
                "UPDATE vms SET processed=3, deleted_at=? WHERE id=?",
                (now, vm_id)
            )
            deleted += 1
        except Exception as exc:
            manager.bot.logger.error(MODULE_NAME, f"Failed to delete VM #{vm_id}", exc)

    for vm_id, fn in manager._db_all(
        """SELECT id, filename FROM vms
           WHERE created_at < ? AND created_at >= ? AND processed=1""",
        (archive_cutoff, delete_cutoff)
    ):
        try:
            src = manager._resolve_path(fn)
            dst = _archive_dir / fn
            if src and src.exists() and not dst.exists():
                src.rename(dst)
            manager._db_exec(
                "UPDATE vms SET processed=2, archived_at=? WHERE id=?",
                (now, vm_id)
            )
            archived += 1
        except Exception as exc:
            manager.bot.logger.error(MODULE_NAME, f"Failed to archive VM #{vm_id}", exc)

    manager._db_exec(
        "INSERT OR REPLACE INTO vms_scheduled_jobs (job_name, last_run) VALUES ('archive', ?)",
        (now,)
    )
    manager.bot.logger.log(MODULE_NAME,
        f"Archive job complete - archived: {archived}, deleted: {deleted}")


async def backfill(manager, scan_after, label="Backfill"):
    manager._backfill_running = True
    general_channel = None
    for guild in manager.bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL_NAME)
        if ch is not None:
            general_channel = ch
            break
    if general_channel is None:
        manager.bot.logger.log(MODULE_NAME,
            f"{label}: could not find #{GENERAL_CHANNEL_NAME} in any guild - skipping", "WARNING")
        manager._backfill_running = False
        return

    manager.bot.logger.log(MODULE_NAME,
        f"{label}: scanning #{general_channel.name} in '{general_channel.guild.name}' "
        f"from {scan_after.strftime('%Y-%m-%d %H:%M:%S UTC')} onwards...")

    manager.bot.logger.log(MODULE_NAME, f"{label}: pre-loading Whisper model...")
    loop = asyncio.get_running_loop()
    model = await loop.run_in_executor(manager._executor, load_whisper)
    if model is None:
        manager.bot.logger.log(MODULE_NAME,
            f"{label}: Whisper failed to load - files will still be downloaded", "WARNING")
    else:
        manager.bot.logger.log(MODULE_NAME, f"{label}: Whisper ready")

    manager._ensure_bulk_proc()

    known_ids = {
        r[0]
        for r in manager._db_all("SELECT discord_message_id FROM vms WHERE discord_message_id IS NOT NULL")
    }

    downloaded = 0
    skipped = 0
    errors = 0
    msgs_seen = 0

    BACKFILL_SLEEP_EVERY = 200
    BACKFILL_SLEEP_SECS = 2.0
    BACKFILL_DL_SLEEP = 0.5

    try:
        async for message in general_channel.history(limit=None, after=scan_after, oldest_first=True):
            msgs_seen += 1
            if message.author.bot:
                manager._save_backfill_checkpoint(message.id)
                continue
            if msgs_seen % BACKFILL_SLEEP_EVERY == 0:
                manager.bot.logger.log(MODULE_NAME,
                    f"{label}: scanned {msgs_seen} messages "
                    f"({downloaded} downloaded) - pausing {BACKFILL_SLEEP_SECS}s...")
                await asyncio.sleep(BACKFILL_SLEEP_SECS)
            for att in message.attachments:
                is_vm = att.filename.lower() == "voice-message.ogg"
                if not is_vm:
                    continue
                msg_id_str = str(message.id)
                if msg_id_str in known_ids:
                    skipped += 1
                    continue
                try:
                    ts = int(message.created_at.timestamp())
                    guild_id = str(message.guild.id) if message.guild else None
                    conn = manager._conn()
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

                    username = getattr(message.author, 'name', None)
                    canon_name = _vm_canonical_name(vm_id, username, msg_id_str, ts)
                    canon_path = _vms_dir / canon_name
                    raw = await att.read()
                    canon_path.write_bytes(raw)

                    duration = await asyncio.get_running_loop().run_in_executor(
                        manager._executor, get_ogg_duration, str(canon_path)
                    )
                    manager._db_exec(
                        "UPDATE vms SET filename=?, duration_secs=? WHERE id=?",
                        (canon_name, duration, vm_id)
                    )
                    manager._bulk_proc.feed(vm_id, str(canon_path))
                    known_ids.add(msg_id_str)
                    downloaded += 1
                    if downloaded % 100 == 0:
                        manager.bot.logger.log(MODULE_NAME,
                            f"{label}: {downloaded} downloaded so far...")
                    await asyncio.sleep(BACKFILL_DL_SLEEP)
                except Exception as exc:
                    manager.bot.logger.log(MODULE_NAME,
                        f"{label}: failed to download message {message.id}: {exc}", "WARNING")
                    errors += 1
            manager._save_backfill_checkpoint(message.id)
    except discord.Forbidden:
        manager.bot.logger.log(MODULE_NAME,
            f"{label}: no permission to read #{general_channel.name} - skipping", "WARNING")
    except Exception as exc:
        manager.bot.logger.log(MODULE_NAME,
            f"{label}: history scrape error - {exc}", "ERROR")
    finally:
        manager._backfill_running = False
        if manager._bulk_proc and manager._bulk_proc.is_running():
            manager._bulk_proc.done_feeding()

    manager.bot.logger.log(MODULE_NAME,
        f"{label} complete: {downloaded} downloaded, {skipped} already known, {errors} errors")
    manager._clear_backfill_checkpoint()


def purge_bot_vms(manager):
    bot_rows = manager._db_all(
        "SELECT id, filename FROM vms WHERE filename LIKE 'vm\\_Embot\\_%' ESCAPE '\\'"
    )
    if not bot_rows:
        return
    manager.bot.logger.log(MODULE_NAME,
        f"Purging {len(bot_rows)} DB entry/entries sent by the bot (Embot)...")
    purged_files = 0
    for _, filename in bot_rows:
        for search_dir in (_vms_dir, _archive_dir, _broken_dir):
            candidate = search_dir / filename
            if candidate.exists():
                try:
                    candidate.unlink()
                    purged_files += 1
                    manager.bot.logger.log(MODULE_NAME,
                        f"Deleted bot VM file: {candidate}")
                except Exception as exc:
                    manager.bot.logger.log(MODULE_NAME,
                        f"Could not delete {candidate}: {exc}", "WARNING")
    vm_ids = [(vid,) for vid, _ in bot_rows]
    conn = manager._conn()
    try:
        conn.executemany("DELETE FROM vms_playback WHERE vm_id=?", vm_ids)
        conn.executemany("DELETE FROM vms WHERE id=?", vm_ids)
        conn.commit()
    except Exception as exc:
        manager.bot.logger.log(MODULE_NAME, f"DB purge error: {exc}", "WARNING")
    finally:
        conn.close()
    manager.bot.logger.log(MODULE_NAME,
        f"Bot-VM purge complete: {len(vm_ids)} row(s) removed, "
        f"{purged_files} file(s) deleted")


def setup(bot):
    bot.logger.log(MODULE_NAME, "VMS storage loaded")
