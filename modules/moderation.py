# [file name]: moderation.py
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import re
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
import asyncio
from typing import Optional, Dict, List
import sqlite3
import json
import os
from pathlib import Path
import io
from dataclasses import dataclass, field as _field
from PIL import Image, ImageDraw, ImageFont
import pytz
import tempfile
from collections import deque
from cryptography.fernet import Fernet

MODULE_NAME = "MODERATION"

# ==================== STANDARD ERROR STRINGS ====================
# These are UI copy — they stay in code. Everything else lives in the DB.

ERROR_NO_PERMISSION      = "❌ You need a moderation role (Moderator, Admin, or Owner) to use this command."
ERROR_REASON_REQUIRED    = "❌ You must provide a reason for this action."
ERROR_CANNOT_ACTION_SELF = "❌ You cannot perform this action on yourself."
ERROR_CANNOT_ACTION_BOT  = "❌ I cannot perform this action on myself."
ERROR_HIGHER_ROLE        = "❌ You cannot perform this action on someone with a higher or equal role."

# ==================== PATH HELPERS ====================

def _script_dir() -> Path:
    """Root Embot/ directory (two levels up from modules/)."""
    return Path(__file__).parent.parent.absolute()

def _db_path() -> str:
    p = _script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "moderation.db")

# ==================== DATABASE SCHEMA ====================

DB_SCHEMA = """
PRAGMA journal_mode=WAL;

-- ── Role persistence ──────────────────────────────────────────────────────────
-- Replaces member_roles.json
CREATE TABLE IF NOT EXISTS mod_member_roles (
    guild_id    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role_ids    TEXT NOT NULL,   -- JSON array of integer role IDs
    saved_at    TEXT NOT NULL,
    username    TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- ── Strikes / warnings ───────────────────────────────────────────────────────
-- Replaces moderation_strikes.json
CREATE TABLE IF NOT EXISTS mod_strikes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    reason      TEXT NOT NULL
);

-- ── Active mutes ──────────────────────────────────────────────────────────────
-- Replaces muted_users.json
CREATE TABLE IF NOT EXISTS mod_mutes (
    guild_id            TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    reason              TEXT NOT NULL,
    moderator           TEXT NOT NULL,
    timestamp           TEXT NOT NULL,
    duration_seconds    INTEGER,
    expiry_time         TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- ── Pending oversight actions ─────────────────────────────────────────────────
-- Replaces mod_oversight_data.json
CREATE TABLE IF NOT EXISTS mod_pending_actions (
    action_id           TEXT PRIMARY KEY,
    action              TEXT NOT NULL,
    moderator_id        INTEGER NOT NULL,
    moderator           TEXT NOT NULL,
    user_id             INTEGER,
    user_name           TEXT,
    reason              TEXT NOT NULL,
    guild_id            INTEGER NOT NULL,
    channel_id          INTEGER,
    message_id          INTEGER,
    timestamp           TEXT NOT NULL,
    context_messages    TEXT,           -- JSON array
    duration            TEXT,
    additional          TEXT,           -- JSON object
    flags               TEXT,           -- JSON array
    embed_id_inchat     INTEGER,
    embed_id_botlog     INTEGER,
    status              TEXT NOT NULL DEFAULT 'pending'
);

-- ── Ban appeals ───────────────────────────────────────────────────────────────
-- Replaces ban_appeals.json
CREATE TABLE IF NOT EXISTS mod_appeals (
    appeal_id           TEXT PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    guild_id            INTEGER NOT NULL,
    appeal_text         TEXT NOT NULL,
    submitted_at        TEXT NOT NULL,
    deadline            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    votes_for           TEXT NOT NULL DEFAULT '[]',     -- JSON array of user_id strings
    votes_against       TEXT NOT NULL DEFAULT '[]',     -- JSON array of user_id strings
    channel_message_id  INTEGER
);

-- ── Ban reversal invites ──────────────────────────────────────────────────────
-- Replaces ban_reversal_invites.json
CREATE TABLE IF NOT EXISTS mod_invites (
    invite_key  TEXT PRIMARY KEY,   -- "{guild_id}_{user_id}"
    code        TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

-- ── Rules state ───────────────────────────────────────────────────────────────
-- Replaces data/rules_state.json
CREATE TABLE IF NOT EXISTS mod_rules_state (
    guild_id    TEXT PRIMARY KEY,
    message_id  INTEGER,
    rules_hash  TEXT
);

-- ── Deletion attempt log (transient — cleared after daily report) ─────────────
CREATE TABLE IF NOT EXISTS mod_deletion_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id          TEXT NOT NULL,
    deleter         TEXT NOT NULL,
    deleter_id      INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    original_title  TEXT,
    is_warning      INTEGER NOT NULL DEFAULT 0
);

-- ── Startup log ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mod_startup_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    startup_time INTEGER NOT NULL
);
"""

# ── Default seed data ─────────────────────────────────────────────────────────
# Applied once on first init; never overwrites existing rows.

# ── Config file path ─────────────────────────────────────────────────────────

def _config_path() -> Path:
    p = _script_dir() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p / "moderation.json"

def _load_config() -> dict:
    """Load config/moderation.json. Raises FileNotFoundError if missing."""
    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Missing required config file: {path}\n"
            "Ensure config/moderation.json is present (it should be committed to the repo)."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(data: dict) -> None:
    """Atomically write config/moderation.json."""
    path = _config_path()
    import tempfile
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


def _migrate(db_path: str) -> None:
    """
    One-time migration: pull config/roles/wordlists out of the DB and
    merge rules.json into config/moderation.json, then drop those DB tables.

    Safe to call on every startup — it checks whether migration is needed first.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        needs_migration = bool(
            tables & {"mod_config", "mod_elevated_roles", "mod_word_lists"}
        )
        rules_file = Path(__file__).parent.parent / "rules.json"
        has_rules_json = rules_file.exists()

        if not needs_migration and not has_rules_json:
            return  # Nothing to do

        # Load existing JSON config (or defaults) as the merge base
        cfg = _load_config()

        # ── Pull values from mod_config ───────────────────────────────────────
        if "mod_config" in tables:
            for row in conn.execute("SELECT key, value FROM mod_config").fetchall():
                k, v = row["key"], row["value"]
                # Cast numeric strings back to int where the default is int
                # Cast to int if value looks like a plain integer
                try:
                    if str(v) == str(int(v)):
                        v = int(v)
                except (ValueError, TypeError):
                    pass
                cfg[k] = v

        # ── Pull elevated roles ───────────────────────────────────────────────
        if "mod_elevated_roles" in tables:
            rows = conn.execute("SELECT role_name FROM mod_elevated_roles").fetchall()
            if rows:
                cfg["elevated_roles"] = [r["role_name"] for r in rows]

        # ── Pull word lists ───────────────────────────────────────────────────
        if "mod_word_lists" in tables:
            rows = conn.execute(
                "SELECT category, term FROM mod_word_lists ORDER BY category, id"
            ).fetchall()
            if rows:
                known_categories = {"child_safety", "racial_slurs", "tos_violations", "banned_words"}
                wl: dict = {c: [] for c in known_categories}
                for r in rows:
                    wl.setdefault(r["category"], []).append(r["term"])
                cfg["word_lists"] = wl

        # ── Merge rules.json ──────────────────────────────────────────────────
        if has_rules_json:
            try:
                with open(rules_file, "r", encoding="utf-8") as f:
                    rules_data = json.load(f)
                cfg["rules"] = rules_data
            except Exception as e:
                import sys
                print(f"[MODERATION] Migration: failed to read rules.json: {e}", file=sys.stderr)

        # ── Write merged config ───────────────────────────────────────────────
        _save_config(cfg)

        # ── Drop migrated DB tables ───────────────────────────────────────────
        for tbl in ("mod_word_lists", "mod_elevated_roles", "mod_config"):
            if tbl in tables:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.commit()

        # ── Remove rules.json ─────────────────────────────────────────────────
        if has_rules_json:
            try:
                rules_file.unlink()
            except Exception:
                pass

        import sys
        print("[MODERATION] Migration complete → config/moderation.json", file=sys.stderr)
    finally:
        conn.close()


def _init_db(db_path: str) -> None:
    """Create schema on first run. Safe to call on every startup."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ==================== DB CONNECTION HELPERS ====================

def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c

def _db_exec(db_path: str, query: str, params: tuple = ()):
    c = _conn(db_path)
    try:
        c.execute(query, params)
        c.commit()
    finally:
        c.close()

def _db_one(db_path: str, query: str, params: tuple = ()):
    c = _conn(db_path)
    try:
        return c.execute(query, params).fetchone()
    finally:
        c.close()

def _db_all(db_path: str, query: str, params: tuple = ()):
    c = _conn(db_path)
    try:
        return c.execute(query, params).fetchall()
    finally:
        c.close()


# ==================== CONFIG ACCESSOR ====================

class ModConfig:
    """
    Reads user-configurable values from config/moderation.json.
    Mutations (add/remove word, add/remove role) write back to the JSON file atomically.
    Call reload() to pick up manual edits without restarting.
    """

    def __init__(self):
        self._data: dict = _load_config()

    def reload(self) -> None:
        """Re-read config/moderation.json from disk."""
        self._data = _load_config()

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        """Persist a single key change to disk."""
        self._data[key] = value
        _save_config(self._data)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def owner_id(self) -> int:
        return self.get_int("owner_id")

    @property
    def join_logs_channel_id(self) -> int:
        return self.get_int("join_logs_channel_id")

    @property
    def bot_logs_channel_id(self) -> int:
        return self.get_int("bot_logs_channel_id")

    @property
    def rules_channel_name(self) -> str:
        return self.get("rules_channel_name", "rules")

    @property
    def min_reason_length(self) -> int:
        return self.get_int("min_reason_length", 10)

    @property
    def muted_role_name(self) -> str:
        return self.get("muted_role_name", "Muted")

    @property
    def report_time_cst(self) -> str:
        return self.get("report_time_cst", "00:00")

    @property
    def context_message_count(self) -> int:
        return self.get_int("context_message_count", 30)

    @property
    def invite_cleanup_days(self) -> int:
        return self.get_int("invite_cleanup_days", 7)

    # ── Elevated roles ────────────────────────────────────────────────────────

    def get_elevated_roles(self) -> List[str]:
        return list(self._data.get("elevated_roles", []))

    def add_elevated_role(self, role_name: str) -> None:
        roles = self.get_elevated_roles()
        if role_name not in roles:
            roles.append(role_name)
            self._data["elevated_roles"] = roles
            _save_config(self._data)

    def remove_elevated_role(self, role_name: str) -> None:
        roles = self.get_elevated_roles()
        if role_name in roles:
            roles.remove(role_name)
            self._data["elevated_roles"] = roles
            _save_config(self._data)

    # ── Word lists ────────────────────────────────────────────────────────────

    def get_word_list(self, category: str) -> List[str]:
        return list(self._data.get("word_lists", {}).get(category, []))

    def add_word(self, category: str, term: str) -> None:
        wl = self._data.setdefault("word_lists", {})
        terms = wl.setdefault(category, [])
        if term not in terms:
            terms.append(term)
            _save_config(self._data)

    def remove_word(self, category: str, term: str) -> None:
        wl = self._data.get("word_lists", {})
        terms = wl.get(category, [])
        if term in terms:
            terms.remove(term)
            _save_config(self._data)

    @property
    def child_safety(self) -> List[str]:
        return self.get_word_list("child_safety")

    @property
    def racial_slurs(self) -> List[str]:
        return self.get_word_list("racial_slurs")

    @property
    def tos_violations(self) -> List[str]:
        return self.get_word_list("tos_violations")

    @property
    def banned_words(self) -> List[str]:
        return self.get_word_list("banned_words")

    # ── Rules content ─────────────────────────────────────────────────────────

    def get_rules(self) -> Optional[dict]:
        """Return the rules dict from config, or None if absent/empty."""
        rules = self._data.get("rules")
        if rules and rules.get("rules"):
            return rules
        return None

    def save_rules(self, rules_data: dict) -> None:
        """Write updated rules content back to config."""
        self._data["rules"] = rules_data
        _save_config(self._data)


# ==================== HELPERS ====================

def has_elevated_role(member: discord.Member, cfg: ModConfig) -> bool:
    if member.guild.owner_id == member.id:
        return True
    elevated = cfg.get_elevated_roles()
    return any(role.name in elevated for role in member.roles)

def validate_reason(reason: Optional[str], min_len: int) -> tuple:
    if not reason or reason.strip() == "" or reason == "No reason provided":
        return False, ERROR_REASON_REQUIRED
    if len(reason) < min_len:
        return False, f"❌ Reason must be at least {min_len} characters long."
    return True, None

def parse_duration(duration: str) -> tuple:
    """Parse a duration string like '10m', '2h', '1d'. Returns (seconds, label)."""
    if not duration:
        return None, "Permanent"
    m = re.match(r'^(\d+)([smhd])$', duration.lower())
    if not m:
        return None, "Permanent"
    value, unit = int(m.group(1)), m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    labels      = {'s': 'second', 'm': 'minute', 'h': 'hour', 'd': 'day'}
    seconds = value * multipliers[unit]
    label   = f"{value} {labels[unit]}{'s' if value != 1 else ''}"
    return seconds, label

def get_event_logger(bot):
    return getattr(bot, '_logger_event_logger', None)

def matches_banned_term(term: str, content_lower: str) -> bool:
    term_lower = term.lower()
    if "://" in term_lower or "www." in term_lower:
        return term_lower in content_lower
    return bool(re.search(r'\b' + re.escape(term_lower) + r'\b', content_lower))


# ==================== UNIFIED CONTEXT ====================

