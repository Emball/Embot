import asyncio
import discord
from pathlib import Path

MODULE_NAME = "SB_MIGRATE"

def setup(bot):
    async def _run():
        await bot.wait_until_ready()
        try:
            import starboard as sb
            guild = bot.guilds[0]
            sb_channel = guild.get_channel(sb.CONFIG["channel_id"])
            if not sb_channel:
                bot.logger.log(MODULE_NAME, "Starboard channel not found", "WARNING")
                return

            with sb._get_conn() as conn:
                rows = conn.execute(
                    "SELECT source_msg_id, starboard_msg_id, channel_id, author_id, "
                    "author_name, peak_stars, current_stars, first_starred_at, "
                    "last_updated_at, content_preview FROM starboard_entries"
                ).fetchall()

            bot.logger.log(MODULE_NAME, f"Starting delete+repost of {len(rows)} entries")
            ok = skip = fail = 0

            for row in rows:
                try:
                    try:
                        old = await sb_channel.fetch_message(int(row["starboard_msg_id"]))
                        await old.delete()
                    except discord.NotFound:
                        pass

                    src_channel = guild.get_channel(int(row["channel_id"]))
                    if not src_channel:
                        skip += 1
                        continue
                    try:
                        src_msg = await src_channel.fetch_message(int(row["source_msg_id"]))
                    except Exception:
                        skip += 1
                        continue

                    layout = sb._build_layout(src_msg, row["current_stars"])
                    new_msg = await sb_channel.send(view=layout, allowed_mentions=discord.AllowedMentions.none())

                    entry = dict(row)
                    entry["starboard_msg_id"] = str(new_msg.id)
                    sb._upsert_entry(row["source_msg_id"], entry)
                    ok += 1
                except Exception as e:
                    bot.logger.log(MODULE_NAME, f"Failed {row['source_msg_id']}: {e}", "WARNING")
                    fail += 1

            bot.logger.log(MODULE_NAME, f"Done: {ok} reposted, {skip} skipped, {fail} failed")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Migration failed", e)
        finally:
            Path(__file__).unlink(missing_ok=True)

    task = asyncio.ensure_future(_run())
    def _done(t):
        if t.exception():
            bot.logger.log(MODULE_NAME, f"Task error: {t.exception()}", "WARNING")
    task.add_done_callback(_done)
