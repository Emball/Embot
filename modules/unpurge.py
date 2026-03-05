# [file name]: unpurge.py
"""
Unpurge — Banned Member Re-Invite System for Embot
====================================================
• Console command: unpurge
• Fires as a BACKGROUND TASK via bot.loop.create_task() so the console
  thread returns immediately and the gateway heartbeat is never starved
• Scans ALL of #bot-logs (100k+ messages) for embeds containing "Member Banned"
• Saves a rolling checkpoint (logs/unpurge_checkpoint.json) after every
  message — re-running 'unpurge' resumes from exactly where it left off
• Once scanning completes, generates ONE shared unlimited invite link
• DMs every collected user that embed, with full error handling

Scan cadence (mirrors vms.py _backfill_from_date):
  • limit=None, oldest_first=True
  • asyncio.sleep(2.0) every 200 messages  ← breathing room for gateway
  • Progress logged every 1 000 messages
  • On 429 / HTTP error: checkpoint saved, task exits cleanly; re-run to resume

DM cadence:
  • 1.5 s between sends
  • Honour retry_after on 429, one retry, then skip
"""

import asyncio
import json
import re
import discord
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MODULE_NAME = "UNPURGE"

# ==================== CONFIGURATION ====================

BOT_LOGS_CHANNEL_NAME  = "bot-logs"

# Scan throttle — identical to vms.py BACKFILL_SLEEP_* values
SCAN_SLEEP_EVERY       = 200    # yield to event loop every N messages
SCAN_SLEEP_SECS        = 2.0    # seconds to sleep at each yield
SCAN_LOG_EVERY         = 1000   # log a progress line every N messages

# DM throttle
DM_DELAY_SECONDS       = 1.5

# Invite — one shared link for everyone
INVITE_MAX_USES        = 0       # 0 = unlimited
INVITE_MAX_AGE         = 604800  # 7 days in seconds


# ==================== CHECKPOINT ====================

def _checkpoint_path() -> Path:
    # Stored in logs/ next to the session log files
    return Path(__file__).parent.parent / "logs" / "unpurge_checkpoint.json"


def _load_checkpoint() -> dict:
    path = _checkpoint_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "last_message_id": data.get("last_message_id"),
                "found_ids":       list(data.get("found_ids", [])),
            }
        except Exception:
            pass
    return {"last_message_id": None, "found_ids": []}


def _save_checkpoint(last_message_id: int, found_ids: set[int]):
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "last_message_id": last_message_id,
                "found_ids":       list(found_ids),
            }, f)
        tmp.replace(path)
    except Exception:
        pass  # non-fatal — worst case we re-scan from last saved point


def _clear_checkpoint():
    try:
        _checkpoint_path().unlink(missing_ok=True)
    except Exception:
        pass


# ==================== EMBED PARSING ====================

def _embed_contains_ban(embed: discord.Embed) -> bool:
    haystack = " ".join(filter(None, [
        embed.title       or "",
        embed.description or "",
        embed.footer.text if embed.footer else "",
        embed.author.name if embed.author else "",
        *(f.name  or "" for f in embed.fields),
        *(f.value or "" for f in embed.fields),
    ]))
    return "member banned" in haystack.lower()


