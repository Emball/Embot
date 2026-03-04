"""
starboard.py — Dyno-style Starboard module for Embot

Configure everything in the CONFIG block below, then restart/reload the module.
No slash commands — all settings live here in the file.
"""

import discord
from discord.ext import commands
from pathlib import Path
from datetime import datetime, timezone
import json
import sqlite3
import asyncio

MODULE_NAME = "STARBOARD"

# ══════════════════════════════════════════════════════════════════════════════
#  Path helpers (must be defined before CONFIG is loaded)
# ══════════════════════════════════════════════════════════════════════════════

def _script_dir() -> Path:
    return Path(__file__).parent.parent.absolute()  # modules/ → Embot/

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                         CONFIGURATION                                   ║
# ╠══════════════════════════════════════════════════════════════════════════╣
# ║  Edit config/starboard_config.json (created automatically on first run) ║
# ║  then restart or `reload starboard` to apply changes.                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _load_starboard_config() -> dict:
    """Load config/starboard_config.json, falling back to defaults."""
    config_path = _script_dir() / "config" / "starboard_config.json"
    defaults = {
        "channel_id": 0,     # Must be set in starboard_config.json
        "threshold": 3,
        "emoji": "⭐",
        "self_star": False,
    }
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults.update(data)
        except Exception as e:
            print(f"[STARBOARD] Failed to load starboard_config.json: {e}")
    return defaults

CONFIG = _load_starboard_config()

def _db_path() -> Path:
    return _script_dir() / "db" / "starboard.db"

# ── SQLite schema ─────────────────────────────────────────────────────────

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS starboard_entries (
    source_msg_id     TEXT PRIMARY KEY,
    starboard_msg_id  TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    author_id         TEXT NOT NULL,
    author_name       TEXT NOT NULL,
    peak_stars        INTEGER NOT NULL DEFAULT 0,
    current_stars     INTEGER NOT NULL DEFAULT 0,
    first_starred_at  TEXT NOT NULL,
    last_updated_at   TEXT NOT NULL,
    content_preview   TEXT NOT NULL DEFAULT ''
);
"""

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def _init_db() -> None:
    """Initialise DB (create tables, migrate legacy JSON if present)."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── DB helpers ────────────────────────────────────────────────────────────

def _get_entry(msg_key: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM starboard_entries WHERE source_msg_id = ?", (msg_key,)
        ).fetchone()
    return dict(row) if row else None