class ModContext:
    """
    Wraps either a discord.Interaction (slash) or commands.Context (prefix)
    into a single interface so command logic never has to branch on type.
    """
    def __init__(self, source):
        self._source = source
        self._replied = False

        if isinstance(source, discord.Interaction):
            self.guild   = source.guild
            self.channel = source.channel
            self.author  = source.user
            self.bot     = source.client
            self.message = None
        else:
            self.guild   = source.guild
            self.channel = source.channel
            self.author  = source.author
            self.bot     = source.bot
            self.message = source.message

    async def reply(self, content=None, *, embed=None, ephemeral=False, delete_after=None):
        msg_obj = None
        if isinstance(self._source, discord.Interaction):
            if not self._replied:
                self._replied = True
                await self._source.response.send_message(
                    content=content, embed=embed, ephemeral=ephemeral)
                if not ephemeral:
                    try:
                        msg_obj = await self._source.original_response()
                    except Exception:
                        pass
            else:
                msg_obj = await self._source.followup.send(
                    content=content, embed=embed, ephemeral=ephemeral)
        else:
            msg_obj = await self._source.send(content=content, embed=embed)
            if delete_after and msg_obj:
                await msg_obj.delete(delay=delete_after)
        return msg_obj.id if msg_obj else None

    async def error(self, message: str):
        if isinstance(self._source, discord.Interaction):
            await self.reply(message, ephemeral=True)
        else:
            await self.reply(message, delete_after=8)

    async def defer(self):
        if isinstance(self._source, discord.Interaction):
            await self._source.response.defer()
        self._replied = True

    async def followup(self, content=None, *, embed=None, ephemeral=False):
        if isinstance(self._source, discord.Interaction):
            msg = await self._source.followup.send(
                content=content, embed=embed, ephemeral=ephemeral)
            return msg.id if msg else None
        else:
            msg = await self._source.send(content=content, embed=embed)
            return msg.id if msg else None


# ==================== VIEWS & MODALS ====================

class BanAppealView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @ui.button(label="Submit Appeal", style=discord.ButtonStyle.primary, emoji="📝")
    async def appeal_button(self, interaction: discord.Interaction, button: ui.Button):
        if not hasattr(interaction.client, 'moderation') or \
                not hasattr(interaction.client.moderation, 'submit_appeal'):
            await interaction.response.send_message(
                "❌ Appeal system not available.", ephemeral=True)
            return
        modal = BanAppealModal(interaction.client.moderation, self.guild_id)
        await interaction.response.send_modal(modal)


