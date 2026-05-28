"""Temporary: audit songcache channel vs DB, log orphan messages."""
import sqlite3, asyncio
from pathlib import Path

DB_PATH = "/home/embis/Documents/Embot/db/musicarchive.db"
CHANNEL_ID = 1509452076282023946

async def _run(bot):
    await asyncio.sleep(5)
    chan = bot.get_channel(CHANNEL_ID)
    if not chan:
        bot.logger.log("AUDIT", "Channel not found", "ERROR")
        return
    db = sqlite3.connect(DB_PATH)
    known_ids = {r[0] for r in db.execute("SELECT DISTINCT message_id FROM song_cache").fetchall()}
    db.close()
    orphans = []
    async for msg in chan.history(limit=None):
        if msg.author == bot.user and msg.attachments:
            if str(msg.id) not in known_ids:
                files = [a.filename for a in msg.attachments]
                orphans.append((str(msg.id), files))
    bot.logger.log("AUDIT", f"Scan done. DB msgs={len(known_ids)} orphan_msgs={len(orphans)}")
    for msg_id, files in orphans:
        bot.logger.log("AUDIT", f"ORPHAN msg={msg_id} files={files}")
    Path(__file__).unlink(missing_ok=True)

async def setup(bot):
    asyncio.get_event_loop().create_task(_run(bot))