def _extract_user_id(embed: discord.Embed) -> Optional[int]:
    """
    Find the first Discord snowflake (17-20 digits) in any embed text.
    Matches bare snowflakes and <@id> / <@!id> mentions.
    Search order: fields → description → footer → author name.
    """
    pattern = re.compile(r'<@!?(\d{17,20})>|(?<!\d)(\d{17,20})(?!\d)')

    def _first(text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        m = pattern.search(text)
        return int(m.group(1) or m.group(2)) if m else None

    for field in embed.fields:
        uid = _first(field.value) or _first(field.name)
        if uid:
            return uid

    return (
        _first(embed.description)
        or (_first(embed.footer.text) if embed.footer else None)
        or (_first(embed.author.name) if embed.author else None)
    )


# ==================== DM EMBED ====================

def _build_dm_embed(guild: discord.Guild, invite: discord.Invite) -> discord.Embed:
    expire_ts = int(datetime.now(timezone.utc).timestamp()) + INVITE_MAX_AGE
    embed = discord.Embed(
        title="👋  You're Welcome Back!",
        description=(
            f"Hey! We wanted to reach out and let you know that your ban "
            f"from **{guild.name}** has been lifted.\n\n"
            f"Whenever you're ready, you're more than welcome to come back. "
            f"Use the invite link below to rejoin — we hope to see you around!"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="🔗  Invite Link",
        value=f"[Click here to rejoin **{guild.name}**]({invite.url})\n`{invite.url}`",
        inline=False,
    )
    embed.add_field(name="⏳  Expires", value=f"<t:{expire_ts}:R>", inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"{guild.name}  •  You can ignore this if you're not interested.")
    return embed


# ==================== BACKGROUND TASK ====================

async def _unpurge_task(bot):
    """
    Long-running background coroutine.  Scheduled via bot.loop.create_task()
    so the console thread returns immediately — the gateway heartbeat is
    never blocked regardless of how long the scan takes.
    """
    logger = bot.logger

    try:
        # ── Locate guild & channel ────────────────────────────────────────────
        guild: Optional[discord.Guild] = None
        for g in bot.guilds:
            if g.me and g.me.guild_permissions.create_instant_invite:
                guild = g
                break

        if guild is None:
            logger.log(MODULE_NAME,
                "No guild with Create Invite permission found.", "ERROR")
            print("[UNPURGE] ❌  No accessible guild with Create Invite permission.")
            return

        bot_logs: Optional[discord.TextChannel] = discord.utils.get(
            guild.text_channels, name=BOT_LOGS_CHANNEL_NAME
        )
        if bot_logs is None:
            logger.log(MODULE_NAME,
                f"#{BOT_LOGS_CHANNEL_NAME} not found in '{guild.name}'.", "ERROR")
            print(f"[UNPURGE] ❌  #{BOT_LOGS_CHANNEL_NAME} not found in '{guild.name}'.")
            return

        # ── Load checkpoint ───────────────────────────────────────────────────
        checkpoint = _load_checkpoint()
        resume_id  = checkpoint["last_message_id"]
        found_ids  : set[int] = set(checkpoint["found_ids"])

        if resume_id:
            logger.log(MODULE_NAME,
                f"Checkpoint found — resuming after message {resume_id} "
                f"({len(found_ids)} IDs already collected).")
            print(f"[UNPURGE] ♻️   Resuming from checkpoint ({len(found_ids)} IDs already collected).")
        else:
            logger.log(MODULE_NAME,
                f"No checkpoint — full scan of #{BOT_LOGS_CHANNEL_NAME} starting.")
            print(f"[UNPURGE] 🔍  Scanning #{BOT_LOGS_CHANNEL_NAME} in the background...")

        print("[UNPURGE] Progress will be logged to console.")

        # ── Scan ──────────────────────────────────────────────────────────────
        after_obj = discord.Object(id=resume_id) if resume_id else None
        msgs_seen = 0
        ban_hits  = 0
        no_id     = 0
        scan_ok   = False

        try:
            async for message in bot_logs.history(
                limit=None,
                oldest_first=True,
                after=after_obj,
            ):
                msgs_seen += 1

                for embed in message.embeds:
                    if not _embed_contains_ban(embed):
                        continue
                    ban_hits += 1
                    uid = _extract_user_id(embed)
                    if uid:
                        found_ids.add(uid)
                    else:
                        no_id += 1
                        logger.log(MODULE_NAME,
                            f"Ban embed in msg {message.id} — no extractable user ID.",
                            "WARNING")

                # Rolling checkpoint after every message (same as vms.py)
                _save_checkpoint(message.id, found_ids)

                # Yield to event loop every SCAN_SLEEP_EVERY messages
                # This is the critical fix: gateway heartbeats fire during
                # these awaits, so the bot never appears to hang.
                if msgs_seen % SCAN_SLEEP_EVERY == 0:
                    logger.log(MODULE_NAME,
                        f"Scan: {msgs_seen:,} msgs | {ban_hits} ban entries | "
                        f"{len(found_ids)} unique IDs — pausing {SCAN_SLEEP_SECS}s…")
                    await asyncio.sleep(SCAN_SLEEP_SECS)

                elif msgs_seen % SCAN_LOG_EVERY == 0:
                    logger.log(MODULE_NAME,
                        f"Scan: {msgs_seen:,} msgs | {ban_hits} ban entries | "
                        f"{len(found_ids)} unique IDs")

            scan_ok = True

        except discord.Forbidden:
            logger.log(MODULE_NAME,
                f"Missing Read Message History for #{BOT_LOGS_CHANNEL_NAME}.", "ERROR")
            print(f"[UNPURGE] ❌  Missing permission to read #{BOT_LOGS_CHANNEL_NAME}.")
            return

        except discord.HTTPException as e:
            retry = getattr(e, 'retry_after', 10.0)
            if e.status == 429:
                logger.log(MODULE_NAME,
                    f"Rate limited during scan — waiting {retry:.1f}s. "
                    "Checkpoint saved; re-run 'unpurge' to resume.", "WARNING")
                print(f"[UNPURGE] ⏳  Rate limited during scan — waiting {retry:.1f}s then stopping.")
                await asyncio.sleep(retry)
            else:
                logger.log(MODULE_NAME,
                    f"HTTP {e.status} during scan: {e} — checkpoint saved, re-run to resume.",
                    "ERROR")
                print(f"[UNPURGE] ❌  HTTP error during scan: {e}\n"
                      "            Checkpoint saved — re-run 'unpurge' to resume.")
            return

        except Exception as e:
            logger.log(MODULE_NAME,
                f"Unexpected scan error: {e} — checkpoint saved, re-run to resume.", "ERROR")
            print(f"[UNPURGE] ❌  Scan error: {e} — re-run 'unpurge' to resume.")
            return

        logger.log(MODULE_NAME,
            f"Scan complete: {msgs_seen:,} messages | {ban_hits} ban entries | "
            f"{len(found_ids)} unique IDs | {no_id} entries with no ID")
        print(
            f"[UNPURGE] ✅  Scan done — {msgs_seen:,} messages, "
            f"{ban_hits} ban entries, {len(found_ids)} unique IDs found."
        )

        if not found_ids:
            print("[UNPURGE] ℹ️   No banned user IDs found — nothing to do.")
            _clear_checkpoint()
            return

        # ── Create one shared invite ──────────────────────────────────────────
        invite_channel: Optional[discord.TextChannel] = None
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.create_instant_invite and perms.view_channel:
                invite_channel = ch
                break

        if invite_channel is None:
            logger.log(MODULE_NAME, "No channel available to create an invite.", "ERROR")
            print("[UNPURGE] ❌  No channel available to create an invite.")
            return

        try:
            invite = await invite_channel.create_invite(
                max_uses=INVITE_MAX_USES,
                max_age=INVITE_MAX_AGE,
                unique=True,
                reason="Unpurge: re-invite for previously banned members",
            )
            logger.log(MODULE_NAME,
                f"Shared invite created: {invite.url} (via #{invite_channel.name})")
            print(f"[UNPURGE] 🔗  Invite: {invite.url}")
        except discord.Forbidden:
            logger.log(MODULE_NAME, "Missing Create Invite permission.", "ERROR")
            print("[UNPURGE] ❌  Missing Create Invite permission.")
            return
        except discord.HTTPException as e:
            logger.log(MODULE_NAME, f"Failed to create invite: {e}", "ERROR")
            print(f"[UNPURGE] ❌  Failed to create invite: {e}")
            return

        dm_embed = _build_dm_embed(guild, invite)

        # ── DM each user ──────────────────────────────────────────────────────
        sent       = 0
        closed_dms = 0
        not_found  = 0
        failed     = 0

        logger.log(MODULE_NAME, f"Sending invites to {len(found_ids)} user(s)…")
        print(f"[UNPURGE] 📬  Sending invites to {len(found_ids)} user(s)…")

        for uid in found_ids:
            await asyncio.sleep(DM_DELAY_SECONDS)

            user: Optional[discord.User] = bot.get_user(uid)
            if user is None:
                try:
                    user = await bot.fetch_user(uid)
                except discord.NotFound:
                    logger.log(MODULE_NAME,
                        f"User {uid} not found (deleted/invalid) — skipping.")
                    print(f"[UNPURGE]   ⚠️  [{uid}] Deleted/invalid — skipped.")
                    not_found += 1
                    continue
                except discord.HTTPException as e:
                    logger.log(MODULE_NAME,
                        f"HTTP error fetching user {uid}: {e}", "WARNING")
                    print(f"[UNPURGE]   ⚠️  [{uid}] HTTP error fetching user — skipped.")
                    failed += 1
                    continue

            try:
                await user.send(embed=dm_embed)
                logger.log(MODULE_NAME, f"Sent invite to {user} ({uid})")
                print(f"[UNPURGE]   ✅  [{uid}] → {user}")
                sent += 1

            except discord.Forbidden:
                logger.log(MODULE_NAME,
                    f"Cannot DM {user} ({uid}) — DMs closed or bot blocked.")
                print(f"[UNPURGE]   🔒  [{uid}] DMs closed — {user} skipped.")
                closed_dms += 1

            except discord.HTTPException as e:
                if e.status == 429:
                    retry = getattr(e, 'retry_after', 5.0)
                    logger.log(MODULE_NAME,
                        f"Rate limited DMing {user} ({uid}) — waiting {retry:.1f}s.",
                        "WARNING")
                    print(f"[UNPURGE]   ⏳  Rate limited — waiting {retry:.1f}s…")
                    await asyncio.sleep(retry)
                    try:
                        await user.send(embed=dm_embed)
                        logger.log(MODULE_NAME,
                            f"Sent invite to {user} ({uid}) after rate limit wait.")
                        print(f"[UNPURGE]   ✅  [{uid}] → {user} (after retry)")
                        sent += 1
                    except Exception as retry_err:
                        logger.log(MODULE_NAME,
                            f"Retry failed for {user} ({uid}): {retry_err}", "WARNING")
                        print(f"[UNPURGE]   ❌  [{uid}] Retry failed — skipped.")
                        failed += 1
                else:
                    logger.log(MODULE_NAME,
                        f"HTTP {e.status} DMing {user} ({uid}): {e}", "WARNING")
                    print(f"[UNPURGE]   ❌  [{uid}] HTTP error — skipped.")
                    failed += 1

            except Exception as e:
                logger.log(MODULE_NAME,
                    f"Unexpected error DMing user {uid}: {e}", "ERROR")
                print(f"[UNPURGE]   ❌  [{uid}] Unexpected error — skipped.")
                failed += 1

        # ── Summary ───────────────────────────────────────────────────────────
        summary = (
            f"\n[UNPURGE] 📊  Done.\n"
            f"           ✅  Sent:           {sent}\n"
            f"           🔒  Closed DMs:     {closed_dms}\n"
            f"           👻  Not found:      {not_found}\n"
            f"           ❌  Other failures: {failed}\n"
            f"           🔗  Invite URL:     {invite.url}\n"
        )
        print(summary)
        logger.log(MODULE_NAME,
            f"Complete — sent={sent}, closed_dms={closed_dms}, "
            f"not_found={not_found}, failed={failed}, invite={invite.url}")

        _clear_checkpoint()

    finally:
        bot._unpurge_running = False


# ==================== MODULE SETUP ====================

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up Unpurge module")

    if not hasattr(bot, 'console_commands'):
        bot.logger.log(MODULE_NAME,
            "bot.console_commands not available — command not registered.", "WARNING")
        return

    async def handle_unpurge(args: str):
        """
        Console handler — returns IMMEDIATELY by scheduling the real work
        as a fire-and-forget background task on the event loop.

        This is the same pattern used by vms-resume: the console thread's
        .result(timeout=30) call in Embot.py never times out because this
        coroutine exits in microseconds.  The scan runs freely in the
        background without ever blocking the gateway heartbeat.
        """
        if getattr(bot, '_unpurge_running', False):
            print("[UNPURGE] ⚠️   Already running — wait for it to finish.")
            return

        bot._unpurge_running = True
        bot.loop.create_task(_unpurge_task(bot))
        print("[UNPURGE] ⚙️   Unpurge started in the background.")
        print("          Progress will appear in the console as it runs.")
        # Returns immediately — console thread is unblocked

    bot.console_commands['unpurge'] = {
        'description': 'Scan #bot-logs for banned members and DM a shared re-invite link',
        'handler':     handle_unpurge,
    }

    bot.logger.log(MODULE_NAME, "Registered console command: unpurge")
    bot.logger.log(MODULE_NAME, "Unpurge module setup complete")