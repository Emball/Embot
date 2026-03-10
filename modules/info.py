"""
info.py — Auto-syncing info and commands embeds for Embot.

Two embed managers, each mirroring the RulesManager pattern from moderation.py:

  • InfoManager   → #info channel  — welcome, links, archive usage
  • CommandsManager → #commands channel — user-facing command reference

Content lives in  config/info_config.json  (created automatically if absent).
Only the posted message IDs + content hashes live in the DB.

Admin commands
  /infoset     [channel]   — force-sync / re-post the #info embed
  /commandsset [channel]   — force-sync / re-post the #commands embed

Both embeds auto-update within 60 s of any config file change.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

MODULE_NAME = "INFO"

# ═══════════════════════════════════════════════════════════════════════════════
#  Path helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _script_dir() -> Path:
    """Root Embot/ directory (two levels up from modules/)."""
    return Path(__file__).parent.parent.absolute()

def _db_path() -> str:
    p = _script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "info.db")

def _config_path() -> Path:
    p = _script_dir() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p / "info_config.json"

# ═══════════════════════════════════════════════════════════════════════════════
#  Default config  (written to disk the first time the module loads)
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG: dict = {
    # ── Channel names ──────────────────────────────────────────────────────────
    "info_channel_name":     "info",
    "commands_channel_name": "commands",

    # ── #info embed ────────────────────────────────────────────────────────────
    "info": {
        "title":       "Welcome to the Ǝmball Pit",
        "description": (
            "You've found the best place on the internet for Eminem edits, remasters, "
            "leaks, and community discussion. Below you'll find everything you need to "
            "get started."
        ),
        "color": 0x5865F2,
        "footer": "Use /archive to grab any song · DMs open for remaster requests",

        # Each section becomes one embed field (inline: false unless specified)
        "sections": [
            {
                "name": "🎙️ Archive Command",
                "value": (
                    "The archive is the heart of the bot — instant access to the complete "
                    "Eminem catalogue in FLAC or MP3, sent straight to your DMs.\n\n"
                    "**`/archive [flac/mp3] [song title] [version (optional)]`**\n"
                    "```\n"
                    "/archive flac antichrist 2005 version\n"
                    "/archive mp3 mockingbird\n"
                    "/archive flac lose yourself instrumental\n"
                    "```"
                )
            },
            {
                "name": "🔗 Links",
                "value": (
                    "[My Edit & Remaster Archive](https://example.com/archive)\n"
                    "[Eminem Leak Tracker](https://example.com/leaks)\n"
                    "[Emball Community Edits Tracker](https://example.com/community)\n"
                    "[The Complete Eminem Archive](https://example.com/full)\n"
                    "[PayPal Tip Jar](https://paypal.me/example)"
                )
            },
            {
                "name": "💸 Paid Remaster Requests",
                "value": (
                    "I also do paid remaster requests for **$10**. "
                    "If you want a song remastered, DM me."
                )
            }
        ]
    },

    # ── #commands embed ────────────────────────────────────────────────────────
    "commands": {
        "title":       "Bot Commands",
        "description": "Everything available to regular members.",
        "color":       0x2ECC71,
        "footer":      "Slash commands only · responses are ephemeral where noted",

        "sections": [
            {
                "name": "🗂️ /archive",
                "value": (
                    "`/archive [flac/mp3] [title] [version?]`\n"
                    "Retrieve any song from the complete Eminem catalogue. "
                    "Sent to your DMs. Version is optional — use it for "
                    "live recordings, instrumentals, demos, remixes, etc.\n"
                    "```\n"
                    "/archive flac antichrist 2005 version\n"
                    "/archive mp3  mockingbird\n"
                    "/archive flac lose yourself instrumental\n"
                    "```"
                )
            },
            {
                "name": "🎙️ Voice Messages",
                "value": (
                    "`/vmtranscribe disable` — Stop the bot auto-transcribing your VMs.\n"
                    "`/vmtranscribe enable` — Turn auto-transcription back on.\n"
                    "`/vmstats` — Stats and fun facts about the server's VM archive."
                )
            },
            {
                "name": "🏆 Community & XP",
                "value": (
                    "`/xp [@member?]` — Check your XP or another member's.\n"
                    "`/leaderboard` — Top 10 XP rankings.\n"
                    "`/submission_info [message_id]` — Look up a community submission."
                )
            },
            {
                "name": "🎵 Music Player",
                "value": (
                    "`/play [url/query]` — Play audio in your voice channel.\n"
                    "`/pause` · `/resume` · `/skip` · `/stop` · `/leave`\n"
                    "`/queue` — View the current queue.\n"
                    "`/loop` — Toggle looping."
                )
            },
            {
                "name": "🎱 Magic Emball",
                "value": "`/magicemball [question]` — Ask the magic 8-ball anything."
            },
            {
                "name": "📜 Other",
                "value": (
                    "`/rules` — View the server rules.\n"
                    "`?archive` · `?tracker` · `?edits` · `?tips` — Quick links "
                    "(type in any channel)."
                )
            }
        ]
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
#  Config I/O
# ═══════════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    """Load info_config.json, seeding defaults if it doesn't exist yet."""
    path = _config_path()
    if not path.exists():
        _save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Back-fill any missing top-level keys from defaults
        changed = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
                changed = True
        if changed:
            _save_config(data)
        return data
    except Exception as e:
        print(f"[{MODULE_NAME}] Failed to load info_config.json: {e}")
        return DEFAULT_CONFIG

