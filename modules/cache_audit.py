"""
Temporary module: audits song_cache for CDN URL mismatches caused by
attachment order assumption in _send_batch. Fetches each multi-file
message from Discord, matches attachments by normalized filename, and
corrects any wrong cdn_url entries. Deletes itself when done.
"""
import asyncio
import sqlite3
from pathlib import Path
from music_archive import normalize_title
from _utils import script_dir

MODULE_NAME = "CACHE_AUDIT"


async def _run_audit(bot):
    db_path = script_dir() / 'db' / 'musicarchive.db'
    conn = sqlite3.connect(db_path)

    # fetch all multi-file messages
    multi = conn.execute(
        'SELECT message_id, channel_id FROM song_cache GROUP BY message_id HAVING COUNT(*) > 1'
    ).fetchall()
    bot.logger.log(MODULE_NAME, f"Auditing {len(multi)} multi-file messages...")

    mismatches = 0
    fixed = 0
    unfixable = 0
    checked_msgs = 0

    for message_id, channel_id in multi:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            bot.logger.log(MODULE_NAME, f"Channel {channel_id} not found, skipping", "WARNING")
            continue
        try:
            msg = await channel.fetch_message(int(message_id))
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not fetch msg {message_id}: {e}", "WARNING")
            continue

        checked_msgs += 1
        # build normalized filename -> attachment url map from actual Discord message
        att_map = {normalize_title(Path(a.filename).stem): a for a in msg.attachments}

        # get all DB entries for this message
        rows = conn.execute(
            'SELECT file_path, file_name, cdn_url FROM song_cache WHERE message_id = ?',
            (message_id,)
        ).fetchall()

        for file_path, file_name, stored_url in rows:
            key = normalize_title(Path(file_name).stem)
            att = att_map.get(key)
            if att is None:
                bot.logger.log(MODULE_NAME, f"No attachment match for {file_name} in msg {message_id}", "WARNING")
                unfixable += 1
                continue
            if att.url.split('?')[0] != stored_url.split('?')[0]:
                mismatches += 1
                conn.execute(
                    'UPDATE song_cache SET cdn_url = ? WHERE file_path = ?',
                    (att.url, file_path)
                )
                bot.logger.log(MODULE_NAME, f"Fixed: {file_name}")
                fixed += 1

        await asyncio.sleep(0.5)  # be gentle with Discord rate limits

    conn.commit()
    conn.close()
    bot.logger.log(MODULE_NAME,
        f"Audit complete: {checked_msgs}/{len(multi)} messages checked, "
        f"{mismatches} mismatches found, {fixed} fixed, {unfixable} unfixable")

    Path(__file__).unlink(missing_ok=True)
    bot.logger.log(MODULE_NAME, "Module self-deleted")


def setup(bot):
    async def _start():
        await bot.wait_until_ready()
        await _run_audit(bot)
    asyncio.ensure_future(_start())
    bot.logger.log(MODULE_NAME, "Cache audit started")