class ActionReviewView(ui.View):
    def __init__(self, moderation_system, action_id: str, action: Dict):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.action_id  = action_id
        self.action     = action

    @ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.moderation.approve_action(self.action_id)
        if success:
            await interaction.response.send_message(
                "✅ Action approved and removed from pending.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "❌ Failed to approve action.", ephemeral=True)

    @ui.button(label="Revert", style=discord.ButtonStyle.red, emoji="↩️")
    async def revert_button(self, interaction: discord.Interaction, button: ui.Button):
        guild = self.moderation.bot.get_guild(self.action['guild_id'])
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
            return
        success = await self.moderation.revert_action(self.action_id, guild)
        if success:
            await interaction.response.send_message(
                "↩️ Action reverted successfully.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "❌ Failed to revert action.", ephemeral=True)

    @ui.button(label="View Chat", style=discord.ButtonStyle.gray, emoji="💬")
    async def view_chat_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.action.get('channel_id') or not self.action.get('message_id'):
            await interaction.response.send_message(
                "❌ No chat link available.", ephemeral=True)
            return
        guild_id   = self.action['guild_id']
        channel_id = self.action['channel_id']
        message_id = self.action['message_id']
        jump_link  = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        await interaction.response.send_message(
            f"📍 [Jump to message]({jump_link})", ephemeral=True)


class AppealVoteView(ui.View):
    def __init__(self, moderation_system, appeal_id: str):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.appeal_id  = appeal_id

    def _updated_embed(self, message: discord.Message,
                        votes_for: list, votes_against: list) -> discord.Embed:
        """Return a copy of the message embed with the vote counts updated."""
        old = message.embeds[0] if message.embeds else None
        embed = discord.Embed(
            title=old.title if old else "📝 Ban Appeal",
            description=old.description if old else "",
            color=old.color if old else 0x9b59b6,
            timestamp=old.timestamp if old else datetime.now(timezone.utc),
        )
        for field in (old.fields if old else []):
            if field.name == "Votes":
                continue  # will re-add below
            embed.add_field(name=field.name, value=field.value, inline=field.inline)
        embed.add_field(
            name="Votes",
            value=f"✅ Yes: **{len(votes_for)}**  ·  ❌ No: **{len(votes_against)}**",
            inline=False,
        )
        if old and old.footer:
            embed.set_footer(text=old.footer.text)
        return embed

    @ui.button(label="Vote Yes", style=discord.ButtonStyle.green,
               emoji="✅", custom_id="appeal_accept")
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        cfg = self.moderation.cfg
        if not has_elevated_role(interaction.user, cfg):
            await interaction.response.send_message(
                "❌ Only moderators can vote on appeals.", ephemeral=True)
            return
        appeal = self.moderation._get_appeal(self.appeal_id)
        if not appeal:
            await interaction.response.send_message(
                "❌ Appeal no longer exists.", ephemeral=True)
            return

        uid           = str(interaction.user.id)
        votes_for     = json.loads(appeal["votes_for"])
        votes_against = json.loads(appeal["votes_against"])

        if uid in votes_for:
            await interaction.response.send_message(
                "You already voted Yes.", ephemeral=True)
            return
        if uid in votes_against:
            votes_against.remove(uid)
        votes_for.append(uid)

        self.moderation._update_appeal_votes(
            self.appeal_id, votes_for, votes_against)
        updated_embed = self._updated_embed(interaction.message, votes_for, votes_against)
        await interaction.response.edit_message(embed=updated_embed, view=self)

    @ui.button(label="Vote No", style=discord.ButtonStyle.red,
               emoji="❌", custom_id="appeal_deny")
    async def deny_button(self, interaction: discord.Interaction, button: ui.Button):
        cfg = self.moderation.cfg
        if not has_elevated_role(interaction.user, cfg):
            await interaction.response.send_message(
                "❌ Only moderators can vote on appeals.", ephemeral=True)
            return
        appeal = self.moderation._get_appeal(self.appeal_id)
        if not appeal:
            await interaction.response.send_message(
                "❌ Appeal no longer exists.", ephemeral=True)
            return

        uid           = str(interaction.user.id)
        votes_for     = json.loads(appeal["votes_for"])
        votes_against = json.loads(appeal["votes_against"])

        if uid in votes_against:
            await interaction.response.send_message(
                "You already voted No.", ephemeral=True)
            return
        if uid in votes_for:
            votes_for.remove(uid)
        votes_against.append(uid)

        self.moderation._update_appeal_votes(
            self.appeal_id, votes_for, votes_against)
        updated_embed = self._updated_embed(interaction.message, votes_for, votes_against)
        await interaction.response.edit_message(embed=updated_embed, view=self)


class BanAppealModal(ui.Modal, title="Ban Appeal"):
    appeal_text = ui.TextInput(
        label="Why should you be unbanned?",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why you believe the ban should be lifted...",
        required=True,
        max_length=1000,
    )

    def __init__(self, moderation_system, guild_id: int):
        super().__init__()
        self.moderation = moderation_system
        self.guild_id   = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.moderation.submit_appeal(
            interaction.user.id, self.guild_id, self.appeal_text.value)
        embed = discord.Embed(
            title="✅ Appeal Submitted",
            description="Your ban appeal has been submitted and will be reviewed.",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="What happens next?",
            value="Your appeal has been posted to the staff team. They will vote on it "
                  "over the next 24 hours and you'll be notified of the outcome automatically.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== MEDIA SAFETY SCANNER ====================

_EXPLICIT_LABELS = {
    "EXPOSED_ANUS", "EXPOSED_BUTTOCKS", "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F", "EXPOSED_GENITALIA_M", "EXPOSED_BELLY",
}
_NUDENET_THRESHOLD = 0.45
_AGE_THRESHOLD     = 20
_SCAN_IMAGE_EXTS   = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif'}
_SCAN_VIDEO_EXTS   = {'.mp4', '.mov', '.webm', '.avi', '.mkv'}

_nudenet_model  = None
_deepface_ready = False

def _load_nudenet():
    global _nudenet_model
    if _nudenet_model is None:
        try:
            from nudenet import NudeClassifier  # type: ignore
            _nudenet_model = NudeClassifier()
        except ImportError:
            pass
        except Exception as e:
            import logging
            logging.getLogger("MediaScanner").error(f"NudeNet load failed: {e}")
    return _nudenet_model

def _load_deepface():
    global _deepface_ready
    try:
        import deepface  # noqa  type: ignore
        _deepface_ready = True
    except ImportError:
        _deepface_ready = False
    return _deepface_ready

import logging as _logging
_scanner_log = _logging.getLogger("MediaScanner")

@dataclass
class _FileScanResult:
    filename: str
    scannable: bool
    explicit: bool = False
    min_age: Optional[float] = None
    blocked: bool = False
    reason: str = ""

@dataclass
class ScanVerdict:
    blocked: bool
    safe_files: list = _field(default_factory=list)
    blocked_files: list = _field(default_factory=list)


class MediaScanner:
    def __init__(self, bot, cfg: ModConfig, get_bot_logs_fn):
        self.bot            = bot
        self.cfg            = cfg
        self._get_bot_logs  = get_bot_logs_fn
        self._models_loaded = False
        self._load_lock     = asyncio.Lock()

    async def _ensure_models(self):
        if self._models_loaded:
            return
        async with self._load_lock:
            if self._models_loaded:
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _load_nudenet)
            await loop.run_in_executor(None, _load_deepface)
            self._models_loaded = True

    async def scan_files(self, files_data: list, guild=None, context: str = "") -> ScanVerdict:
        await self._ensure_models()
        loop = asyncio.get_running_loop()
        safe, blocked = [], []

        for entry in files_data:
            filename: str = entry['filename']
            data: bytes   = entry['data']
            ext = os.path.splitext(filename.lower())[1]

            if ext not in _SCAN_IMAGE_EXTS and ext not in _SCAN_VIDEO_EXTS:
                safe.append(entry)
                continue

            result = _FileScanResult(filename=filename, scannable=True)
            try:
                is_explicit = await loop.run_in_executor(
                    None, self._nudenet_scan, data, ext)
                result.explicit = is_explicit
                if is_explicit:
                    min_age = await loop.run_in_executor(
                        None, self._deepface_age, data)
                    result.min_age = min_age
                    if min_age is not None and min_age < _AGE_THRESHOLD:
                        result.blocked = True
                        result.reason  = (f"Explicit + apparent age "
                                          f"{min_age:.1f} < {_AGE_THRESHOLD}")
            except Exception as e:
                result.blocked = True
                result.reason  = f"Scan error (blocked as precaution): {e}"
                _scanner_log.error(f"Scan error [{filename}]: {e}")

            if result.blocked:
                blocked.append(result)
            else:
                safe.append(entry)

        verdict = ScanVerdict(blocked=bool(blocked), safe_files=safe, blocked_files=blocked)
        if verdict.blocked:
            await self._alert(verdict, guild, context)
        return verdict

    def _nudenet_scan(self, data: bytes, ext: str) -> bool:
        if _nudenet_model is None:
            return False
        if ext in _SCAN_VIDEO_EXTS:
            data = self._first_frame(data)
            if data is None:
                return False
            ext = '.jpg'
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            result = _nudenet_model.classify(path)
            preds  = list(result.values())[0] if result else {}
            return any(
                lbl in _EXPLICIT_LABELS and conf >= _NUDENET_THRESHOLD
                for lbl, conf in preds.items()
            )
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def _deepface_age(self, data: bytes) -> Optional[float]:
        if not _deepface_ready:
            return None
        try:
            from deepface import DeepFace  # type: ignore
            import numpy as np
            from PIL import Image as _Image
            arr     = np.array(_Image.open(io.BytesIO(data)).convert("RGB"))
            results = DeepFace.analyze(arr, actions=["age"],
                                        enforce_detection=False, silent=True)
            if isinstance(results, dict):
                results = [results]
            ages = [float(r["age"]) for r in results if r.get("age") is not None]
            return min(ages) if ages else None
        except Exception as e:
            _scanner_log.warning(f"DeepFace age error: {e}")
            return None

    def _first_frame(self, video_data: bytes) -> Optional[bytes]:
        try:
            import cv2, numpy as np  # type: ignore
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp:
                tmp.write(video_data)
                path = tmp.name
            try:
                cap = cv2.VideoCapture(path)
                ret, frame = cap.read()
                cap.release()
                if not ret or frame is None:
                    return None
                _, buf = cv2.imencode('.jpg', frame)
                return buf.tobytes()
            finally:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        except ImportError:
            return None
        except Exception as e:
            _scanner_log.error(f"First-frame extraction failed: {e}")
            return None

    async def _alert(self, verdict: ScanVerdict, guild, context: str):
        summary = "\n".join(
            f"• `{r.filename}` — {r.reason}" for r in verdict.blocked_files)
        embed = discord.Embed(
            title="🚫 Re-host Blocked — Potential Illegal Content",
            description=(
                "One or more files were **blocked from re-hosting**. "
                "Encrypted copies have been deleted. "
                "**No image content is included in this report.**"
            ),
            color=0xff0000,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Context", value=context or "*(none)*", inline=False)
        embed.add_field(
            name=f"Blocked ({len(verdict.blocked_files)})",
            value=summary[:1024], inline=False)
        embed.add_field(
            name="Action Required",
            value=(
                "Review via Discord audit logs. If content is illegal, report to "
                "Discord Trust & Safety and NCMEC CyberTipline (missingkids.org)."
            ),
            inline=False,
        )
        embed.set_footer(text="No flagged content was uploaded to Discord CDN")
        try:
            owner = await self.bot.fetch_user(self.cfg.owner_id)
            if owner:
                await owner.send(embed=embed)
        except Exception:
            pass
        if guild:
            try:
                ch = self._get_bot_logs(guild)
                if ch:
                    await ch.send(embed=embed)
            except Exception:
                pass


# ==================== RULES MANAGER ====================

class RulesManager:
    """
    Manages the server rules embed in the #rules channel.
    Rules content lives in config/moderation.json under the "rules" key.
    Only the *state* (posted message ID + hash) is persisted in the DB.
    """

    def __init__(self, bot, db_path: str, cfg: ModConfig):
        self.bot      = bot
        self._db      = db_path
        self.cfg      = cfg

    # ── DB-backed state ───────────────────────────────────────────────────────

    def _get_state(self, guild_id: int) -> tuple:
        """Returns (message_id, rules_hash) for the guild, or (None, None)."""
        row = _db_one(self._db,
                      "SELECT message_id, rules_hash FROM mod_rules_state WHERE guild_id=?",
                      (str(guild_id),))
        if row:
            return row["message_id"], row["rules_hash"]
        return None, None

    def _save_state(self, guild_id: int, message_id: int, rules_hash: str):
        _db_exec(self._db,
                 "INSERT INTO mod_rules_state (guild_id, message_id, rules_hash) VALUES (?,?,?) "
                 "ON CONFLICT(guild_id) DO UPDATE SET "
                 "message_id=excluded.message_id, rules_hash=excluded.rules_hash",
                 (str(guild_id), message_id, rules_hash))

    # ── Rules file helpers ────────────────────────────────────────────────────

    def load_rules(self) -> Optional[dict]:
        """Load rules content from config/moderation.json."""
        data = self.cfg.get_rules()
        if data is None:
            self.bot.logger.log("RULES", "No rules content found in config/moderation.json", "WARNING")
        return data

    def save_rules(self, data: dict) -> None:
        """Persist updated rules content back to config/moderation.json."""
        self.cfg.save_rules(data)

    @staticmethod
    def _hash_rules(data: dict) -> str:
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def get_rule_text(self, rule_number: int) -> Optional[str]:
        data = self.load_rules()
        if not data:
            return None
        for rule in data.get("rules", []):
            if rule.get("number") == rule_number:
                return f"**Rule {rule['number']} — {rule['title']}**: {rule['description']}"
        return None

    def list_rules_summary(self) -> list:
        data = self.load_rules()
        if not data:
            return []
        return [f"Rule {r['number']} — {r['title']}" for r in data.get("rules", [])]

    def build_embed(self, data: dict) -> discord.Embed:
        color = data.get("color", 0x3498db)
        embed = discord.Embed(
            title=f"📜  {data.get('title', 'Server Rules')}",
            description=data.get("description", ""),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        for rule in data.get("rules", []):
            embed.add_field(
                name=f"Rule {rule['number']}  ·  {rule['title']}",
                value=rule["description"],
                inline=False,
            )
        embed.set_footer(text=data.get("footer", "Please follow the rules"))
        return embed

    def _get_rules_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        return discord.utils.get(guild.text_channels, name=self.cfg.rules_channel_name)

    async def sync(self, guild: discord.Guild, *, force: bool = False) -> bool:
        data = self.load_rules()
        if not data:
            return False

        current_hash = self._hash_rules(data)
        channel      = self._get_rules_channel(guild)
        if not channel:
            self.bot.logger.log("RULES", f"#rules channel not found in {guild.name}", "WARNING")
            return False

        posted_msg_id, posted_hash = self._get_state(guild.id)

        existing_ok = False
        if posted_msg_id and not force:
            try:
                msg = await channel.fetch_message(posted_msg_id)
                if current_hash == posted_hash:
                    existing_ok = True
                else:
                    await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                self.bot.logger.log("RULES", f"Could not fetch rules message: {e}", "WARNING")

        if existing_ok:
            return False

        try:
            async for msg in channel.history(limit=50):
                if msg.author == guild.me and msg.embeds:
                    await msg.delete()
        except Exception:
            pass

        embed = self.build_embed(data)
        try:
            new_msg = await channel.send(embed=embed)
            self._save_state(guild.id, new_msg.id, current_hash)
            self.bot.logger.log("RULES", f"Rules embed posted (message {new_msg.id})")
            return True
        except Exception as e:
            self.bot.logger.log("RULES", f"Failed to post rules embed: {e}", "ERROR")
            return False

    async def on_ready(self, guild: discord.Guild):
        await self.sync(guild)

    def start_watcher(self, guild: discord.Guild):
        self._watch_guild = guild
        if not self._watcher_task_running():
            self._watch_task = self.bot.loop.create_task(self._watch_loop())

    def _watcher_task_running(self) -> bool:
        return hasattr(self, "_watch_task") and not self._watch_task.done()

    async def _watch_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            try:
                guild = self._watch_guild
                if guild:
                    data = self.load_rules()
                    if data:
                        current_hash = self._hash_rules(data)
                        _, posted_hash = self._get_state(guild.id)
                        if current_hash != posted_hash:
                            self.bot.logger.log(
                                "RULES", "rules content change detected — syncing embed")
                            await self.sync(guild, force=True)
            except Exception as e:
                self.bot.logger.log("RULES", f"Watcher error: {e}", "WARNING")


# ==================== MODERATION SYSTEM ====================

class ModerationSystem:
    """
    Unified moderation and oversight system.
    All persistent state lives in /data/moderation.db — no JSON files.
    """

    def __init__(self, bot):
        self.bot     = bot
        self._db     = _db_path()
        # Run migration before anything else (safe no-op if already done)
        _migrate(self._db)

        # Initialise DB schema
        _init_db(self._db)

        self.cfg     = ModConfig()

        # Per-process Fernet key
        self._fernet = Fernet(Fernet.generate_key())

        # Encrypted media staging directory
        self.media_dir = _script_dir() / "cache" / "moderation"
        self.media_dir.mkdir(exist_ok=True)

        # Pre-upload safety scanner
        self.scanner = MediaScanner(bot, self.cfg, self._get_bot_logs_channel)

        # Purge orphaned .enc files from previous run
        _purged = 0
        for _enc in self.media_dir.glob("*.enc"):
            try:
                _enc.unlink()
                _purged += 1
            except Exception:
                pass
        if _purged:
            bot.logger.log(MODULE_NAME,
                f"Purged {_purged} orphaned .enc file(s) from previous session")

        # In-memory caches (not persisted — rebuilt from events each run)
        BOT_LOG_CACHE_SIZE      = 500
        self._bot_log_cache: Dict[int, Dict] = {}
        self._bot_log_order: deque           = deque()
        self._bot_log_cache_size             = BOT_LOG_CACHE_SIZE
        self._deletion_warnings: Dict[int, str] = {}

        # Message cache for context (guild_id -> channel_id -> list)
        self.message_cache = {}

        # Media cache index: message_id -> {'files': [...], 'author_id', 'guild_id'}
        self.media_cache = {}

        # Tracked embeds for deletion monitoring
        self.tracked_embeds = {}

        # Record this startup
        _db_exec(self._db,
                 "INSERT INTO mod_startup_log (startup_time) VALUES (?)",
                 (int(datetime.now(timezone.utc).timestamp()),))

        # Background tasks
        self.check_expired_mutes.start()
        self.cleanup_invites.start()
        self.send_daily_report.start()
        self.resolve_expired_appeals.start()

        bot.logger.log(MODULE_NAME, "Moderation system initialised (SQLite)")

    # ==================== DB HELPERS ====================

    def _conn(self) -> sqlite3.Connection:
        return _conn(self._db)

    def _exec(self, query: str, params: tuple = ()):
        _db_exec(self._db, query, params)

    def _one(self, query: str, params: tuple = ()):
        return _db_one(self._db, query, params)

    def _all(self, query: str, params: tuple = ()):
        return _db_all(self._db, query, params)

    # ==================== ROLE PERSISTENCE ====================

    def save_member_roles(self, member: discord.Member):
        gk, uk = str(member.guild.id), str(member.id)
        role_ids = json.dumps([r.id for r in member.roles if r.id != member.guild.id])
        self._exec(
            "INSERT INTO mod_member_roles (guild_id, user_id, role_ids, saved_at, username) "
            "VALUES (?,?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "role_ids=excluded.role_ids, saved_at=excluded.saved_at, username=excluded.username",
            (gk, uk, role_ids, datetime.now(timezone.utc).isoformat(), str(member)),
        )

    async def restore_member_roles(self, member: discord.Member):
        gk, uk = str(member.guild.id), str(member.id)
        row = self._one(
            "SELECT role_ids FROM mod_member_roles WHERE guild_id=? AND user_id=?",
            (gk, uk))
        if not row:
            return
        role_ids = json.loads(row["role_ids"])
        roles = [member.guild.get_role(rid) for rid in role_ids]
        roles = [r for r in roles if r]
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Role persistence - restoring previous roles")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to restore roles for {member}", e)

    # ==================== STRIKE SYSTEM ====================

    def add_strike(self, user_id, reason) -> int:
        self._exec(
            "INSERT INTO mod_strikes (user_id, timestamp, reason) VALUES (?,?,?)",
            (str(user_id), datetime.now(timezone.utc).isoformat(), reason),
        )
        return self.get_strikes(user_id)

    def get_strikes(self, user_id) -> int:
        row = self._one(
            "SELECT COUNT(*) AS cnt FROM mod_strikes WHERE user_id=?",
            (str(user_id),))
        return row["cnt"] if row else 0

    def get_strike_details(self, user_id) -> list:
        rows = self._all(
            "SELECT timestamp, reason FROM mod_strikes WHERE user_id=? ORDER BY id",
            (str(user_id),))
        return [{"timestamp": r["timestamp"], "reason": r["reason"]} for r in rows]

    def clear_strikes(self, user_id) -> bool:
        count = self.get_strikes(user_id)
        if count == 0:
            return False
        self._exec("DELETE FROM mod_strikes WHERE user_id=?", (str(user_id),))
        return True

    # ==================== MUTE MANAGER ====================

    def add_mute(self, guild_id, user_id, reason, moderator, duration_seconds=None):
        expiry = None
        if duration_seconds:
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).isoformat()
        self._exec(
            "INSERT INTO mod_mutes (guild_id, user_id, reason, moderator, timestamp, "
            "duration_seconds, expiry_time) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "reason=excluded.reason, moderator=excluded.moderator, "
            "timestamp=excluded.timestamp, duration_seconds=excluded.duration_seconds, "
            "expiry_time=excluded.expiry_time",
            (str(guild_id), str(user_id), reason, str(moderator),
             datetime.now(timezone.utc).isoformat(), duration_seconds, expiry),
        )

    def remove_mute(self, guild_id, user_id):
        self._exec(
            "DELETE FROM mod_mutes WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )

    def is_muted(self, guild_id, user_id) -> bool:
        row = self._one(
            "SELECT 1 FROM mod_mutes WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)))
        return row is not None

    def get_expired_mutes(self) -> list:
        now  = datetime.now(timezone.utc).isoformat()
        rows = self._all(
            "SELECT guild_id, user_id FROM mod_mutes "
            "WHERE expiry_time IS NOT NULL AND expiry_time <= ?",
            (now,))
        return [{"guild_id": int(r["guild_id"]), "user_id": int(r["user_id"])} for r in rows]

    # ==================== MEDIA CACHE HELPERS ====================

    def _encrypt_to_disk(self, message_id: int, index: int, data: bytes) -> Path:
        encrypted = self._fernet.encrypt(data)
        path      = self.media_dir / f"{message_id}_{index}.enc"
        path.write_bytes(encrypted)
        return path

    def _decrypt_from_disk(self, path: Path) -> bytes:
        return self._fernet.decrypt(path.read_bytes())

    def _delete_media_files(self, message_id: int):
        entry = self.media_cache.pop(message_id, None)
        if not entry:
            return
        for f in entry['files']:
            try:
                f['path'].unlink(missing_ok=True)
            except Exception:
                pass

    async def cache_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        guild_id   = str(message.guild.id)
        channel_id = str(message.channel.id)
        self.message_cache.setdefault(guild_id, {}).setdefault(channel_id, [])

        downloaded = []
        for idx, att in enumerate(message.attachments):
            try:
                data = await att.read()
                path = self._encrypt_to_disk(message.id, idx, data)
                downloaded.append({
                    'filename':     att.filename,
                    'path':         path,
                    'content_type': att.content_type or 'application/octet-stream',
                    'url':          att.url,
                })
            except Exception as e:
                self.bot.logger.log(
                    MODULE_NAME,
                    f"Failed to cache attachment {att.filename}: {e}", "WARNING")

        if downloaded:
            self.media_cache[message.id] = {
                'files':     downloaded,
                'author_id': message.author.id,
                'guild_id':  message.guild.id,
            }

        msg_data = {
            'id':          message.id,
            'author':      str(message.author),
            'author_id':   message.author.id,
            'content':     message.content,
            'timestamp':   message.created_at.isoformat(),
            'attachments': [att.url for att in message.attachments],
            'embeds':      len(message.embeds),
        }
        cache_list = self.message_cache[guild_id][channel_id]
        cache_list.append(msg_data)
        if len(cache_list) > 100:
            evicted = cache_list.pop(0)
            if evicted.get('id'):
                self._delete_media_files(evicted['id'])

    def get_context_messages(
        self, guild_id: int, channel_id: int,
        around_message_id: int, count: int = None
    ) -> List[Dict]:
        if count is None:
            count = self.cfg.context_message_count
        messages = self.message_cache.get(str(guild_id), {}).get(str(channel_id), [])
        target_idx = next(
            (i for i, m in enumerate(messages) if m['id'] == around_message_id), None)
        if target_idx is None:
            return messages[-count:]
        half  = count // 2
        start = max(0, target_idx - half)
        end   = min(len(messages), target_idx + half + 1)
        return messages[start:end]

    def generate_context_screenshot(
        self, messages: List[Dict], highlighted_msg_id: Optional[int] = None
    ) -> io.BytesIO:
        width       = 800
        line_height = 60
        padding     = 20
        height      = len(messages) * line_height + padding * 2
        img         = Image.new('RGB', (width, height), color='#36393f')
        draw        = ImageDraw.Draw(img)
        try:
            font      = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
            font_bold = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except Exception:
            font      = ImageFont.load_default()
            font_bold = font
        y = padding
        for msg in messages:
            if highlighted_msg_id and msg['id'] == highlighted_msg_id:
                draw.rectangle([0, y - 5, width, y + line_height - 5], fill='#4a4d52')
            timestamp   = datetime.fromisoformat(msg['timestamp']).strftime("%H:%M")
            author_text = f"{msg['author']} - {timestamp}"
            draw.text((padding, y), author_text, fill='#7289da', font=font_bold)
            content = msg['content'][:100]
            if msg['content'] and len(msg['content']) > 100:
                content += "..."
            if not content and msg['attachments']:
                content = "[Attachment]"
            if not content and msg['embeds'] > 0:
                content = "[Embed]"
            if not content:
                content = "[Empty message]"
            draw.text((padding, y + 20), content, fill='#dcddde', font=font)
            y += line_height
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer

    # ==================== OVERSIGHT: LOGGING ACTIONS ====================

    async def log_mod_action(self, action_data: Dict) -> Optional[str]:
        if action_data['action'] in ['mute', 'warn', 'timeout']:
            return None
        action_id = (f"{action_data['guild_id']}_{action_data['action']}_"
                     f"{int(datetime.now(timezone.utc).timestamp())}_{uuid.uuid4().hex[:8]}")
        context_messages = []
        if 'message_id' in action_data and 'channel_id' in action_data:
            context_messages = self.get_context_messages(
                action_data['guild_id'],
                action_data['channel_id'],
                action_data['message_id'],
            )
        self._exec(
            "INSERT OR REPLACE INTO mod_pending_actions "
            "(action_id, action, moderator_id, moderator, user_id, user_name, reason, "
            " guild_id, channel_id, message_id, timestamp, context_messages, duration, "
            " additional, flags, embed_id_inchat, embed_id_botlog, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'pending')",
            (
                action_id,
                action_data['action'],
                action_data['moderator_id'],
                action_data['moderator'],
                action_data.get('user_id'),
                action_data.get('user'),
                action_data['reason'],
                action_data['guild_id'],
                action_data.get('channel_id'),
                action_data.get('message_id'),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(context_messages),
                action_data.get('duration'),
                json.dumps(action_data.get('additional', {})),
                json.dumps([]),
            ),
        )
        self.bot.logger.log(
            MODULE_NAME,
            f"Logged mod action: {action_id} by {action_data['moderator']}")
        return action_id

    def _get_pending_action(self, action_id: str) -> Optional[Dict]:
        row = self._one(
            "SELECT * FROM mod_pending_actions WHERE action_id=?", (action_id,))
        if not row:
            return None
        return self._row_to_action(row)

    def _row_to_action(self, row) -> Dict:
        return {
            'id':               row["action_id"],
            'action':           row["action"],
            'moderator_id':     row["moderator_id"],
            'moderator':        row["moderator"],
            'user_id':          row["user_id"],
            'user':             row["user_name"],
            'reason':           row["reason"],
            'guild_id':         row["guild_id"],
            'channel_id':       row["channel_id"],
            'message_id':       row["message_id"],
            'timestamp':        row["timestamp"],
            'context_messages': json.loads(row["context_messages"] or "[]"),
            'duration':         row["duration"],
            'additional':       json.loads(row["additional"] or "{}"),
            'flags':            json.loads(row["flags"] or "[]"),
            'embed_ids': {
                'inchat': row["embed_id_inchat"],
                'botlog': row["embed_id_botlog"],
            },
            'status': row["status"],
        }

    def resolve_pending_action(self, user_id: int, action_type: str):
        # Delete only the most recent pending action of this type for the user,
        # to avoid accidentally wiping multiple records if the user was banned
        # more than once.
        row = self._one(
            "SELECT action_id FROM mod_pending_actions "
            "WHERE user_id=? AND action=? AND status='pending' "
            "ORDER BY timestamp DESC LIMIT 1",
            (user_id, action_type),
        )
        if row:
            self._exec(
                "DELETE FROM mod_pending_actions WHERE action_id=?",
                (row["action_id"],),
            )

    async def send_cached_media_to_logs(
        self, guild: discord.Guild, message_id: int,
        author_str: str, reason: str, extra_content: str = None
    ):
        bot_logs = self._get_bot_logs_channel(guild)
        if not bot_logs:
            return
        cached = self.media_cache.get(message_id)
        if not cached or not cached['files']:
            return
        files = []
        for f in cached['files']:
            try:
                data = self._decrypt_from_disk(f['path'])
                files.append(discord.File(fp=io.BytesIO(data), filename=f['filename']))
            except Exception as e:
                self.bot.logger.log(
                    MODULE_NAME,
                    f"Failed to decrypt cached file {f['filename']}: {e}", "WARNING")
        if not files:
            return
        embed = discord.Embed(
            title=reason, color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User",       value=author_str,           inline=True)
        embed.add_field(name="Message ID", value=str(message_id),      inline=True)
        if extra_content:
            embed.add_field(name="Message Content",
                            value=extra_content[:1024] or "*empty*", inline=False)
        embed.set_footer(text=f"{len(files)} attachment(s) re-hosted below")
        await self.send_bot_log(guild, embed, files_data=cached['files'])

    def track_embed(self, message_id: int, action_id: str, embed_type: str):
        self.tracked_embeds[message_id] = {'action_id': action_id, 'type': embed_type}
        _col_map = {'inchat': 'embed_id_inchat', 'botlog': 'embed_id_botlog'}
        col = _col_map.get(embed_type)
        if col is None:
            self.bot.logger.log(MODULE_NAME, f"track_embed: unknown embed_type {embed_type!r}", "WARNING")
            return
        queries = {
            'embed_id_inchat': "UPDATE mod_pending_actions SET embed_id_inchat=? WHERE action_id=?",
            'embed_id_botlog': "UPDATE mod_pending_actions SET embed_id_botlog=? WHERE action_id=?",
        }
        self._exec(queries[col], (message_id, action_id))

    async def handle_embed_deletion(self, message_id: int):
        if message_id not in self.tracked_embeds:
            return
        info      = self.tracked_embeds.pop(message_id)
        action_id = info['action_id']
        embed_type = info['type']

        row = self._one(
            "SELECT flags FROM mod_pending_actions WHERE action_id=?", (action_id,))
        if not row:
            return
        flags = json.loads(row["flags"] or "[]")

        if embed_type == 'inchat':
            if 'inchat_deleted' not in flags:
                flags.append('inchat_deleted')
        else:
            if 'botlog_deleted' not in flags:
                flags.append('botlog_deleted')

        inchat_deleted = 'inchat_deleted' in flags
        botlog_deleted = 'botlog_deleted' in flags
        if inchat_deleted and botlog_deleted:
            if 'red_flag' not in flags:
                flags.append('red_flag')
                self.bot.logger.log(
                    MODULE_NAME,
                    f"🚩 RED FLAG: Both embeds deleted for action {action_id}", "WARNING")
        elif inchat_deleted or botlog_deleted:
            if 'yellow_flag' not in flags:
                flags.append('yellow_flag')
                self.bot.logger.log(
                    MODULE_NAME,
                    f"⚠️ YELLOW FLAG: Embed deleted for action {action_id}", "WARNING")

        self._exec(
            "UPDATE mod_pending_actions SET flags=? WHERE action_id=?",
            (json.dumps(flags), action_id),
        )

    # ==================== OVERSIGHT: ACTION REVIEW ====================

    async def approve_action(self, action_id: str) -> bool:
        row = self._one(
            "SELECT 1 FROM mod_pending_actions WHERE action_id=?", (action_id,))
        if not row:
            return False
        self._exec(
            "DELETE FROM mod_pending_actions WHERE action_id=?", (action_id,))
        self.bot.logger.log(MODULE_NAME, f"Action {action_id} approved")
        return True

    async def revert_action(self, action_id: str, guild: discord.Guild) -> bool:
        action = self._get_pending_action(action_id)
        if not action:
            return False
        if action['action'] == 'ban':
            return await self._revert_ban(action, guild)
        elif action['action'] == 'mute':
            return await self._revert_mute(action, guild)
        else:
            self._exec(
                "DELETE FROM mod_pending_actions WHERE action_id=?", (action_id,))
            return True

    async def _revert_ban(self, action: Dict, guild: discord.Guild) -> bool:
        try:
            user_id = action['user_id']
            user    = await self.bot.fetch_user(user_id)
            await guild.unban(user, reason="Ban reverted after review")
            invite_link = await self._create_ban_reversal_invite(guild, user_id)
            try:
                embed = discord.Embed(
                    title="Ban Reverted",
                    description=f"After reviewing your case, we've decided to revert "
                                f"your ban from **{guild.name}**.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Rejoin Server",
                                value=f"You can rejoin using this invite:\n{invite_link}",
                                inline=False)
                embed.set_footer(text="This invite is for you only and will not expire")
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            bot_logs = self._get_bot_logs_channel(guild)
            if bot_logs:
                log_embed = discord.Embed(
                    title="Ban Reverted (Review System)",
                    description=f"**{user}** ({user_id}) has been unbanned after review.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
                log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
                await self.send_bot_log(guild, log_embed)
            self._exec(
                "DELETE FROM mod_pending_actions WHERE action_id=?", (action['id'],))
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to revert ban {action['id']}", e)
            return False

    async def _revert_mute(self, action: Dict, guild: discord.Guild) -> bool:
        try:
            user_id    = action['user_id']
            member     = guild.get_member(user_id)
            if not member:
                return False
            muted_role = discord.utils.get(guild.roles, name=self.cfg.muted_role_name)
            if muted_role and muted_role in member.roles:
                await member.remove_roles(muted_role, reason="Mute reverted after review")
            try:
                embed = discord.Embed(
                    title="Mute Reverted",
                    description=f"After reviewing your case, your mute in "
                                f"**{guild.name}** has been reverted.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            bot_logs = self._get_bot_logs_channel(guild)
            if bot_logs:
                log_embed = discord.Embed(
                    title="Mute Reverted (Review System)",
                    description=f"**{member}** has been unmuted after review.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
                log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
                await self.send_bot_log(guild, log_embed)
            self._exec(
                "DELETE FROM mod_pending_actions WHERE action_id=?", (action['id'],))
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to revert mute {action['id']}", e)
            return False

    async def _create_ban_reversal_invite(self, guild: discord.Guild, user_id: int) -> str:
        try:
            channel = next(
                (ch for ch in guild.text_channels
                 if ch.permissions_for(guild.me).create_instant_invite),
                None,
            )
            if not channel:
                return "Could not create invite - no suitable channel"
            invite = await channel.create_invite(
                max_uses=1, max_age=0, unique=True,
                reason=f"Ban reversal for user {user_id}")
            key = f"{guild.id}_{user_id}"
            self._exec(
                "INSERT INTO mod_invites (invite_key, code, user_id, guild_id, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(invite_key) DO UPDATE SET "
                "code=excluded.code, created_at=excluded.created_at",
                (key, invite.code, user_id, guild.id, datetime.now(timezone.utc).isoformat()),
            )
            return invite.url
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to create ban reversal invite", e)
            return "Error creating invite"

    def _get_bot_logs_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        ch_id = self.cfg.bot_logs_channel_id
        if ch_id:
            return guild.get_channel(ch_id)
        logger = get_event_logger(self.bot)
        if logger:
            return logger.get_bot_logs_channel(guild)
        return None

    def _register_bot_log(self, message_id: int, log_id: str, embed: discord.Embed,
                           files_data: list = None, is_warning: bool = False,
                           warning_for_log_id: str = None):
        record = {
            'log_id':              log_id,
            'message_id':          message_id,
            'embed': {
                'title':       embed.title,
                'description': embed.description,
                'color':       embed.color.value if embed.color else 0,
                'fields':      [{'name': f.name, 'value': f.value, 'inline': f.inline}
                                 for f in embed.fields],
                'footer':      embed.footer.text if embed.footer else None,
                'image_url':   embed.image.url if embed.image else None,
                'author_name': embed.author.name if embed.author else None,
                'author_icon': embed.author.icon_url if embed.author else None,
            },
            'files_data':          files_data or [],
            'is_warning':          is_warning,
            'warning_for_log_id':  warning_for_log_id,
            'timestamp':           datetime.now(timezone.utc).isoformat(),
        }
        self._bot_log_cache[message_id] = record
        self._bot_log_order.append(message_id)
        while len(self._bot_log_order) > self._bot_log_cache_size:
            self._bot_log_cache.pop(self._bot_log_order.popleft(), None)

    async def send_bot_log(
        self, guild: discord.Guild, embed: discord.Embed,
        files_data: list = None, log_id: str = None
    ) -> Optional[int]:
        bot_logs = self._get_bot_logs_channel(guild)
        if not bot_logs:
            return None
        if log_id is None:
            log_id = f"LOG-{int(datetime.now(timezone.utc).timestamp() * 1000)}"

        if files_data:
            verdict = await self.scanner.scan_files(
                files_data, guild=guild,
                context=f"bot-log send (log_id={log_id})")
            if verdict.blocked and not verdict.safe_files:
                embed.add_field(
                    name="⚠️ Attachment(s) Withheld",
                    value="One or more files were blocked by MediaScanner. "
                          "The server owner has been alerted.",
                    inline=False)
                files_data = None
            elif verdict.blocked:
                files_data = verdict.safe_files

        try:
            if files_data:
                discord_files = [
                    discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                    for f in files_data
                ]
                msg = await bot_logs.send(embed=embed, files=discord_files)
            else:
                msg = await bot_logs.send(embed=embed)
            self._register_bot_log(msg.id, log_id, embed, files_data=files_data)
            return msg.id
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send bot log: {e}")
            return None

    async def handle_bot_log_deletion(
        self, message_id: int, deleter: discord.Member, guild: discord.Guild
    ):
        record = self._bot_log_cache.get(message_id)
        if not record:
            return

        log_id             = record['log_id']
        original_embed_data = record['embed']
        timestamp           = datetime.now(timezone.utc)

        # Persist to deletion_attempts table for daily report
        self._exec(
            "INSERT INTO mod_deletion_attempts "
            "(log_id, deleter, deleter_id, timestamp, original_title, is_warning) "
            "VALUES (?,?,?,?,?,?)",
            (
                log_id, str(deleter), deleter.id,
                timestamp.isoformat(),
                original_embed_data.get('title') or '(no title)',
                int(record.get('is_warning', False)),
            ),
        )
        self.bot.logger.log(
            MODULE_NAME,
            f"⚠️ Bot-log deletion attempted by {deleter} (ID: {deleter.id}) "
            f"for log {log_id}", "WARNING")

        embed = discord.Embed(
            title=original_embed_data.get('title'),
            description=original_embed_data.get('description'),
            color=0xff0000, timestamp=timestamp)
        for field in original_embed_data.get('fields', []):
            embed.add_field(
                name=field['name'], value=field['value'], inline=field['inline'])
        if original_embed_data.get('author_name'):
            embed.set_author(
                name=original_embed_data['author_name'],
                icon_url=original_embed_data.get('author_icon') or None)
        if original_embed_data.get('image_url'):
            embed.set_image(url=original_embed_data['image_url'])
        embed.add_field(
            name="🚨 Deletion Attempted By",
            value=f"{deleter.mention} (`{deleter}` | `{deleter.id}`)", inline=False)
        original_footer = original_embed_data.get('footer') or ''
        embed.set_footer(
            text=f"{original_footer + ' • ' if original_footer else ''}"
                 f"Log ID: {log_id} • Deleting this will cause it to repost")

        new_msg_id = await self.send_bot_log(
            guild, embed, files_data=record.get('files_data'), log_id=log_id)
        if new_msg_id:
            self._deletion_warnings[new_msg_id] = log_id

    # ==================== OVERSIGHT: APPEALS ====================

    def _get_appeal(self, appeal_id: str) -> Optional[sqlite3.Row]:
        return self._one(
            "SELECT * FROM mod_appeals WHERE appeal_id=?", (appeal_id,))

    def _update_appeal_votes(self, appeal_id: str,
                              votes_for: list, votes_against: list):
        self._exec(
            "UPDATE mod_appeals SET votes_for=?, votes_against=? WHERE appeal_id=?",
            (json.dumps(votes_for), json.dumps(votes_against), appeal_id),
        )

    @staticmethod
    def _generate_appeal_id() -> str:
        """Generate a short 11-character YouTube-style ID."""
        import string, secrets
        chars = string.ascii_letters + string.digits + "-_"
        return ''.join(secrets.choice(chars) for _ in range(11))

    async def submit_appeal(self, user_id: int, guild_id: int, appeal_text: str) -> str:
        appeal_id = self._generate_appeal_id()
        deadline  = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        self._exec(
            "INSERT OR REPLACE INTO mod_appeals "
            "(appeal_id, user_id, guild_id, appeal_text, submitted_at, deadline, "
            " status, votes_for, votes_against, channel_message_id) "
            "VALUES (?,?,?,?,?,?,'pending','[]','[]',NULL)",
            (appeal_id, user_id, guild_id, appeal_text,
             datetime.now(timezone.utc).isoformat(), deadline),
        )
        self.bot.logger.log(MODULE_NAME, f"Appeal submitted: {appeal_id}")

        try:
            guild = self.bot.get_guild(guild_id)
            if guild:
                appeals_ch = discord.utils.get(guild.text_channels, name="mod-chat")
                if appeals_ch:
                    user = await self.bot.fetch_user(user_id)
                    embed = discord.Embed(
                        title="📝 Ban Appeal",
                        description=appeal_text,
                        color=0x9b59b6, timestamp=datetime.now(timezone.utc))
                    embed.add_field(name="User",
                                    value=f"{user} (`{user_id}`)", inline=True)
                    embed.add_field(
                        name="Deadline",
                        value=f"<t:{int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())}:R>",
                        inline=True)
                    embed.add_field(
                        name="Votes",
                        value="✅ Yes: **0**  ·  ❌ No: **0**",
                        inline=False)
                    embed.set_footer(
                        text=f"Appeal ID: {appeal_id} • "
                             f"Voting closes in 24 hours • Ties are denied")
                    view = AppealVoteView(self, appeal_id)
                    msg  = await appeals_ch.send(embed=embed, view=view)
                    self._exec(
                        "UPDATE mod_appeals SET channel_message_id=? WHERE appeal_id=?",
                        (msg.id, appeal_id),
                    )
                else:
                    self.bot.logger.log(
                        MODULE_NAME,
                        "submit_appeal: #mod-chat channel not found", "WARNING")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to post appeal", e)

        return appeal_id

    async def approve_appeal(self, appeal_id: str) -> bool:
        row = self._get_appeal(appeal_id)
        if not row:
            return False
        try:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                return False
            user        = await self.bot.fetch_user(row["user_id"])
            invite_link = await self._create_ban_reversal_invite(guild, row["user_id"])
            await guild.unban(user, reason="Appeal approved")
            try:
                embed = discord.Embed(
                    title="Ban Appeal Approved",
                    description=f"Your appeal for **{guild.name}** has been approved!",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                embed.add_field(
                    name="Rejoin Server",
                    value=f"You can rejoin using this invite:\n{invite_link}", inline=False)
                embed.set_footer(text="Welcome back!")
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            bot_logs = self._get_bot_logs_channel(guild)
            if bot_logs:
                log_embed = discord.Embed(
                    title="Ban Appeal Approved",
                    description=f"**{user}** has been unbanned after appeal approval.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                log_embed.add_field(name="Appeal Text",
                                    value=row["appeal_text"][:1024], inline=False)
                await self.send_bot_log(guild, log_embed)
            self._exec("DELETE FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to approve appeal {appeal_id}", e)
            return False

    async def deny_appeal(self, appeal_id: str) -> bool:
        row = self._get_appeal(appeal_id)
        if not row:
            return False
        try:
            guild = self.bot.get_guild(row["guild_id"])
            user  = await self.bot.fetch_user(row["user_id"])
            try:
                embed = discord.Embed(
                    title="Ban Appeal Denied",
                    description=f"Your appeal for **{guild.name}** has been reviewed and denied.",
                    color=0xe74c3c, timestamp=datetime.now(timezone.utc))
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            self._exec("DELETE FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to deny appeal {appeal_id}", e)
            return False

    # ==================== OVERSIGHT: DAILY REPORT ====================

    async def send_action_review(self, owner: discord.User, action_id: str, action: Dict):
        embed = discord.Embed(
            title=f"🔍 {action['action'].upper()} Action Review",
            color=(0xe74c3c if 'red_flag' in action['flags'] else
                   0xf39c12 if 'yellow_flag' in action['flags'] else 0x5865f2),
            timestamp=datetime.fromisoformat(action['timestamp']),
        )
        if action['flags']:
            flags_text = []
            if 'red_flag' in action['flags']:
                flags_text.append("🚩 **RED FLAG** - Both embeds deleted")
            elif 'yellow_flag' in action['flags']:
                flags_text.append("⚠️ **YELLOW FLAG** - Embed deleted")
            if 'inchat_deleted' in action['flags']:
                flags_text.append("❌ In-chat embed deleted")
            if 'botlog_deleted' in action['flags']:
                flags_text.append("❌ Bot-log embed deleted")
            embed.add_field(name="⚠️ Flags", value="\n".join(flags_text), inline=False)
        embed.add_field(
            name="Moderator",
            value=f"{action['moderator']} (ID: {action['moderator_id']})", inline=True)
        if action.get('user'):
            embed.add_field(
                name="User",
                value=f"{action['user']} (ID: {action['user_id']})", inline=True)
        embed.add_field(name="Reason", value=action['reason'], inline=False)
        if action.get('duration'):
            embed.add_field(name="Duration", value=action['duration'], inline=True)
        if action['context_messages']:
            embed.add_field(
                name="Context",
                value=f"{len(action['context_messages'])} messages logged", inline=True)
        view = ActionReviewView(self, action_id, action)
        await owner.send(embed=embed, view=view)
        if action['context_messages']:
            img = self.generate_context_screenshot(
                action['context_messages'], action.get('message_id'))
            await owner.send(file=discord.File(img, "context.png"))

    async def generate_daily_report(self):
        try:
            owner = await self.bot.fetch_user(self.cfg.owner_id)
            if not owner:
                return

            # Pull deletion attempts from DB then clear them
            attempt_rows = self._all(
                "SELECT * FROM mod_deletion_attempts ORDER BY id")
            self._exec("DELETE FROM mod_deletion_attempts")

            # Flagged pending actions
            red_flags    = []
            yellow_flags = []
            for row in self._all(
                "SELECT * FROM mod_pending_actions WHERE flags != '[]' AND flags != 'null'"
            ):
                action = self._row_to_action(row)
                if 'red_flag' in action['flags']:
                    red_flags.append((action['id'], action))
                elif 'yellow_flag' in action['flags']:
                    yellow_flags.append((action['id'], action))

            total_issues = len(attempt_rows) + len(red_flags) + len(yellow_flags)

            if total_issues == 0:
                embed = discord.Embed(
                    title="📊 Daily Integrity Report",
                    description="✅ No deletion attempts or mod-action flags in the last 24 hours.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                await owner.send(embed=embed)
                return

            embed = discord.Embed(
                title="📊 Daily Integrity Report",
                description=(
                    f"**{len(attempt_rows)}** bot-log deletion attempt(s)\n"
                    f"**{len(red_flags)}** 🚩 red-flag mod action(s)\n"
                    f"**{len(yellow_flags)}** ⚠️ yellow-flag mod action(s)"
                ),
                color=0xff4500, timestamp=datetime.now(timezone.utc))
            await owner.send(embed=embed)

            if attempt_rows:
                detail = discord.Embed(
                    title="🗑️ Bot-Log Deletion Attempts",
                    color=0xff0000, timestamp=datetime.now(timezone.utc))
                for attempt in attempt_rows[:20]:
                    detail.add_field(
                        name=f"Log `{attempt['log_id']}`",
                        value=(
                            f"**By:** {attempt['deleter']} (`{attempt['deleter_id']}`)\n"
                            f"**Original:** {attempt['original_title']}\n"
                            f"**At:** {attempt['timestamp'][:19].replace('T', ' ')} UTC"
                        ),
                        inline=False)
                if len(attempt_rows) > 20:
                    detail.set_footer(text=f"...and {len(attempt_rows) - 20} more.")
                await owner.send(embed=detail)

            for action_id, action in (red_flags + yellow_flags)[:10]:
                await self.send_action_review(owner, action_id, action)

            self.bot.logger.log(MODULE_NAME, "Daily integrity report sent to owner")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to generate daily report", e)

    # ==================== BACKGROUND TASKS ====================

    @tasks.loop(minutes=1)
    async def resolve_expired_appeals(self):
        try:
            now      = datetime.now(timezone.utc).isoformat()
            past_due = self._all(
                "SELECT * FROM mod_appeals WHERE status='pending' AND deadline <= ?",
                (now,))
            for row in past_due:
                appeal_id     = row["appeal_id"]
                votes_for     = len(json.loads(row["votes_for"]))
                votes_against = len(json.loads(row["votes_against"]))
                accepted      = votes_for > votes_against

                self.bot.logger.log(
                    MODULE_NAME,
                    f"Appeal {appeal_id} deadline reached — "
                    f"Accept: {votes_for}, Deny: {votes_against} → "
                    f"{'APPROVED' if accepted else 'DENIED'}")

                try:
                    guild = self.bot.get_guild(row["guild_id"])
                    if guild:
                        appeals_ch = discord.utils.get(
                            guild.text_channels, name="mod-chat")
                        if appeals_ch and row["channel_message_id"]:
                            try:
                                msg    = await appeals_ch.fetch_message(
                                    row["channel_message_id"])
                                result_color = 0x2ecc71 if accepted else 0xe74c3c
                                result_text  = (
                                    f"✅ Accepted ({votes_for}–{votes_against})"
                                    if accepted else
                                    f"❌ Denied ({votes_against}–{votes_for})")
                                embed = msg.embeds[0] if msg.embeds else discord.Embed()
                                embed.color = result_color
                                embed.add_field(
                                    name="Result", value=result_text, inline=False)
                                embed.set_footer(
                                    text=f"Appeal ID: {appeal_id} • Voting closed")
                                disabled_view = AppealVoteView(self, appeal_id)
                                for item in disabled_view.children:
                                    item.disabled = True
                                await msg.edit(embed=embed, view=disabled_view)
                            except Exception as e:
                                self.bot.logger.error(
                                    MODULE_NAME, "Failed to update appeal message", e)
                except Exception as e:
                    self.bot.logger.error(
                        MODULE_NAME, "Failed to resolve appeal channel message", e)

                if accepted:
                    await self.approve_appeal(appeal_id)
                else:
                    await self.deny_appeal(appeal_id)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in appeal resolution task", e)

    @resolve_expired_appeals.before_loop
    async def before_resolve_expired_appeals(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def check_expired_mutes(self):
        try:
            for mute in self.get_expired_mutes():
                guild  = self.bot.get_guild(mute['guild_id'])
                if not guild:
                    continue
                member = guild.get_member(mute['user_id'])
                if not member:
                    self.remove_mute(mute['guild_id'], mute['user_id'])
                    continue
                muted_role = discord.utils.get(
                    guild.roles, name=self.cfg.muted_role_name)
                if muted_role and muted_role in member.roles:
                    try:
                        await member.remove_roles(
                            muted_role, reason="Mute duration expired")
                        self.bot.logger.log(MODULE_NAME, f"Auto-unmuted {member}")
                        self.remove_mute(mute['guild_id'], mute['user_id'])
                    except Exception as e:
                        self.bot.logger.error(
                            MODULE_NAME, f"Failed to auto-unmute {member}", e)
                else:
                    # Role already gone (e.g. manually removed); clean up DB entry
                    self.remove_mute(mute['guild_id'], mute['user_id'])
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in mute expiry checker", e)

    @check_expired_mutes.before_loop
    async def before_check_expired_mutes(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def cleanup_invites(self):
        try:
            cleanup_days = self.cfg.invite_cleanup_days
            cutoff = (datetime.now(timezone.utc) - timedelta(days=cleanup_days)).isoformat()
            old_rows = self._all(
                "SELECT * FROM mod_invites WHERE created_at < ?", (cutoff,))
            for row in old_rows:
                try:
                    guild = self.bot.get_guild(row["guild_id"])
                    if guild:
                        invites = await guild.invites()
                        for inv in invites:
                            if inv.code == row["code"]:
                                await inv.delete(reason="Unused ban reversal invite cleanup")
                                break
                except Exception:
                    pass
            if old_rows:
                self._exec(
                    "DELETE FROM mod_invites WHERE created_at < ?", (cutoff,))
                self.bot.logger.log(
                    MODULE_NAME, f"Cleaned up {len(old_rows)} old ban reversal invites")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to cleanup old invites", e)

    @cleanup_invites.before_loop
    async def before_cleanup_invites(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def send_daily_report(self):
        try:
            await self.generate_daily_report()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send daily report", e)

    @send_daily_report.before_loop
    async def before_send_daily_report(self):
        await self.bot.wait_until_ready()
        # Sleep until the next midnight CST before the first run
        cst    = pytz.timezone('America/Chicago')
        now    = datetime.now(cst)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)


# ==================== UNIFIED COMMAND LOGIC ====================

async def _do_ban(ctx: ModContext, mod: ModerationSystem,
                  user: discord.User, reason: str = None, delete_days: int = 0,
                  fake: bool = False, rule_number: int = None):
    cfg = mod.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)

    rule_text = None
    if rule_number is not None:
        rules_mgr: Optional[RulesManager] = getattr(ctx.bot, "rules_manager", None)
        if rules_mgr:
            rule_text = rules_mgr.get_rule_text(rule_number)
        if not rule_text:
            return await ctx.error(
                f"❌ Rule **{rule_number}** not found.")
        reason = f"{rule_text} | {reason.strip()}" if reason and reason.strip() else rule_text

    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if user == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if user == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    member = ctx.guild.get_member(user.id)
    if member and member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)

    delete_days = max(0, min(7, delete_days))
    try:
        dm_reason_field = reason
        if rule_number is not None and rule_text:
            dm_reason_field = f"**Rule {rule_number} violation**\n{rule_text}"

        if not fake:
            try:
                dm = discord.Embed(
                    title="🔨 You have been banned",
                    description=f"You have been banned from **{ctx.guild.name}**",
                    color=0x992d22, timestamp=datetime.now(timezone.utc))
                dm.add_field(name="Reason",    value=dm_reason_field,    inline=False)
                dm.add_field(name="Moderator", value=str(ctx.author),    inline=True)
                dm.add_field(name="📝 Appeal Process",
                             value="If you believe this ban was unjustified, submit an "
                                   "appeal below. Staff will vote within 24 hours.",
                             inline=False)
                dm.set_footer(text="Appeals are reviewed by server staff")
                await user.send(embed=dm, view=BanAppealView(ctx.guild.id))
            except discord.Forbidden:
                pass
            await ctx.guild.ban(user, reason=f"{reason} - By {ctx.author}",
                                 delete_message_days=delete_days)

        embed = discord.Embed(
            title="✅ User Banned",
            description=f"{user.mention} has been banned.",
            color=0x992d22, timestamp=datetime.now(timezone.utc))
        if rule_number is not None:
            embed.add_field(name="Rule Violated", value=f"Rule {rule_number}", inline=True)
            embed.add_field(name="Rule Text",     value=rule_text,             inline=False)
            extra = reason[len(rule_text):].lstrip(" |").strip()
            if extra:
                embed.add_field(name="Additional Note", value=extra, inline=False)
        else:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator",         value=ctx.author.mention,    inline=True)
        embed.add_field(name="Messages Deleted",  value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        if not fake:
            el             = get_event_logger(ctx.bot)
            botlog_msg_id  = None
            if el:
                botlog_msg_id = await el.log_ban(
                    ctx.guild, user, ctx.author, reason, delete_days, ctx.channel)
            action_id = await mod.log_mod_action({
                'action': 'ban', 'moderator_id': ctx.author.id,
                'moderator': str(ctx.author), 'user_id': user.id, 'user': str(user),
                'reason': reason, 'guild_id': ctx.guild.id,
                'channel_id': ctx.channel.id,
                'message_id': ctx.message.id if ctx.message else None,
                'duration': None, 'additional': {'delete_days': delete_days},
            })
            if inchat_msg_id and action_id:
                mod.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id and action_id:
                mod.track_embed(botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME,
            f"{'[FAKE] ' if fake else ''}{ctx.author} banned {user}")
    except discord.Forbidden:
        await ctx.error("❌ I don't have permission to ban this user.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to ban the user.")
        ctx.bot.logger.error(MODULE_NAME, "Ban failed", e)


async def _do_unban(ctx: ModContext, mod: ModerationSystem,
                    user_id: str, reason: str = "No reason provided", fake: bool = False):
    if not ctx.author.guild_permissions.ban_members:
        return await ctx.error("❌ You don't have permission to unban members.")
    try:
        user = await ctx.bot.fetch_user(int(user_id))
        if not fake:
            await ctx.guild.unban(user, reason=f"{reason} - By {ctx.author}")
            mod.resolve_pending_action(user.id, 'ban')
        embed = discord.Embed(
            title="✅ User Unbanned",
            description=f"{user.mention} has been unbanned.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",    value=reason,              inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,  inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unbanned {user}")
    except ValueError:
        await ctx.error("❌ Invalid user ID.")
    except discord.NotFound:
        await ctx.error("❌ User not found or not banned.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unban.")
        ctx.bot.logger.error(MODULE_NAME, "Unban failed", e)


async def _do_kick(ctx: ModContext, mod: ModerationSystem,
                   member: discord.Member, reason: str, fake: bool = False):
    cfg = mod.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)
    try:
        try:
            dm = discord.Embed(
                title="👢 You have been kicked",
                description=f"You have been kicked from **{ctx.guild.name}**",
                color=0xe67e22, timestamp=datetime.now(timezone.utc))
            dm.add_field(name="Reason",    value=reason,          inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.set_footer(text="You can rejoin if you have an invite link")
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        if not fake:
            await member.kick(reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(
            title="✅ Member Kicked",
            description=f"{member.mention} has been kicked.",
            color=0xe67e22, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",    value=reason,             inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_kick(
                ctx.guild, member, ctx.author, reason, ctx.channel)
        action_id = await mod.log_mod_action({
            'action': 'kick', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author), 'user_id': member.id, 'user': str(member),
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
        })
        if inchat_msg_id and action_id:
            mod.track_embed(inchat_msg_id, action_id, 'inchat')
        if botlog_msg_id and action_id:
            mod.track_embed(botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} kicked {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to kick the member.")
        ctx.bot.logger.error(MODULE_NAME, "Kick failed", e)


async def _do_timeout(ctx: ModContext, mod: ModerationSystem,
                      member: discord.Member, duration: int,
                      reason: str, fake: bool = False):
    cfg = mod.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if not (1 <= duration <= 40320):
        return await ctx.error("❌ Duration must be between 1 and 40320 minutes.")
    try:
        if not fake:
            await member.timeout(
                datetime.now(timezone.utc) + timedelta(minutes=duration),
                reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(
            title="✅ Member Timed Out",
            description=f"{member.mention} timed out for **{duration}** minutes.",
            color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",    value=reason,              inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,  inline=True)
        embed.add_field(name="Duration",  value=f"{duration} minutes", inline=True)
        await ctx.reply(embed=embed)
        el = get_event_logger(ctx.bot)
        if el:
            await el.log_timeout(
                ctx.guild, member, ctx.author, reason, f"{duration} minutes", ctx.channel)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} timed out {member} for {duration}m")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to timeout the member.")
        ctx.bot.logger.error(MODULE_NAME, "Timeout failed", e)


async def _do_untimeout(ctx: ModContext, mod: ModerationSystem,
                         member: discord.Member, fake: bool = False):
    cfg = mod.cfg
    if not ctx.author.guild_permissions.moderate_members and \
            not has_elevated_role(ctx.author, cfg):
        return await ctx.error("❌ You don't have permission to moderate members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        if not fake:
            await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        embed = discord.Embed(
            title="✅ Timeout Removed",
            description=f"{member.mention}'s timeout has been removed.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} removed timeout from {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to remove the timeout.")
        ctx.bot.logger.error(MODULE_NAME, "Untimeout failed", e)


async def _do_mute(ctx: ModContext, mod: ModerationSystem,
                   member: discord.Member, reason: str = "No reason provided",
                   duration: Optional[str] = None, fake: bool = False):
    cfg = mod.cfg
    if not ctx.author.guild_permissions.manage_roles and \
            not has_elevated_role(ctx.author, cfg):
        return await ctx.error("❌ You don't have permission to mute members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)

    duration_seconds, duration_str = parse_duration(duration or "")
    try:
        muted_role = discord.utils.get(ctx.guild.roles, name=cfg.muted_role_name)
        if not muted_role:
            muted_role = await ctx.guild.create_role(
                name=cfg.muted_role_name, color=discord.Color.dark_gray(),
                reason="Creating Muted role for moderation")
            for ch in ctx.guild.channels:
                try:
                    await ch.set_permissions(
                        muted_role, send_messages=False, speak=False)
                except Exception:
                    pass
        if not fake:
            await member.add_roles(muted_role, reason=reason)
            mod.add_mute(ctx.guild.id, member.id, reason, ctx.author, duration_seconds)
        try:
            dm = discord.Embed(
                title="🔇 You Have Been Muted",
                description=f"You have been muted in **{ctx.guild.name}**.",
                color=0xf39c12, timestamp=datetime.now(timezone.utc))
            dm.add_field(name="Reason",    value=reason,          inline=False)
            dm.add_field(name="Duration",  value=duration_str,    inline=True)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        embed = discord.Embed(
            title="✅ Member Muted",
            description=f"{member.mention} has been muted.",
            color=0xf39c12, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",    value=reason,             inline=False)
        embed.add_field(name="Duration",  value=duration_str,       inline=True)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        el = get_event_logger(ctx.bot)
        if el:
            await el.log_mute(
                ctx.guild, member, ctx.author, reason, duration_str, ctx.channel)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} muted {member} for {duration_str}")
    except discord.Forbidden:
        await ctx.error("❌ I don't have permission to mute this member.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to mute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Mute failed", e)


async def _do_unmute(ctx: ModContext, mod: ModerationSystem,
                     member: discord.Member, fake: bool = False):
    cfg = mod.cfg
    if not ctx.author.guild_permissions.manage_roles and \
            not has_elevated_role(ctx.author, cfg):
        return await ctx.error("❌ You don't have permission to manage roles.")
    muted_role = discord.utils.get(ctx.guild.roles, name=cfg.muted_role_name)
    if not muted_role or muted_role not in member.roles:
        return await ctx.error("❌ This member is not muted.")
    try:
        if not fake:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
            mod.remove_mute(ctx.guild.id, member.id)
        embed = discord.Embed(
            title="✅ Member Unmuted",
            description=f"{member.mention} has been unmuted.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unmuted {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unmute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Unmute failed", e)


async def _do_softban(ctx: ModContext, mod: ModerationSystem,
                      member: discord.Member, reason: str,
                      delete_days: int = 7, fake: bool = False):
    cfg = mod.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)
    delete_days = max(0, min(7, delete_days))
    try:
        if not fake:
            await member.ban(reason=f"Softban: {reason} - By {ctx.author}",
                             delete_message_days=delete_days)
            await ctx.guild.unban(member, reason=f"Softban unban - By {ctx.author}")
        embed = discord.Embed(
            title="✅ Member Softbanned",
            description=f"{member.mention} softbanned (messages deleted, can rejoin).",
            color=0x992d22, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",            value=reason,              inline=False)
        embed.add_field(name="Moderator",         value=ctx.author.mention,  inline=True)
        embed.add_field(name="Messages Deleted",  value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_softban(
                ctx.guild, member, ctx.author, reason, delete_days, ctx.channel)
        action_id = await mod.log_mod_action({
            'action': 'softban', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author), 'user_id': member.id, 'user': str(member),
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'delete_days': delete_days},
        })
        if action_id:
            if inchat_msg_id:
                mod.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                mod.track_embed(botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} softbanned {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to softban the member.")
        ctx.bot.logger.error(MODULE_NAME, "Softban failed", e)


async def _do_warn(ctx: ModContext, mod: ModerationSystem,
                   member: discord.Member, reason: str, fake: bool = False):
    cfg = mod.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        strike_count = (mod.get_strikes(member.id) + 1
                        if fake else mod.add_strike(member.id, reason))
        try:
            dm = discord.Embed(
                title="⚠️ Warning",
                description=f"You have been warned in **{ctx.guild.name}**",
                color=0xf39c12, timestamp=datetime.now(timezone.utc))
            dm.add_field(name="Reason",          value=reason,               inline=False)
            dm.add_field(name="Moderator",        value=str(ctx.author),      inline=True)
            dm.add_field(name="Total Warnings",   value=str(strike_count),    inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        embed = discord.Embed(
            title="⚠️ Member Warned",
            description=f"{member.mention} has been warned.",
            color=0xf39c12, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",        value=reason,             inline=False)
        embed.add_field(name="Moderator",     value=ctx.author.mention, inline=True)
        embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
        await ctx.reply(embed=embed)
        el = get_event_logger(ctx.bot)
        if el:
            await el.log_warn(
                ctx.guild, member, ctx.author, reason, strike_count, ctx.channel)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} warned {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to warn the member.")
        ctx.bot.logger.error(MODULE_NAME, "Warn failed", e)


async def _do_warnings(ctx: ModContext, mod: ModerationSystem, member: discord.Member):
    if not ctx.author.guild_permissions.manage_messages and \
            not has_elevated_role(ctx.author, mod.cfg):
        return await ctx.error("❌ You don't have permission to view warnings.")
    strikes = mod.get_strike_details(member.id)
    if not strikes:
        return await ctx.reply(f"✅ **{member}** has no warnings.", ephemeral=True)
    embed = discord.Embed(
        title=f"⚠️ Warnings for {member}",
        description=f"Total warnings: **{len(strikes)}**",
        color=0xf39c12, timestamp=datetime.now(timezone.utc))
    for i, s in enumerate(strikes[-10:], 1):
        ts = datetime.fromisoformat(s['timestamp']).strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(
            name=f"Warning {i}",
            value=f"**Reason:** {s['reason']}\n**Date:** {ts}", inline=False)
    if len(strikes) > 10:
        embed.set_footer(text=f"Showing last 10 of {len(strikes)} warnings")
    await ctx.reply(embed=embed, ephemeral=True)


async def _do_clearwarnings(ctx: ModContext, mod: ModerationSystem, member: discord.Member):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.error("❌ You need Administrator permission to clear warnings.")
    if mod.clear_strikes(member.id):
        embed = discord.Embed(
            title="✅ Warnings Cleared",
            description=f"All warnings cleared for **{member}**.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} cleared warnings for {member}")
    else:
        await ctx.reply(f"**{member}** has no warnings to clear.", ephemeral=True)


async def _do_purge(ctx: ModContext, mod: ModerationSystem,
                    amount: int, target: Optional[discord.Member] = None,
                    fake: bool = False):
    if not has_elevated_role(ctx.author, mod.cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    if not (1 <= amount <= 100):
        return await ctx.error("❌ Amount must be between 1 and 100.")
    if not isinstance(ctx._source, discord.Interaction):
        try:
            await ctx._source.message.delete()
        except Exception:
            pass
    await ctx.defer()
    try:
        check   = (lambda m: m.author.id == target.id) if target else (lambda m: True)
        deleted = [] if fake else await ctx.channel.purge(limit=amount, check=check)
        if fake:
            deleted = [None] * amount

        reason = f"Purged {len(deleted)} message(s)" + (
            f" from {target}" if target else "")
        embed = discord.Embed(
            title="✅ Messages Purged",
            description=f"Deleted **{len(deleted)}** messages"
                        f"{f' from {target.mention}' if target else ''}.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Channel",   value=ctx.channel.mention, inline=True)
        inchat_msg_id = await ctx.followup(embed=embed)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_purge(
                ctx.guild, ctx.author, len(deleted), ctx.channel, target)
        action_id = await mod.log_mod_action({
            'action': 'purge', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': target.id if target else None,
            'user': str(target) if target else None,
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'amount': len(deleted)},
        })
        if action_id:
            if inchat_msg_id:
                mod.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                mod.track_embed(botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} purged {len(deleted)} messages")
    except Exception as e:
        await ctx.followup("❌ An error occurred while trying to purge messages.",
                           ephemeral=True)
        ctx.bot.logger.error(MODULE_NAME, "Purge failed", e)


async def _do_slowmode(ctx: ModContext, mod: ModerationSystem,
                       seconds: int, channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("❌ You don't have permission to manage channels.")
    if not (0 <= seconds <= 21600):
        return await ctx.error("❌ Slowmode must be between 0 and 21600 seconds.")
    target = channel or ctx.channel
    try:
        await target.edit(slowmode_delay=seconds,
                          reason=f"Slowmode set by {ctx.author}")
        if seconds == 0:
            embed = discord.Embed(title="✅ Slowmode Disabled",
                                  description=f"Slowmode disabled in {target.mention}.",
                                  color=0x2ecc71)
        else:
            embed = discord.Embed(title="✅ Slowmode Enabled",
                                  description=f"Slowmode set to **{seconds}s** in {target.mention}.",
                                  color=0x3498db)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(
            MODULE_NAME, f"{ctx.author} set slowmode to {seconds}s in {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to set slowmode.")
        ctx.bot.logger.error(MODULE_NAME, "Slowmode failed", e)


async def _do_lock(ctx: ModContext, mod: ModerationSystem,
                   reason: str, channel: Optional[discord.TextChannel] = None,
                   fake: bool = False):
    cfg = mod.cfg
    if not has_elevated_role(ctx.author, cfg):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason, cfg.min_reason_length)
    if not ok:
        return await ctx.error(err)
    target = channel or ctx.channel
    try:
        if not fake:
            await target.set_permissions(
                ctx.guild.default_role, send_messages=False,
                reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"{target.mention} has been locked.",
            color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",    value=reason,             inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_lock(ctx.guild, ctx.author, reason, target)
        action_id = await mod.log_mod_action({
            'action': 'lock', 'moderator_id': ctx.author.id,
            'moderator': str(ctx.author), 'user_id': None, 'user': None,
            'reason': reason, 'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'channel': target.id},
        })
        if action_id:
            if inchat_msg_id:
                mod.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                mod.track_embed(botlog_msg_id, action_id, 'botlog')
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} locked {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to lock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Lock failed", e)


async def _do_unlock(ctx: ModContext, mod: ModerationSystem,
                     channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("❌ You don't have permission to manage channels.")
    target = channel or ctx.channel
    try:
        await target.set_permissions(
            ctx.guild.default_role, send_messages=None,
            reason=f"Unlocked by {ctx.author}")
        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=f"{target.mention} has been unlocked.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unlocked {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unlock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Unlock failed", e)


# ==================== SETUP ====================

def setup(bot):
    mod_system = ModerationSystem(bot)
    bot._mod_system        = mod_system
    bot.moderation_manager = mod_system
    bot.mod_oversight      = mod_system
    bot.moderation         = mod_system

    # Convenience shorthand used throughout
    _mod = mod_system
    _cfg = mod_system.cfg

    # ---- SLASH COMMANDS ----

    @bot.tree.command(name="ban", description="Ban a user from the server")
    @app_commands.describe(
        user="User to ban",
        reason="Reason for the ban — not required if rule is provided",
        rule="Rule number violated",
        delete_days="Days of messages to delete (0-7)",
        fake="Simulate without executing",
    )
    @app_commands.default_permissions(ban_members=True)
    async def slash_ban(interaction: discord.Interaction, user: discord.User,
                        reason: Optional[str] = None, rule: Optional[int] = None,
                        delete_days: Optional[int] = 0, fake: bool = False):
        if rule is None and not reason:
            await interaction.response.send_message(
                "❌ You must provide either a reason or a rule number.", ephemeral=True)
            return
        await _do_ban(ModContext(interaction), _mod, user, reason,
                      delete_days, fake=fake, rule_number=rule)

    @bot.tree.command(name="unban", description="Unban a user from the server")
    @app_commands.describe(user_id="User ID to unban", reason="Reason for unban",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_unban(interaction: discord.Interaction, user_id: str,
                          reason: Optional[str] = "No reason provided", fake: bool = False):
        await _do_unban(ModContext(interaction), _mod, user_id, reason, fake=fake)

    @bot.tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(kick_members=True)
    async def slash_kick(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_kick(ModContext(interaction), _mod, member, reason, fake=fake)

    @bot.tree.command(name="timeout", description="Timeout a member")
    @app_commands.describe(member="Member to timeout", duration="Duration in minutes",
                           reason="Reason (min 10 chars)", fake="Simulate without executing")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_timeout(interaction: discord.Interaction, member: discord.Member,
                            duration: int, reason: str, fake: bool = False):
        await _do_timeout(ModContext(interaction), _mod, member, duration, reason, fake=fake)

    @bot.tree.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from",
                           fake="Simulate without executing")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_untimeout(interaction: discord.Interaction, member: discord.Member,
                               fake: bool = False):
        await _do_untimeout(ModContext(interaction), _mod, member, fake=fake)

    @bot.tree.command(name="mute", description="Mute a member")
    @app_commands.describe(member="Member to mute", reason="Reason for mute",
                           duration="Duration e.g. 10m, 1h, 1d (empty = permanent)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_mute(interaction: discord.Interaction, member: discord.Member,
                         reason: str = "No reason provided",
                         duration: Optional[str] = None, fake: bool = False):
        await _do_mute(ModContext(interaction), _mod, member, reason, duration, fake=fake)

    @bot.tree.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Member to unmute", fake="Simulate without executing")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_unmute(interaction: discord.Interaction, member: discord.Member,
                            fake: bool = False):
        await _do_unmute(ModContext(interaction), _mod, member, fake=fake)

    @bot.tree.command(name="softban",
                      description="Softban a member (ban+unban to delete messages)")
    @app_commands.describe(member="Member to softban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 7)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_softban(interaction: discord.Interaction, member: discord.Member,
                            reason: str, delete_days: Optional[int] = 7,
                            fake: bool = False):
        await _do_softban(ModContext(interaction), _mod, member, reason,
                          delete_days, fake=fake)

    @bot.tree.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warn(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_warn(ModContext(interaction), _mod, member, reason, fake=fake)

    @bot.tree.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
        await _do_warnings(ModContext(interaction), _mod, member)

    @bot.tree.command(name="clearwarnings", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.default_permissions(administrator=True)
    async def slash_clearwarnings(interaction: discord.Interaction, member: discord.Member):
        await _do_clearwarnings(ModContext(interaction), _mod, member)

    @bot.tree.command(name="purge", description="Delete multiple messages")
    @app_commands.describe(amount="Number of messages to delete (1-100)",
                           user="Only delete messages from this user (optional)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_purge(interaction: discord.Interaction, amount: int,
                          user: Optional[discord.Member] = None, fake: bool = False):
        await _do_purge(ModContext(interaction), _mod, amount, user, fake=fake)

    @bot.tree.command(name="slowmode", description="Set channel slowmode")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)",
                           channel="Channel to apply to (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_slowmode(interaction: discord.Interaction, seconds: int,
                             channel: Optional[discord.TextChannel] = None):
        await _do_slowmode(ModContext(interaction), _mod, seconds, channel)

    @bot.tree.command(name="lock", description="Lock a channel")
    @app_commands.describe(reason="Reason for locking (min 10 chars)",
                           channel="Channel to lock (default: current)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_lock(interaction: discord.Interaction, reason: str,
                         channel: Optional[discord.TextChannel] = None, fake: bool = False):
        await _do_lock(ModContext(interaction), _mod, reason, channel, fake=fake)

    @bot.tree.command(name="unlock", description="Unlock a channel")
    @app_commands.describe(channel="Channel to unlock (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_unlock(interaction: discord.Interaction,
                           channel: Optional[discord.TextChannel] = None):
        await _do_unlock(ModContext(interaction), _mod, channel)

    # ---- PREFIX COMMANDS ----

    @bot.command(name="ban")
    async def prefix_ban(ctx, user: discord.User = None, *, args: str = ""):
        if not user:
            return await ctx.send(
                "❌ Usage: `?ban @user <reason> [rule:<n>] [days:<0-7>] [fake]`",
                delete_after=10)
        working    = args
        fake       = bool(re.search(r'(?:^|\s)fake(?:\s|$)', working, re.IGNORECASE))
        working    = re.sub(r'(?:^|\s)fake(?=\s|$)', ' ', working, flags=re.IGNORECASE)
        rule_match = re.search(r'(?:^|\s)rule:(\d+)(?=\s|$)', working, re.IGNORECASE)
        rule_number = int(rule_match.group(1)) if rule_match else None
        if rule_match:
            working = re.sub(r'(?:^|\s)rule:\d+(?=\s|$)', ' ', working, flags=re.IGNORECASE)
        days_match  = re.search(r'(?:^|\s)days:(\d+)(?=\s|$)', working, re.IGNORECASE)
        delete_days = int(days_match.group(1)) if days_match else 0
        if days_match:
            working = re.sub(r'(?:^|\s)days:\d+(?=\s|$)', ' ', working, flags=re.IGNORECASE)
        reason = working.strip() or None
        if rule_number is None and not reason:
            return await ctx.send(
                "❌ You must provide either a reason or `rule:<n>`.", delete_after=8)
        await _do_ban(ModContext(ctx), _mod, user, reason, delete_days,
                      fake=fake, rule_number=rule_number)

    @bot.command(name="unban")
    async def prefix_unban(ctx, user_id: str = None, *, reason: str = "No reason provided"):
        if not user_id:
            return await ctx.send("❌ Usage: `?unban <user_id> [reason]`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake" if parts else False
        if fake:
            reason = " ".join(parts[:-1]) or "No reason provided"
        await _do_unban(ModContext(ctx), _mod, user_id, reason, fake=fake)

    @bot.command(name="kick")
    async def prefix_kick(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?kick @member <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake" if parts else False
        if fake:
            reason = " ".join(parts[:-1])
        await _do_kick(ModContext(ctx), _mod, member, reason, fake=fake)

    @bot.command(name="timeout")
    async def prefix_timeout(ctx, member: discord.Member = None,
                              duration: int = None, *, reason: str = ""):
        if not member or duration is None:
            return await ctx.send(
                "❌ Usage: `?timeout @member <minutes> <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake" if parts else False
        if fake:
            reason = " ".join(parts[:-1])
        await _do_timeout(ModContext(ctx), _mod, member, duration, reason, fake=fake)

    @bot.command(name="untimeout")
    async def prefix_untimeout(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?untimeout @member`", delete_after=8)
        await _do_untimeout(ModContext(ctx), _mod, member, fake=fake.lower() == "fake")

    @bot.command(name="mute")
    async def prefix_mute(ctx, member: discord.Member = None, *, args: str = ""):
        if not member:
            return await ctx.send(
                "❌ Usage: `?mute @member [duration] [reason]`", delete_after=8)
        duration = None
        reason   = args or "No reason provided"
        parts    = args.split(None, 1)
        if parts and re.match(r'^\d+[smhd]$', parts[0].lower()):
            duration = parts[0]
            reason   = parts[1] if len(parts) > 1 else "No reason provided"
        r_parts = reason.split()
        fake    = r_parts[-1].lower() == "fake" if r_parts else False
        if fake:
            reason = " ".join(r_parts[:-1]) or "No reason provided"
        await _do_mute(ModContext(ctx), _mod, member, reason, duration, fake=fake)

    @bot.command(name="unmute")
    async def prefix_unmute(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?unmute @member`", delete_after=8)
        await _do_unmute(ModContext(ctx), _mod, member, fake=fake.lower() == "fake")

    @bot.command(name="softban")
    async def prefix_softban(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?softban @member <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake" if parts else False
        if fake:
            reason = " ".join(parts[:-1])
        await _do_softban(ModContext(ctx), _mod, member, reason, fake=fake)

    @bot.command(name="warn")
    async def prefix_warn(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?warn @member <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake" if parts else False
        if fake:
            reason = " ".join(parts[:-1])
        await _do_warn(ModContext(ctx), _mod, member, reason, fake=fake)

    @bot.command(name="warnings")
    async def prefix_warnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("❌ Usage: `?warnings @member`", delete_after=8)
        await _do_warnings(ModContext(ctx), _mod, member)

    @bot.command(name="clearwarnings")
    async def prefix_clearwarnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("❌ Usage: `?clearwarnings @member`", delete_after=8)
        await _do_clearwarnings(ModContext(ctx), _mod, member)

    @bot.command(name="purge")
    async def prefix_purge(ctx, amount: int = None,
                            member: discord.Member = None, fake: str = ""):
        if amount is None:
            return await ctx.send("❌ Usage: `?purge <amount> [@member]`", delete_after=8)
        await _do_purge(ModContext(ctx), _mod, amount, member,
                        fake=fake.lower() == "fake")

    @bot.command(name="slowmode")
    async def prefix_slowmode(ctx, seconds: int = None,
                               channel: discord.TextChannel = None):
        if seconds is None:
            return await ctx.send(
                "❌ Usage: `?slowmode <seconds> [#channel]`", delete_after=8)
        await _do_slowmode(ModContext(ctx), _mod, seconds, channel)

    @bot.command(name="lock")
    async def prefix_lock(ctx, channel: Optional[discord.TextChannel] = None, *, reason: str = ""):
        parts = reason.split()
        fake  = parts[-1].lower() == "fake" if parts else False
        if fake:
            reason = " ".join(parts[:-1])
        await _do_lock(ModContext(ctx), _mod, reason, channel, fake=fake)

    @bot.command(name="unlock")
    async def prefix_unlock(ctx, channel: discord.TextChannel = None):
        await _do_unlock(ModContext(ctx), _mod, channel)

    # ---- OVERSIGHT COMMANDS ----

    @bot.tree.command(name="report",
                      description="[Owner only] Trigger the moderation report immediately")
    async def report_command(interaction: discord.Interaction):
        if interaction.user.id != _cfg.owner_id:
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner.", ephemeral=True)
            return
        await interaction.response.send_message("📊 Generating report...", ephemeral=True)
        try:
            await _mod.generate_daily_report()
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Manual report generation failed", e)

    # ---- EVENT LISTENERS ----

    @bot.listen()
    async def on_message(message):
        if message.author.bot or not message.guild:
            return

        await _mod.cache_message(message)

        content_lower = message.content.lower()

        for word in _cfg.child_safety:
            if matches_banned_term(word, content_lower):
                try:
                    await message.delete()
                    await message.guild.ban(
                        message.author,
                        reason=f"Auto-ban: Child safety violation - '{word}'")
                    bot.logger.log(
                        MODULE_NAME, f"AUTO-BAN: {message.author} child safety", "WARNING")
                    el = get_event_logger(bot)
                    if el:
                        await el.log_autoban(
                            message.guild, message.author,
                            "Child safety violation", message.channel)
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-ban failed", e)
                return

        for word in _cfg.racial_slurs:
            if matches_banned_term(word, content_lower):
                try:
                    await message.delete()
                    count = _mod.add_strike(
                        message.author.id, f"Racial slur: '{word}'")
                    if count >= 2:
                        await message.guild.ban(
                            message.author,
                            reason="Auto-ban: Repeated racial slurs (2 strikes)")
                        bot.logger.log(
                            MODULE_NAME,
                            f"AUTO-BAN: {message.author} repeated slurs", "WARNING")
                        el = get_event_logger(bot)
                        if el:
                            await el.log_autoban_strike(
                                message.guild, message.author, count,
                                "Repeated racial slurs", message.channel)
                    else:
                        try:
                            dm = discord.Embed(
                                title="⚠️ Warning - Strike 1/2",
                                description="Your message was deleted for inappropriate language.",
                                color=0xf39c12)
                            dm.add_field(
                                name="Action",
                                value="Second strike = automatic ban.", inline=False)
                            await message.author.send(embed=dm)
                        except Exception:
                            pass
                        bot.logger.log(
                            MODULE_NAME, f"STRIKE 1: {message.author} slur", "WARNING")
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-mod slur handling failed", e)
                return

        for word in _cfg.banned_words:
            if matches_banned_term(word, content_lower):
                try:
                    await message.delete()
                    bot.logger.log(
                        MODULE_NAME, f"Deleted banned word from {message.author}")
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Message deletion failed", e)
                return

    @bot.listen()
    async def on_message_delete(message):
        await _mod.handle_embed_deletion(message.id)

        if not message.guild:
            return

        bot_logs_channel = _mod._get_bot_logs_channel(message.guild)
        if bot_logs_channel and message.channel.id == bot_logs_channel.id:
            if message.id in _mod._bot_log_cache:
                deleter = None
                try:
                    await asyncio.sleep(0.75)
                    async for entry in message.guild.audit_logs(
                        limit=10, action=discord.AuditLogAction.message_delete
                    ):
                        age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                        if age < 15 and entry.target.id == message.author.id:
                            deleter = entry.user
                            break
                except Exception as e:
                    _mod.bot.logger.log(
                        MODULE_NAME, f"Audit log fetch failed: {e}", "WARNING")

                if deleter and deleter.id == message.guild.me.id:
                    return

                if deleter is None or has_elevated_role(deleter, _cfg):
                    await _mod.handle_bot_log_deletion(
                        message.id, deleter or message.guild.me, message.guild)
                elif message.id in _mod._deletion_warnings:
                    original_log_id = _mod._deletion_warnings.pop(message.id)
                    warning_embed = discord.Embed(
                        title="🚨 Bot-Log Deletion Warning — REPOSTED",
                        description=(
                            f"A deletion warning for log `{original_log_id}` was itself deleted.\n"
                            "This report will continue to reappear every time it is deleted."
                        ),
                        color=0xff0000, timestamp=discord.utils.utcnow())
                    warning_embed.add_field(
                        name="Original Log ID", value=original_log_id, inline=True)
                    warning_embed.set_footer(
                        text="Deleting this message will cause it to repost again.")
                    try:
                        new_warn_msg = await bot_logs_channel.send(embed=warning_embed)
                        _mod._register_bot_log(
                            new_warn_msg.id, f"WARN-{original_log_id}",
                            warning_embed, is_warning=True,
                            warning_for_log_id=original_log_id)
                        _mod._deletion_warnings[new_warn_msg.id] = original_log_id
                    except Exception as e:
                        _mod.bot.logger.error(
                            MODULE_NAME, f"Failed to repost deletion warning: {e}")
            return

        if not message.author.bot and message.id in _mod.media_cache:
            guild_id   = str(message.guild.id)
            channel_id = str(message.channel.id)
            cached     = _mod.media_cache.get(message.id)
            rehosted   = []
            if cached:
                for f in cached['files']:
                    try:
                        data = _mod._decrypt_from_disk(f['path'])
                        rehosted.append({'filename': f['filename'], 'data': data})
                    except FileNotFoundError:
                        _mod.bot.logger.log(
                            MODULE_NAME,
                            f"Skipping stale cache entry — encrypted file already gone: "
                            f"{f['path'].name}", "WARNING")
                    except Exception as e:
                        _mod.bot.logger.log(
                            MODULE_NAME,
                            f"Failed to decrypt {f['filename']} for deletion log: {e}",
                            "WARNING")
            if not hasattr(bot, '_pending_rehosted_media'):
                bot._pending_rehosted_media = {}
            bot._pending_rehosted_media[message.id] = rehosted
            _mod._delete_media_files(message.id)
            channel_msgs = _mod.message_cache.get(guild_id, {}).get(channel_id, [])
            _mod.message_cache[guild_id][channel_id] = [
                m for m in channel_msgs if m['id'] != message.id
            ]

    @bot.listen()
    async def on_message_edit(before, after):
        if not after.guild or after.author.bot:
            return

        before_att_ids = {att.id for att in before.attachments}
        after_att_ids  = {att.id for att in after.attachments}
        removed_ids    = before_att_ids - after_att_ids
        if not removed_ids:
            return

        guild_id   = str(after.guild.id)
        channel_id = str(after.channel.id)

        channel_msgs = _mod.message_cache.get(guild_id, {}).get(channel_id, [])
        for msg in channel_msgs:
            if msg['id'] == after.id:
                msg['attachments'] = [att.url for att in after.attachments]
                break

        cached        = _mod.media_cache.get(after.id)
        removed_files = []
        if cached:
            removed_filenames = {
                att.filename for att in before.attachments if att.id in removed_ids}
            kept = []
            for f in cached['files']:
                if f['filename'] in removed_filenames:
                    try:
                        data = _mod._decrypt_from_disk(f['path'])
                        removed_files.append({'filename': f['filename'], 'data': data})
                    except Exception as e:
                        _mod.bot.logger.log(
                            MODULE_NAME,
                            f"Failed to decrypt removed attachment {f['filename']}: {e}",
                            "WARNING")
                    finally:
                        f['path'].unlink(missing_ok=True)
                else:
                    kept.append(f)
            if kept:
                _mod.media_cache[after.id]['files'] = kept
            else:
                _mod.media_cache.pop(after.id, None)

        if not removed_files:
            bot_logs = _mod._get_bot_logs_channel(after.guild)
            if bot_logs:
                embed = discord.Embed(
                    title="✂️ Attachment Removed from Message",
                    color=discord.Color.yellow(),
                    timestamp=datetime.now(timezone.utc))
                embed.set_author(
                    name=str(after.author),
                    icon_url=after.author.display_avatar.url)
                description = (f"**{after.author.mention} removed an attachment "
                               f"in {after.channel.mention}**")
                if after.content:
                    description += f"\n{after.content}"
                embed.description = description
                removed_names = ", ".join(
                    att.filename for att in before.attachments if att.id in removed_ids)
                embed.add_field(
                    name="Removed File(s)", value=removed_names or "unknown", inline=False)
                embed.add_field(
                    name="Note",
                    value="File was not in local cache; original URLs may be expired.",
                    inline=False)
                embed.set_footer(
                    text=f"Author: {after.author.id} | Message ID: {after.id}")
                await _mod.send_bot_log(after.guild, embed)
            return

        image_exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        audio_exts = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus',
                      '.mp4', '.mov', '.webm')

        image_files = [f for f in removed_files
                       if f['filename'].lower().endswith(image_exts)]
        other_files  = [f for f in removed_files
                        if not f['filename'].lower().endswith(image_exts)]

        embed = discord.Embed(color=discord.Color.yellow(), timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(after.author),
                         icon_url=after.author.display_avatar.url)
        description = (f"**{after.author.mention} removed an attachment "
                       f"in {after.channel.mention}**")
        if after.content:
            description += f"\n{after.content}"
        embed.description = description
        embed.set_footer(text=f"Author: {after.author.id} | Message ID: {after.id}")

        bot_logs = _mod._get_bot_logs_channel(after.guild)

        if image_files and not other_files:
            embed.set_image(url=f"attachment://{image_files[0]['filename']}")
            await _mod.send_bot_log(after.guild, embed, files_data=removed_files)
        elif other_files:
            has_audio = any(f['filename'].lower().endswith(audio_exts) for f in other_files)
            label     = "audio" if has_audio else "file"
            embed.add_field(name="Attachment",
                            value=f"*{label} hosted above*", inline=False)
            if bot_logs:
                discord_files = [
                    discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                    for f in removed_files
                ]
                await bot_logs.send(files=discord_files)
            await _mod.send_bot_log(after.guild, embed)
        else:
            await _mod.send_bot_log(after.guild, embed, files_data=removed_files)

    @bot.listen()
    async def on_member_remove(member):
        _mod.save_member_roles(member)

    @bot.listen()
    async def on_member_join(member):
        await _mod.restore_member_roles(member)

    # ---- RULES MANAGER ----

    rules_manager    = RulesManager(bot, _mod._db, _cfg)
    bot.rules_manager = rules_manager

    @bot.listen("on_ready")
    async def _rules_on_ready():
        for guild in bot.guilds:
            await rules_manager.on_ready(guild)
            rules_manager.start_watcher(guild)
        bot.logger.log(MODULE_NAME, "Rules manager synced on ready")

    @bot.tree.command(name="rules", description="List all server rules")
    async def slash_rules(interaction: discord.Interaction):
        data = rules_manager.load_rules()
        if not data:
            await interaction.response.send_message(
                "❌ Rules file not found or could not be loaded.", ephemeral=True)
            return
        embed = rules_manager.build_embed(data)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="updaterules",
                      description="Force-refresh the #rules channel embed from rules.json")
    @app_commands.default_permissions(administrator=True)
    async def slash_updaterules(interaction: discord.Interaction):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        posted = await rules_manager.sync(interaction.guild, force=True)
        if posted:
            await interaction.followup.send(
                "✅ Rules embed has been refreshed in the rules channel.", ephemeral=True)
        else:
            await interaction.followup.send(
                "ℹ️ Rules embed is already up to date.", ephemeral=True)

    bot.logger.log(MODULE_NAME, "Moderation setup complete")