def _save_config(data: dict) -> None:
    """Atomically write info_config.json."""
    path = _config_path()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise

# ═══════════════════════════════════════════════════════════════════════════════
#  DB helpers
# ═══════════════════════════════════════════════════════════════════════════════

DB_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS info_embed_state (
    embed_key   TEXT NOT NULL,   -- 'info' or 'commands'
    guild_id    TEXT NOT NULL,
    message_id  INTEGER,
    content_hash TEXT,
    PRIMARY KEY (embed_key, guild_id)
);
"""

def _init_db() -> None:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()

def _db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def _db_exec(query: str, params: tuple = ()) -> None:
    c = _db_conn()
    try:
        c.execute(query, params)
        c.commit()
    finally:
        c.close()

def _db_one(query: str, params: tuple = ()):
    c = _db_conn()
    try:
        return c.execute(query, params).fetchone()
    finally:
        c.close()

# ═══════════════════════════════════════════════════════════════════════════════
#  EmbedManager  (handles one channel — reusable for both info and commands)
# ═══════════════════════════════════════════════════════════════════════════════

class EmbedManager:
    """
    Manages a single auto-syncing embed in a named channel.

    embed_key   — 'info' or 'commands'  (used as DB key and config section key)
    channel_cfg_key — config key that stores the target channel name
    """

    def __init__(self, bot, embed_key: str, channel_cfg_key: str):
        self.bot             = bot
        self.embed_key       = embed_key
        self.channel_cfg_key = channel_cfg_key
        self._watch_task: Optional[asyncio.Task] = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cfg(self) -> dict:
        return _load_config()

    def _embed_data(self) -> Optional[dict]:
        return self._cfg().get(self.embed_key)

    def _channel_name(self) -> str:
        return self._cfg().get(self.channel_cfg_key, self.embed_key)

    @staticmethod
    def _hash(data: dict) -> str:
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _get_state(self, guild_id: int) -> tuple:
        """Returns (message_id, content_hash) or (None, None)."""
        row = _db_one(
            "SELECT message_id, content_hash FROM info_embed_state "
            "WHERE embed_key=? AND guild_id=?",
            (self.embed_key, str(guild_id))
        )
        if row:
            return row["message_id"], row["content_hash"]
        return None, None

    def _save_state(self, guild_id: int, message_id: int, content_hash: str) -> None:
        _db_exec(
            "INSERT INTO info_embed_state (embed_key, guild_id, message_id, content_hash) "
            "VALUES (?,?,?,?) ON CONFLICT(embed_key, guild_id) DO UPDATE SET "
            "message_id=excluded.message_id, content_hash=excluded.content_hash",
            (self.embed_key, str(guild_id), message_id, content_hash)
        )

    def _get_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        return discord.utils.get(guild.text_channels, name=self._channel_name())

    # ── Embed builder ─────────────────────────────────────────────────────────

    def build_embed(self, data: dict) -> discord.Embed:
        embed = discord.Embed(
            title=data.get("title", ""),
            description=data.get("description", ""),
            color=data.get("color", 0x5865F2),
            timestamp=datetime.now(timezone.utc),
        )
        for section in data.get("sections", []):
            embed.add_field(
                name=section.get("name", "\u200b"),
                value=section.get("value", "\u200b"),
                inline=section.get("inline", False),
            )
        if data.get("footer"):
            embed.set_footer(text=data["footer"])
        return embed

    # ── Core sync ─────────────────────────────────────────────────────────────

    async def sync(self, guild: discord.Guild, *, force: bool = False) -> bool:
        """
        Post or update the embed in the target channel.
        Returns True if a new message was posted / embed was edited.
        """
        data = self._embed_data()
        if not data:
            self.bot.logger.log(MODULE_NAME,
                f"No '{self.embed_key}' section in info_config.json — skipping sync", "WARNING")
            return False

        current_hash = self._hash(data)
        channel      = self._get_channel(guild)
        if not channel:
            self.bot.logger.log(MODULE_NAME,
                f"#{self._channel_name()} not found in {guild.name} — skipping", "WARNING")
            return False

        posted_msg_id, posted_hash = self._get_state(guild.id)

        # If the hash matches and we're not forcing, there's nothing to do
        if not force and posted_msg_id and current_hash == posted_hash:
            try:
                await channel.fetch_message(posted_msg_id)
                return False   # message exists and is up to date
            except discord.NotFound:
                pass           # message was deleted — fall through and repost

        # Delete any stale embed(s) the bot previously posted in this channel
        if posted_msg_id:
            try:
                stale = await channel.fetch_message(posted_msg_id)
                await stale.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        # Also sweep for any other bot embeds left in the channel
        try:
            async for msg in channel.history(limit=50):
                if msg.author == guild.me and msg.embeds:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass

        # Post fresh embed
        embed = self.build_embed(data)
        try:
            new_msg = await channel.send(embed=embed)
            self._save_state(guild.id, new_msg.id, current_hash)
            self.bot.logger.log(MODULE_NAME,
                f"#{self._channel_name()} embed posted (msg {new_msg.id}) in {guild.name}")
            return True
        except discord.Forbidden:
            self.bot.logger.log(MODULE_NAME,
                f"Missing permissions to post in #{self._channel_name()}", "WARNING")
            return False
        except Exception as e:
            self.bot.logger.log(MODULE_NAME,
                f"Failed to post #{self._channel_name()} embed: {e}", "ERROR")
            return False

    # ── on_ready ──────────────────────────────────────────────────────────────

    async def on_ready(self, guild: discord.Guild) -> None:
        await self.sync(guild)

    # ── File watcher loop ─────────────────────────────────────────────────────

    def start_watcher(self, guild: discord.Guild) -> None:
        self._watch_guild = guild
        if not (self._watch_task and not self._watch_task.done()):
            self._watch_task = self.bot.loop.create_task(self._watch_loop())

    async def _watch_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            try:
                guild = getattr(self, "_watch_guild", None)
                if not guild:
                    continue
                data = self._embed_data()
                if not data:
                    continue
                current_hash = self._hash(data)
                _, posted_hash = self._get_state(guild.id)
                if current_hash != posted_hash:
                    self.bot.logger.log(MODULE_NAME,
                        f"info_config.json change detected — resyncing "
                        f"#{self._channel_name()}")
                    await self.sync(guild, force=True)
            except Exception as e:
                self.bot.logger.log(MODULE_NAME,
                    f"Watcher loop error ({self.embed_key}): {e}", "WARNING")

# ═══════════════════════════════════════════════════════════════════════════════
#  Module setup
# ═══════════════════════════════════════════════════════════════════════════════

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up info module")

    _init_db()
    _load_config()   # seed defaults to disk if first run

    info_mgr  = EmbedManager(bot, embed_key="info",     channel_cfg_key="info_channel_name")
    cmd_mgr   = EmbedManager(bot, embed_key="commands", channel_cfg_key="commands_channel_name")

    # ── Sync on ready ──────────────────────────────────────────────────────────

    @bot.listen("on_ready")
    async def _info_on_ready():
        for guild in bot.guilds:
            await info_mgr.on_ready(guild)
            await cmd_mgr.on_ready(guild)
            info_mgr.start_watcher(guild)
            cmd_mgr.start_watcher(guild)
        bot.logger.log(MODULE_NAME, "Info embeds synced on ready")

    # ── /infoset ──────────────────────────────────────────────────────────────

    @bot.tree.command(
        name="infoset",
        description="[Admin] Force-refresh the #info channel embed from info_config.json"
    )
    @app_commands.default_permissions(administrator=True)
    async def slash_infoset(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        posted = await info_mgr.sync(interaction.guild, force=True)
        cfg    = _load_config()
        ch     = cfg.get("info_channel_name", "info")
        if posted:
            await interaction.followup.send(
                f"✅ #{ch} embed has been refreshed.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"ℹ️ #{ch} embed is already up to date "
                f"(or the channel wasn't found).", ephemeral=True)

    # ── /commandsset ──────────────────────────────────────────────────────────

    @bot.tree.command(
        name="commandsset",
        description="[Admin] Force-refresh the #commands channel embed from info_config.json"
    )
    @app_commands.default_permissions(administrator=True)
    async def slash_commandsset(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        posted = await cmd_mgr.sync(interaction.guild, force=True)
        cfg    = _load_config()
        ch     = cfg.get("commands_channel_name", "commands")
        if posted:
            await interaction.followup.send(
                f"✅ #{ch} embed has been refreshed.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"ℹ️ #{ch} embed is already up to date "
                f"(or the channel wasn't found).", ephemeral=True)

    bot.logger.log(MODULE_NAME, "Info module setup complete")