def _upsert_entry(msg_key: str, data: dict) -> None:
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO starboard_entries
              (source_msg_id, starboard_msg_id, channel_id, author_id, author_name,
               peak_stars, current_stars, first_starred_at, last_updated_at, content_preview)
            VALUES
              (:source_msg_id, :starboard_msg_id, :channel_id, :author_id, :author_name,
               :peak_stars, :current_stars, :first_starred_at, :last_updated_at, :content_preview)
            ON CONFLICT(source_msg_id) DO UPDATE SET
              starboard_msg_id  = excluded.starboard_msg_id,
              peak_stars        = excluded.peak_stars,
              current_stars     = excluded.current_stars,
              last_updated_at   = excluded.last_updated_at,
              content_preview   = excluded.content_preview
        """, {**data, "source_msg_id": msg_key})
        conn.commit()


def _delete_entry(msg_key: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM starboard_entries WHERE source_msg_id = ?", (msg_key,))
        conn.commit()


def _entry_count() -> int:
    with _get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM starboard_entries").fetchone()[0]


# Per-message-id asyncio locks so unrelated messages never block each other.
# LRU-capped to prevent unbounded memory growth (each unique message_id that
# ever receives a reaction would otherwise live here forever).
from collections import OrderedDict

_MSG_LOCK_CAPACITY = 10_000

class _LRULockCache:
    """Thread-safe LRU cache of asyncio.Lock objects keyed by message ID string."""
    def __init__(self, capacity: int):
        self._cap = capacity
        self._cache: OrderedDict[str, asyncio.Lock] = OrderedDict()

    def get(self, key: str) -> asyncio.Lock:
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            self._cache[key] = asyncio.Lock()
            if len(self._cache) > self._cap:
                self._cache.popitem(last=False)
        return self._cache[key]

_msg_locks = _LRULockCache(_MSG_LOCK_CAPACITY)


def _get_msg_lock(msg_id: str) -> asyncio.Lock:
    return _msg_locks.get(msg_id)


# ── Embed / content builders ──────────────────────────────────────────────

def _star_label(count: int) -> str:
    if count >= 15:
        return "🌟"
    elif count >= 10:
        return "💫"
    elif count >= 5:
        return "⭐"
    else:
        return "✨"


def _build_content(count: int, source_channel: discord.TextChannel) -> str:
    return f"{_star_label(count)} **{count}** | {source_channel.mention}"


def _build_embed(message: discord.Message, count: int) -> discord.Embed:
    embed = discord.Embed(
        description=message.content or "",
        color=discord.Color.gold(),
        timestamp=message.created_at,
    )
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)

    # Attach first image attachment
    if message.attachments:
        first = message.attachments[0]
        if first.content_type and first.content_type.startswith("image/"):
            embed.set_image(url=first.url)

    # Fall back to embed image/thumbnail (e.g. Tenor GIFs, link previews)
    if not embed.image and message.embeds:
        for e in message.embeds:
            if e.image:
                embed.set_image(url=e.image.url)
                break
            if e.thumbnail:
                embed.set_image(url=e.thumbnail.url)
                break

    embed.set_footer(text=f"#{message.channel.name}")
    return embed


def _count_reactions(message: discord.Message, emoji: str) -> int:
    for reaction in message.reactions:
        if str(reaction.emoji) == emoji:
            return reaction.count
    return 0


# ── Core handler ──────────────────────────────────────────────────────────

async def _handle_reaction(bot: commands.Bot, payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    if not CONFIG["channel_id"]:
        return
    if str(payload.emoji) != CONFIG["emoji"]:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    starboard_channel = guild.get_channel(CONFIG["channel_id"])
    if starboard_channel is None:
        bot.logger.log(MODULE_NAME, f"Starboard channel {CONFIG['channel_id']} not found", "WARNING")
        return

    # Never process reactions inside the starboard channel itself
    if payload.channel_id == CONFIG["channel_id"]:
        return

    source_channel = guild.get_channel(payload.channel_id)
    if source_channel is None:
        return

    msg_key = str(payload.message_id)

    async with _get_msg_lock(msg_key):

        try:
            message = await source_channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        # Self-star guard
        if not CONFIG["self_star"] and payload.user_id == message.author.id:
            return

        count = _count_reactions(message, CONFIG["emoji"])
        entry = _get_entry(msg_key)

        # ── Below threshold ──────────────────────────────────────────────
        if count < CONFIG["threshold"]:
            if entry:
                try:
                    sb_msg = await starboard_channel.fetch_message(int(entry["starboard_msg_id"]))
                    await sb_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                _delete_entry(msg_key)
            return

        # ── At or above threshold ────────────────────────────────────────
        content = _build_content(count, source_channel)
        embed = _build_embed(message, count)

        if entry:
            if count == entry.get("current_stars") and entry.get("starboard_msg_id"):
                return

            try:
                sb_msg = await starboard_channel.fetch_message(int(entry["starboard_msg_id"]))
                await sb_msg.edit(content=content, embed=embed)
            except discord.NotFound:
                sb_msg = await starboard_channel.send(content=content, embed=embed)
                entry["starboard_msg_id"] = str(sb_msg.id)
            except discord.Forbidden:
                bot.logger.log(MODULE_NAME, "Missing permissions to edit starboard message", "WARNING")
                return

            _upsert_entry(msg_key, {
                **entry,
                "current_stars":  count,
                "peak_stars":     max(entry.get("peak_stars", count), count),
                "last_updated_at": _now_iso(),
            })

        else:
            try:
                sb_msg = await starboard_channel.send(content=content, embed=embed)
            except discord.Forbidden:
                bot.logger.log(MODULE_NAME, "Missing permissions to post to starboard channel", "WARNING")
                return

            _upsert_entry(msg_key, {
                "starboard_msg_id": str(sb_msg.id),
                "channel_id":       str(source_channel.id),
                "author_id":        str(message.author.id),
                "author_name":      message.author.display_name,
                "peak_stars":       count,
                "current_stars":    count,
                "first_starred_at": _now_iso(),
                "last_updated_at":  _now_iso(),
                "content_preview":  (message.content or "")[:100],
            })
            bot.logger.log(MODULE_NAME, f"Posted to starboard: msg {msg_key} by {message.author} ({count} stars)")


# ── Setup ─────────────────────────────────────────────────────────────────

def setup(bot: commands.Bot):
    _init_db()

    if not CONFIG["channel_id"]:
        bot.logger.log(MODULE_NAME, "⚠️  channel_id is not set in CONFIG — starboard will not post until configured", "WARNING")
    else:
        bot.logger.log(
            MODULE_NAME,
            f"Starboard → channel {CONFIG['channel_id']} | "
            f"threshold {CONFIG['threshold']} | emoji {CONFIG['emoji']} | "
            f"entries loaded: {_entry_count()}"
        )

    @bot.listen("on_raw_reaction_add")
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        await _handle_reaction(bot, payload)

    @bot.listen("on_raw_reaction_remove")
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        await _handle_reaction(bot, payload)

    @bot.listen("on_raw_message_delete")
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        """Remove the starboard post when the source message is deleted."""
        if payload.guild_id is None or not CONFIG["channel_id"]:
            return

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return

        msg_key = str(payload.message_id)

        async with _get_msg_lock(msg_key):
            entry = _get_entry(msg_key)
            if not entry:
                return

            starboard_channel = guild.get_channel(CONFIG["channel_id"])
            if starboard_channel:
                try:
                    sb_msg = await starboard_channel.fetch_message(int(entry["starboard_msg_id"]))
                    await sb_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            _delete_entry(msg_key)

    bot.logger.log(MODULE_NAME, "Starboard module loaded.")
