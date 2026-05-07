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
from PIL import Image, ImageDraw, ImageFont
import pytz
import tempfile
from collections import deque
from cryptography.fernet import Fernet

MODULE_NAME = "MODERATION"

# These are UI copy — they stay in code. Everything else lives in the DB.

ERROR_NO_PERMISSION      = "You need a moderation role (Moderator, Admin, or Owner) to use this command."
ERROR_REASON_REQUIRED    = "You must provide a reason for this action."
ERROR_CANNOT_ACTION_SELF = "You cannot perform this action on yourself."
ERROR_CANNOT_ACTION_BOT  = "I cannot perform this action on myself."
ERROR_HIGHER_ROLE        = "You cannot perform this action on someone with a higher or equal role."

def _script_dir() -> Path:
    """Root Embot/ directory (two levels up from modules/)."""
    return Path(__file__).parent.parent.absolute()

def _db_path() -> str:
    p = _script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "moderation.db")

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

-- ── Suspicion / fed-fingerprint system ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mod_suspicion (
    guild_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    score           INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,   -- 1 = manually confirmed flagged
    cleared         INTEGER NOT NULL DEFAULT 0,   -- 1 = manually cleared by mod
    join_invite     TEXT,                          -- invite code used to join
    invite_source   TEXT,                          -- 'leaktracker'|'youtube'|'custom'|'unknown'
    scored_at       TEXT NOT NULL,
    flagged_at      TEXT,
    cleared_at      TEXT,
    cleared_by      TEXT,
    note            TEXT,
    signals         TEXT NOT NULL DEFAULT '[]',   -- JSON array of triggered signal keys
    PRIMARY KEY (guild_id, user_id)
);

"""

# Applied once on first init; never overwrites existing rows.

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
    from _utils import atomic_json_write
    atomic_json_write(_config_path(), data)

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

        if "mod_config"in tables:
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

        if "mod_elevated_roles"in tables:
            rows = conn.execute("SELECT role_name FROM mod_elevated_roles").fetchall()
            if rows:
                cfg["elevated_roles"] = [r["role_name"] for r in rows]

        if has_rules_json:
            try:
                with open(rules_file, "r", encoding="utf-8") as f:
                    rules_data = json.load(f)
                cfg["rules"] = rules_data
            except Exception as e:
                import sys
                print(f"[MODERATION] Migration: failed to read rules.json: {e}", file=sys.stderr)

        _save_config(cfg)

        for tbl in ("mod_word_lists", "mod_elevated_roles", "mod_config"):
            if tbl in tables:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.commit()

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

def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c

_conn_cache: dict = {}

def _get_cached_conn(db_path: str) -> sqlite3.Connection:
    if db_path not in _conn_cache:
        _conn_cache[db_path] = _conn(db_path)
    return _conn_cache[db_path]

def _db_exec(db, query: str, params: tuple = ()):
    if isinstance(db, sqlite3.Connection):
        db.execute(query, params)
        db.commit()
        return
    c = _get_cached_conn(db)
    c.execute(query, params)
    c.commit()

def _db_one(db, query: str, params: tuple = ()):
    if isinstance(db, sqlite3.Connection):
        return db.execute(query, params).fetchone()
    c = _get_cached_conn(db)
    return c.execute(query, params).fetchone()

def _db_all(db, query: str, params: tuple = ()):
    if isinstance(db, sqlite3.Connection):
        return db.execute(query, params).fetchall()
    c = _get_cached_conn(db)
    return c.execute(query, params).fetchall()

class ModConfig:
    """
    Reads user-configurable values from config/moderation.json.
    Mutations (add/remove role) write back to the JSON file atomically.
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

def has_elevated_role(member: discord.Member, cfg: ModConfig) -> bool:
    if member.guild.owner_id == member.id:
        return True
    elevated = cfg.get_elevated_roles()
    return any(role.name in elevated for role in member.roles)

def validate_reason(reason: Optional[str], min_len: int) -> tuple:
    if not reason or reason.strip() == ""or reason == "No reason provided":
        return False, ERROR_REASON_REQUIRED
    if len(reason) < min_len:
        return False, f"Reason must be at least {min_len} characters long."
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

class BanAppealView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @ui.button(label="Submit Appeal", style=discord.ButtonStyle.primary, emoji="📝",
               custom_id="ban_appeal_submit")
    async def appeal_button(self, interaction: discord.Interaction, button: ui.Button):
        if not hasattr(interaction.client, 'moderation') or \
                not hasattr(interaction.client.moderation, 'submit_appeal'):
            await interaction.response.send_message(
                "Appeal system not available.", ephemeral=True)
            return
        guild_id = interaction.guild_id or self.guild_id
        modal = BanAppealModal(interaction.client.moderation, guild_id)
        await interaction.response.send_modal(modal)

class ActionReviewView(ui.View):
    def __init__(self, moderation_system, action_id: str, action: Dict):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.action_id  = action_id
        self.action     = action
        # Stable custom_ids embed the action_id so they survive restarts
        self.approve_btn.custom_id  = f"action_approve:{action_id}"
        self.revert_btn.custom_id   = f"action_revert:{action_id}"
        self.view_chat_btn.custom_id = f"action_viewchat:{action_id}"

    @ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction, button: ui.Button):
        action_id = button.custom_id.split(":", 1)[1]
        success = await self.moderation.approve_action(action_id)
        if success:
            await interaction.response.send_message(
                "Action approved and removed from pending.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "Failed to approve action.", ephemeral=True)

    @ui.button(label="Revert", style=discord.ButtonStyle.red, emoji="↩")
    async def revert_btn(self, interaction: discord.Interaction, button: ui.Button):
        action_id  = button.custom_id.split(":", 1)[1]
        action     = self.moderation._get_pending_action(action_id)
        if not action:
            await interaction.response.send_message("Action not found.", ephemeral=True)
            return
        guild = self.moderation.bot.get_guild(action['guild_id'])
        if not guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        success = await self.moderation.revert_action(action_id, guild)
        if success:
            await interaction.response.send_message(
                "↩ Action reverted successfully.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "Failed to revert action.", ephemeral=True)

    @ui.button(label="View Chat", style=discord.ButtonStyle.gray, emoji="💬")
    async def view_chat_btn(self, interaction: discord.Interaction, button: ui.Button):
        action_id = button.custom_id.split(":", 1)[1]
        action    = self.moderation._get_pending_action(action_id)
        if not action or not action.get('channel_id') or not action.get('message_id'):
            await interaction.response.send_message(
                "No chat link available.", ephemeral=True)
            return
        guild_id   = action['guild_id']
        channel_id = action['channel_id']
        message_id = action['message_id']
        jump_link  = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        await interaction.response.send_message(
            f"[Jump to message]({jump_link})", ephemeral=True)

class AppealVoteView(ui.View):
    def __init__(self, moderation_system, appeal_id: str):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.appeal_id  = appeal_id
        # Embed appeal_id in each custom_id so Discord can route interactions
        # to the correct view instance after a bot restart.
        yes_btn = ui.Button(label="Vote Yes", style=discord.ButtonStyle.green,
                            emoji="✅", custom_id=f"appeal_accept:{appeal_id}")
        no_btn  = ui.Button(label="Vote No",  style=discord.ButtonStyle.red,
                            emoji="❌", custom_id=f"appeal_deny:{appeal_id}")
        yes_btn.callback = self._accept_callback
        no_btn.callback  = self._deny_callback
        self.add_item(yes_btn)
        self.add_item(no_btn)

    def _updated_embed(self, message: discord.Message,
                        votes_for: list, votes_against: list) -> discord.Embed:
        """Return a copy of the message embed with the vote counts updated."""
        old = message.embeds[0] if message.embeds else None
        embed = discord.Embed(
            title=old.title if old else "Ban Appeal",
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
            value=f"Yes: **{len(votes_for)}**  ·   No: **{len(votes_against)}**",
            inline=False,
        )
        if old and old.footer:
            embed.set_footer(text=old.footer.text)
        return embed

    async def _accept_callback(self, interaction: discord.Interaction):
        cfg = self.moderation.cfg
        if not has_elevated_role(interaction.user, cfg):
            await interaction.response.send_message(
                "Only moderators can vote on appeals.", ephemeral=True)
            return
        appeal = self.moderation._get_appeal(self.appeal_id)
        if not appeal:
            await interaction.response.send_message(
                "Appeal no longer exists.", ephemeral=True)
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

    async def _deny_callback(self, interaction: discord.Interaction):
        cfg = self.moderation.cfg
        if not has_elevated_role(interaction.user, cfg):
            await interaction.response.send_message(
                "Only moderators can vote on appeals.", ephemeral=True)
            return
        appeal = self.moderation._get_appeal(self.appeal_id)
        if not appeal:
            await interaction.response.send_message(
                "Appeal no longer exists.", ephemeral=True)
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
            title="Appeal Submitted",
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

class RulesManager:
    """
    Manages the server rules embed in the #rules channel.
    Rules content lives in config/moderation.json under the "rules"key.
    Only the *state* (posted message ID + hash) is persisted in the DB.
    """

    def __init__(self, bot, db_path: str, cfg: ModConfig):
        self.bot      = bot
        self._db      = db_path
        self.cfg      = cfg

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
        return [f"Rule {r['number']} — {r['title']}"for r in data.get("rules", [])]

    def build_embed(self, data: dict) -> discord.Embed:
        color = data.get("color", 0x3498db)
        embed = discord.Embed(
            title=f" {data.get('title', 'Server Rules')}",
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

        # Persistent Fernet key — derived from FERNET_KEY env var.
        # If the env var is absent a new random key is generated and a loud
        # warning is emitted, because previously encrypted .enc files will
        # be unreadable after a restart.
        import base64 as _b64, hashlib as _hl
        _fernet_secret = os.environ.get("FERNET_KEY")
        if _fernet_secret:
            # Derive a 32-byte URL-safe base64 key from the secret
            _derived = _b64.urlsafe_b64encode(
                _hl.sha256(_fernet_secret.encode()).digest()
            )
            self._fernet = Fernet(_derived)
        else:
            self._fernet = Fernet(Fernet.generate_key())
            bot.logger.log(
                MODULE_NAME,
                " FERNET_KEY env var not set — a new random encryption key was generated. "
                "Previously encrypted .enc files from prior sessions are now unreadable. "
                "Set FERNET_KEY to a fixed secret to persist media encryption across restarts.",
                "WARNING",
            )

        # Encrypted media staging directory
        self.media_dir = _script_dir() / "cache"/ "moderation"
        self.media_dir.mkdir(exist_ok=True)

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

        # Message cache for context (guild_id -> channel_id -> list[msg_data])
        # Bounded: max 200 channels per guild, 100 messages per channel.
        MESSAGE_CACHE_MAX_CHANNELS = 200
        self._msg_cache_max_channels = MESSAGE_CACHE_MAX_CHANNELS
        self.message_cache = {}

        # Media cache index: message_id -> {'files': [...], 'author_id', 'guild_id', 'cached_at'}
        # TTL: entries older than MEDIA_CACHE_TTL_SECS are evicted by the cleanup task.
        MEDIA_CACHE_TTL_SECS = 3600   # 1 hour
        self._media_cache_ttl = MEDIA_CACHE_TTL_SECS
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
        self.cleanup_media_cache.start()
        self.send_daily_report.start()
        self.resolve_expired_appeals.start()

        bot.logger.log(MODULE_NAME, "Moderation system initialised (SQLite)")

    def _conn(self) -> sqlite3.Connection:
        return _conn(self._db)

    def _exec(self, query: str, params: tuple = ()):
        _db_exec(self._db, query, params)

    def _one(self, query: str, params: tuple = ()):
        return _db_one(self._db, query, params)

    def _all(self, query: str, params: tuple = ()):
        return _db_all(self._db, query, params)

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
        guild_cache = self.message_cache.setdefault(guild_id, {})

        #    evict the one that hasn't received a message longest (first key).
        if channel_id not in guild_cache and len(guild_cache) >= self._msg_cache_max_channels:
            evict_ch = next(iter(guild_cache))
            evicted_msgs = guild_cache.pop(evict_ch, [])
            for m in evicted_msgs:
                if m.get('id'):
                    self._delete_media_files(m['id'])

        guild_cache.setdefault(channel_id, [])

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
                'cached_at': datetime.now(timezone.utc).timestamp(),
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
        cache_list = guild_cache[channel_id]
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
        try:
            img.save(buffer, format='PNG')
        finally:
            img.close()   # free PIL backing store; BytesIO stays alive for the caller
        buffer.seek(0)
        return buffer

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
            "guild_id, channel_id, message_id, timestamp, context_messages, duration, "
            "additional, flags, embed_id_inchat, embed_id_botlog, status) "
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
                    f"RED FLAG: Both embeds deleted for action {action_id}", "WARNING")
        elif inchat_deleted or botlog_deleted:
            if 'yellow_flag' not in flags:
                flags.append('yellow_flag')
                self.bot.logger.log(
                    MODULE_NAME,
                    f"YELLOW FLAG: Embed deleted for action {action_id}", "WARNING")

        self._exec(
            "UPDATE mod_pending_actions SET flags=? WHERE action_id=?",
            (json.dumps(flags), action_id),
        )

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
            f"Bot-log deletion attempted by {deleter} (ID: {deleter.id}) "
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
            name="Deletion Attempted By",
            value=f"{deleter.mention} (`{deleter}` | `{deleter.id}`)", inline=False)
        original_footer = original_embed_data.get('footer') or ''
        embed.set_footer(
            text=f"{original_footer + ' • ' if original_footer else ''}"
                 f"Log ID: {log_id} • Deleting this will cause it to repost")

        new_msg_id = await self.send_bot_log(
            guild, embed, files_data=record.get('files_data'), log_id=log_id)
        if new_msg_id:
            self._deletion_warnings[new_msg_id] = log_id

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
            "status, votes_for, votes_against, channel_message_id) "
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
                        title="Ban Appeal",
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
                        value="Yes: **0**  ·   No: **0**",
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
        # Always delete the appeal record first so a partial failure never causes a retry loop.
        self._exec("DELETE FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
        try:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                return False
            user = await self.bot.fetch_user(row["user_id"])
            # Attempt unban — user may already be unbanned; treat NotFound as a no-op.
            try:
                await guild.unban(user, reason="Appeal approved")
            except discord.NotFound:
                self.bot.logger.log(MODULE_NAME,
                    f"Appeal {appeal_id}: user {row['user_id']} was not banned — skipping unban")
            invite_link = await self._create_ban_reversal_invite(guild, row["user_id"])
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
                guild_name = guild.name if guild else f"server {row['guild_id']}"
                embed = discord.Embed(
                    title="Ban Appeal Denied",
                    description=f"Your appeal for **{guild_name}** has been reviewed and denied.",
                    color=0xe74c3c, timestamp=datetime.now(timezone.utc))
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            self._exec("DELETE FROM mod_appeals WHERE appeal_id=?", (appeal_id,))
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to deny appeal {appeal_id}", e)
            return False

    async def send_action_review(self, owner: discord.User, action_id: str, action: Dict):
        embed = discord.Embed(
            title=f"{action['action'].upper()} Action Review",
            color=(0xe74c3c if 'red_flag' in action['flags'] else
                   0xf39c12 if 'yellow_flag' in action['flags'] else 0x5865f2),
            timestamp=datetime.fromisoformat(action['timestamp']),
        )
        if action['flags']:
            flags_text = []
            if 'red_flag' in action['flags']:
                flags_text.append("**RED FLAG** - Both embeds deleted")
            elif 'yellow_flag' in action['flags']:
                flags_text.append("**YELLOW FLAG** - Embed deleted")
            if 'inchat_deleted' in action['flags']:
                flags_text.append("In-chat embed deleted")
            if 'botlog_deleted' in action['flags']:
                flags_text.append("Bot-log embed deleted")
            embed.add_field(name="Flags", value="\n".join(flags_text), inline=False)
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
            lines = []
            for msg in action['context_messages']:
                ts = msg.get('timestamp', '')
                author = msg.get('author', 'unknown')
                content = (msg.get('content') or '').strip()
                att = "[Attachment]"if msg.get('attachments') else ""
                emb = "[Embed]"if msg.get('embeds', 0) > 0 else ""
                body = content or att or emb or "[Empty]"
                lines.append(f"[{ts}] {author}: {body}")
            text_dump = "\n".join(lines)
            await owner.send(file=discord.File(io.BytesIO(text_dump.encode('utf-8')), "context.txt"))

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
                    title="Daily Integrity Report",
                    description="No deletion attempts or mod-action flags in the last 24 hours.",
                    color=0x2ecc71, timestamp=datetime.now(timezone.utc))
                await owner.send(embed=embed)
                return

            embed = discord.Embed(
                title="Daily Integrity Report",
                description=(
                    f"**{len(attempt_rows)}** bot-log deletion attempt(s)\n"
                    f"**{len(red_flags)}**  red-flag mod action(s)\n"
                    f"**{len(yellow_flags)}**  yellow-flag mod action(s)"
                ),
                color=0xff4500, timestamp=datetime.now(timezone.utc))
            await owner.send(embed=embed)

            if attempt_rows:
                detail = discord.Embed(
                    title="Bot-Log Deletion Attempts",
                    color=0xff0000, timestamp=datetime.now(timezone.utc))
                for attempt in attempt_rows[:20]:
                    detail.add_field(
                        name=f"Log `{attempt['log_id']}`",
                        value=(
                            f"**By:** {attempt['deleter']} (`{attempt['deleter_id']}`)\n"
                            f"**Original:** {attempt['original_title']}\n"
                            f"**At:** {attempt['timestamp'][:19].replace('T', '')} UTC"
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
                                    f"Accepted ({votes_for}–{votes_against})"
                                    if accepted else
                                    f"Denied ({votes_against}–{votes_for})")
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

    @tasks.loop(minutes=15)
    async def cleanup_media_cache(self):
        """Evict media_cache entries whose TTL has expired."""
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - self._media_cache_ttl
            expired = [
                mid for mid, entry in list(self.media_cache.items())
                if entry.get('cached_at', 0) < cutoff
            ]
            for mid in expired:
                self._delete_media_files(mid)
            if expired:
                self.bot.logger.log(
                    MODULE_NAME,
                    f"media_cache TTL eviction: removed {len(expired)} stale entry/entries")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "media_cache cleanup error", e)

    @cleanup_media_cache.before_loop
    async def before_cleanup_media_cache(self):
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
                f"Rule **{rule_number}** not found.")
        reason = f"{rule_text} | {reason.strip()}"if reason and reason.strip() else rule_text

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
                    title="You have been banned",
                    description=f"You have been banned from **{ctx.guild.name}**",
                    color=0x992d22, timestamp=datetime.now(timezone.utc))
                dm.add_field(name="Reason",    value=dm_reason_field,    inline=False)
                dm.add_field(name="Moderator", value=str(ctx.author),    inline=True)
                dm.add_field(name="Appeal Process",
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
            title="User Banned",
            description=f"{user.mention} has been banned.",
            color=0x992d22, timestamp=datetime.now(timezone.utc))
        if rule_number is not None:
            embed.add_field(name="Rule Violated", value=f"Rule {rule_number}", inline=True)
            embed.add_field(name="Rule Text",     value=rule_text,             inline=False)
            extra = reason[len(rule_text):].lstrip("|").strip()
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
        await ctx.error("I don't have permission to ban this user.")
    except Exception as e:
        await ctx.error("An error occurred while trying to ban the user.")
        ctx.bot.logger.error(MODULE_NAME, "Ban failed", e)

async def _do_unban(ctx: ModContext, mod: ModerationSystem,
                    user_id: str, reason: str = "No reason provided", fake: bool = False):
    if not ctx.author.guild_permissions.ban_members:
        return await ctx.error("You don't have permission to unban members.")
    try:
        user = await ctx.bot.fetch_user(int(user_id))
        if not fake:
            await ctx.guild.unban(user, reason=f"{reason} - By {ctx.author}")
            mod.resolve_pending_action(user.id, 'ban')
        embed = discord.Embed(
            title="User Unbanned",
            description=f"{user.mention} has been unbanned.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reason",    value=reason,              inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,  inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unbanned {user}")
    except ValueError:
        await ctx.error("Invalid user ID.")
    except discord.NotFound:
        await ctx.error("User not found or not banned.")
    except Exception as e:
        await ctx.error("An error occurred while trying to unban.")
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
                title="You have been kicked",
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
            title="Member Kicked",
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
        await ctx.error("An error occurred while trying to kick the member.")
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
        return await ctx.error("Duration must be between 1 and 40320 minutes.")
    try:
        if not fake:
            await member.timeout(
                datetime.now(timezone.utc) + timedelta(minutes=duration),
                reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(
            title="Member Timed Out",
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
        await ctx.error("An error occurred while trying to timeout the member.")
        ctx.bot.logger.error(MODULE_NAME, "Timeout failed", e)

async def _do_untimeout(ctx: ModContext, mod: ModerationSystem,
                         member: discord.Member, fake: bool = False):
    cfg = mod.cfg
    if not ctx.author.guild_permissions.moderate_members and \
            not has_elevated_role(ctx.author, cfg):
        return await ctx.error("You don't have permission to moderate members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        if not fake:
            await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        embed = discord.Embed(
            title="Timeout Removed",
            description=f"{member.mention}'s timeout has been removed.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} removed timeout from {member}")
    except Exception as e:
        await ctx.error("An error occurred while trying to remove the timeout.")
        ctx.bot.logger.error(MODULE_NAME, "Untimeout failed", e)

async def _do_mute(ctx: ModContext, mod: ModerationSystem,
                   member: discord.Member, reason: str = "No reason provided",
                   duration: Optional[str] = None, fake: bool = False):
    cfg = mod.cfg
    if not ctx.author.guild_permissions.manage_roles and \
            not has_elevated_role(ctx.author, cfg):
        return await ctx.error("You don't have permission to mute members.")
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
                title="You Have Been Muted",
                description=f"You have been muted in **{ctx.guild.name}**.",
                color=0xf39c12, timestamp=datetime.now(timezone.utc))
            dm.add_field(name="Reason",    value=reason,          inline=False)
            dm.add_field(name="Duration",  value=duration_str,    inline=True)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        embed = discord.Embed(
            title="Member Muted",
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
        await ctx.error("I don't have permission to mute this member.")
    except Exception as e:
        await ctx.error("An error occurred while trying to mute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Mute failed", e)

async def _do_unmute(ctx: ModContext, mod: ModerationSystem,
                     member: discord.Member, fake: bool = False):
    cfg = mod.cfg
    if not ctx.author.guild_permissions.manage_roles and \
            not has_elevated_role(ctx.author, cfg):
        return await ctx.error("You don't have permission to manage roles.")
    muted_role = discord.utils.get(ctx.guild.roles, name=cfg.muted_role_name)
    if not muted_role or muted_role not in member.roles:
        return await ctx.error("This member is not muted.")
    try:
        if not fake:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
            mod.remove_mute(ctx.guild.id, member.id)
        embed = discord.Embed(
            title="Member Unmuted",
            description=f"{member.mention} has been unmuted.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unmuted {member}")
    except Exception as e:
        await ctx.error("An error occurred while trying to unmute the member.")
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
            title="Member Softbanned",
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
        await ctx.error("An error occurred while trying to softban the member.")
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
                title="Warning",
                description=f"You have been warned in **{ctx.guild.name}**",
                color=0xf39c12, timestamp=datetime.now(timezone.utc))
            dm.add_field(name="Reason",          value=reason,               inline=False)
            dm.add_field(name="Moderator",        value=str(ctx.author),      inline=True)
            dm.add_field(name="Total Warnings",   value=str(strike_count),    inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass
        embed = discord.Embed(
            title="Member Warned",
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
        await ctx.error("An error occurred while trying to warn the member.")
        ctx.bot.logger.error(MODULE_NAME, "Warn failed", e)

async def _do_warnings(ctx: ModContext, mod: ModerationSystem, member: discord.Member):
    if not ctx.author.guild_permissions.manage_messages and \
            not has_elevated_role(ctx.author, mod.cfg):
        return await ctx.error("You don't have permission to view warnings.")
    strikes = mod.get_strike_details(member.id)
    if not strikes:
        return await ctx.reply(f"**{member}** has no warnings.", ephemeral=True)
    embed = discord.Embed(
        title=f"Warnings for {member}",
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
        return await ctx.error("You need Administrator permission to clear warnings.")
    if mod.clear_strikes(member.id):
        embed = discord.Embed(
            title="Warnings Cleared",
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
        return await ctx.error("Amount must be between 1 and 100.")
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

        reason = f"Purged {len(deleted)} message(s)"+ (
            f"from {target}"if target else "")
        embed = discord.Embed(
            title="Messages Purged",
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
        await ctx.followup("An error occurred while trying to purge messages.",
                           ephemeral=True)
        ctx.bot.logger.error(MODULE_NAME, "Purge failed", e)

async def _do_slowmode(ctx: ModContext, mod: ModerationSystem,
                       seconds: int, channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("You don't have permission to manage channels.")
    if not (0 <= seconds <= 21600):
        return await ctx.error("Slowmode must be between 0 and 21600 seconds.")
    target = channel or ctx.channel
    try:
        await target.edit(slowmode_delay=seconds,
                          reason=f"Slowmode set by {ctx.author}")
        if seconds == 0:
            embed = discord.Embed(title="Slowmode Disabled",
                                  description=f"Slowmode disabled in {target.mention}.",
                                  color=0x2ecc71)
        else:
            embed = discord.Embed(title="Slowmode Enabled",
                                  description=f"Slowmode set to **{seconds}s** in {target.mention}.",
                                  color=0x3498db)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(
            MODULE_NAME, f"{ctx.author} set slowmode to {seconds}s in {target.name}")
    except Exception as e:
        await ctx.error("An error occurred while trying to set slowmode.")
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
            title="Channel Locked",
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
        await ctx.error("An error occurred while trying to lock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Lock failed", e)

async def _do_unlock(ctx: ModContext, mod: ModerationSystem,
                     channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("You don't have permission to manage channels.")
    target = channel or ctx.channel
    try:
        await target.set_permissions(
            ctx.guild.default_role, send_messages=None,
            reason=f"Unlocked by {ctx.author}")
        embed = discord.Embed(
            title="Channel Unlocked",
            description=f"{target.mention} has been unlocked.",
            color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unlocked {target.name}")
    except Exception as e:
        await ctx.error("An error occurred while trying to unlock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Unlock failed", e)

def setup(bot):
    mod_system = ModerationSystem(bot)
    bot._mod_system        = mod_system
    bot.moderation_manager = mod_system
    bot.mod_oversight      = mod_system
    bot.moderation         = mod_system

    # Re-register persistent views so buttons work after bot restarts
    bot.add_view(BanAppealView(guild_id=0))
    # Re-register one AppealVoteView per live appeal — custom_ids are per-appeal
    # so a single placeholder view cannot cover them all after a restart.
    for appeal_row in mod_system._all("SELECT appeal_id FROM mod_appeals WHERE status='pending'"):
        bot.add_view(AppealVoteView(moderation_system=mod_system, appeal_id=appeal_row["appeal_id"]))
    # Re-register one ActionReviewView per pending action so owner DM buttons still work
    for row in mod_system._all("SELECT * FROM mod_pending_actions WHERE status='pending'"):
        action = mod_system._row_to_action(row)
        bot.add_view(ActionReviewView(mod_system, action['id'], action))

    # Convenience shorthand used throughout
    _mod = mod_system
    _cfg = mod_system.cfg

    # ---- SLASH COMMANDS ----

    @bot.tree.command(name="ban", description="[Mod] Ban a user from the server")
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
                "You must provide either a reason or a rule number.", ephemeral=True)
            return
        await _do_ban(ModContext(interaction), _mod, user, reason,
                      delete_days, fake=fake, rule_number=rule)

    @bot.tree.command(name="multiban", description="[Mod] Ban multiple users at once with one reason")
    @app_commands.describe(
        user_ids="Space-separated list of user IDs to ban",
        reason="Reason applied to all bans",
        delete_days="Days of messages to delete (0-7)",
        fake="Simulate without executing",
    )
    @app_commands.default_permissions(ban_members=True)
    async def slash_multiban(interaction: discord.Interaction, user_ids: str,
                             reason: str, delete_days: Optional[int] = 0,
                             fake: bool = False):
        if not has_elevated_role(interaction.user, _mod.cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return

        raw_ids = user_ids.split()
        if not raw_ids:
            await interaction.response.send_message(
                "Provide at least one user ID.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Create a single ModContext for the whole multiban operation and mark it
        # as already replied (because we just deferred), so every subsequent
        # ctx.reply() inside _do_ban correctly uses followup.send instead of
        # response.send_message (which would raise InteractionResponded).
        ban_ctx = ModContext(interaction)
        ban_ctx._replied = True

        skipped: list[str] = []
        banned_count = 0
        for raw in raw_ids:
            raw = raw.strip("<@!>")
            if not raw.isdigit():
                skipped.append(f"`{raw}` — not a valid ID, skipped")
                continue
            try:
                user = await interaction.client.fetch_user(int(raw))
            except discord.NotFound:
                skipped.append(f"`{raw}` — user not found, skipped")
                continue
            except Exception:
                skipped.append(f"`{raw}` — fetch failed, skipped")
                continue

            # Rate-limit: wait 10s between each ban (skip delay before the first)
            if banned_count > 0:
                await asyncio.sleep(10)

            try:
                await _do_ban(ban_ctx, _mod, user, reason,
                              delete_days or 0, fake=fake, rule_number=None)
                banned_count += 1
            except Exception:
                skipped.append(f"**{user}** (`{user.id}`) — ban failed")

        # Only report skipped/failed entries; successful bans already have their own embeds
        summary_parts = [f"**Multiban complete** — {banned_count} banned"
                         + ("(dry run)"if fake else "")]
        if skipped:
            summary_parts.append("\n".join(skipped))
        await interaction.followup.send("\n".join(summary_parts), ephemeral=True)

    @bot.tree.command(name="unban", description="[Mod] Unban a user from the server")
    @app_commands.describe(user_id="User ID to unban", reason="Reason for unban",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_unban(interaction: discord.Interaction, user_id: str,
                          reason: Optional[str] = "No reason provided", fake: bool = False):
        await _do_unban(ModContext(interaction), _mod, user_id, reason, fake=fake)

    @bot.tree.command(name="kick", description="[Mod] Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(kick_members=True)
    async def slash_kick(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_kick(ModContext(interaction), _mod, member, reason, fake=fake)

    @bot.tree.command(name="timeout", description="[Mod] Timeout a member")
    @app_commands.describe(member="Member to timeout", duration="Duration in minutes",
                           reason="Reason (min 10 chars)", fake="Simulate without executing")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_timeout(interaction: discord.Interaction, member: discord.Member,
                            duration: int, reason: str, fake: bool = False):
        await _do_timeout(ModContext(interaction), _mod, member, duration, reason, fake=fake)

    @bot.tree.command(name="untimeout", description="[Mod] Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from",
                           fake="Simulate without executing")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_untimeout(interaction: discord.Interaction, member: discord.Member,
                               fake: bool = False):
        await _do_untimeout(ModContext(interaction), _mod, member, fake=fake)

    @bot.tree.command(name="mute", description="[Mod] Mute a member")
    @app_commands.describe(member="Member to mute", reason="Reason for mute",
                           duration="Duration e.g. 10m, 1h, 1d (empty = permanent)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_mute(interaction: discord.Interaction, member: discord.Member,
                         reason: str = "No reason provided",
                         duration: Optional[str] = None, fake: bool = False):
        await _do_mute(ModContext(interaction), _mod, member, reason, duration, fake=fake)

    @bot.tree.command(name="unmute", description="[Mod] Unmute a member")
    @app_commands.describe(member="Member to unmute", fake="Simulate without executing")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_unmute(interaction: discord.Interaction, member: discord.Member,
                            fake: bool = False):
        await _do_unmute(ModContext(interaction), _mod, member, fake=fake)

    @bot.tree.command(name="softban",
                      description="[Mod] Softban a member (ban+unban to delete messages)")
    @app_commands.describe(member="Member to softban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 7)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_softban(interaction: discord.Interaction, member: discord.Member,
                            reason: str, delete_days: Optional[int] = 7,
                            fake: bool = False):
        await _do_softban(ModContext(interaction), _mod, member, reason,
                          delete_days, fake=fake)

    @bot.tree.command(name="warn", description="[Mod] Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warn(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_warn(ModContext(interaction), _mod, member, reason, fake=fake)

    @bot.tree.command(name="warnings", description="[Mod] View warnings for a member")
    @app_commands.describe(member="Member to check")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
        await _do_warnings(ModContext(interaction), _mod, member)

    @bot.tree.command(name="clearwarnings", description="[Admin] Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.default_permissions(administrator=True)
    async def slash_clearwarnings(interaction: discord.Interaction, member: discord.Member):
        await _do_clearwarnings(ModContext(interaction), _mod, member)

    @bot.tree.command(name="purge", description="[Mod] Bulk-delete messages in this channel")
    @app_commands.describe(amount="Number of messages to delete (1-100)",
                           user="Only delete messages from this user (optional)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_purge(interaction: discord.Interaction, amount: int,
                          user: Optional[discord.Member] = None, fake: bool = False):
        await _do_purge(ModContext(interaction), _mod, amount, user, fake=fake)

    @bot.tree.command(name="slowmode", description="[Mod] Set slowmode delay for this channel")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)",
                           channel="Channel to apply to (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_slowmode(interaction: discord.Interaction, seconds: int,
                             channel: Optional[discord.TextChannel] = None):
        await _do_slowmode(ModContext(interaction), _mod, seconds, channel)

    @bot.tree.command(name="lock", description="[Mod] Prevent members from sending messages")
    @app_commands.describe(reason="Reason for locking (min 10 chars)",
                           channel="Channel to lock (default: current)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_lock(interaction: discord.Interaction, reason: str,
                         channel: Optional[discord.TextChannel] = None, fake: bool = False):
        await _do_lock(ModContext(interaction), _mod, reason, channel, fake=fake)

    @bot.tree.command(name="unlock", description="[Mod] Re-allow members to send messages")
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
                "Usage: `?ban @user <reason> [rule:<n>] [days:<0-7>] [fake]`",
                delete_after=10)
        working    = args
        fake       = bool(re.search(r'(?:^|\s)fake(?:\s|$)', working, re.IGNORECASE))
        working    = re.sub(r'(?:^|\s)fake(?=\s|$)', '', working, flags=re.IGNORECASE)
        rule_match = re.search(r'(?:^|\s)rule:(\d+)(?=\s|$)', working, re.IGNORECASE)
        rule_number = int(rule_match.group(1)) if rule_match else None
        if rule_match:
            working = re.sub(r'(?:^|\s)rule:\d+(?=\s|$)', '', working, flags=re.IGNORECASE)
        days_match  = re.search(r'(?:^|\s)days:(\d+)(?=\s|$)', working, re.IGNORECASE)
        delete_days = int(days_match.group(1)) if days_match else 0
        if days_match:
            working = re.sub(r'(?:^|\s)days:\d+(?=\s|$)', '', working, flags=re.IGNORECASE)
        reason = working.strip() or None
        if rule_number is None and not reason:
            return await ctx.send(
                "You must provide either a reason or `rule:<n>`.", delete_after=8)
        await _do_ban(ModContext(ctx), _mod, user, reason, delete_days,
                      fake=fake, rule_number=rule_number)

    @bot.command(name="multiban")
    async def prefix_multiban(ctx, *args):
        """Usage: ?multiban <id|@user> [id|@user ...] reason:<reason> [days:<0-7>] [fake]"""
        if not args:
            return await ctx.send(
                "Usage: `?multiban <id/@user> [id/@user ...] reason:<text> [days:<0-7>] [fake]`",
                delete_after=10)

        fake        = any(a.lower() == "fake"for a in args)
        args        = [a for a in args if a.lower() != "fake"]

        reason_match = re.search(r'reason:(.+)', "".join(args), re.IGNORECASE)
        if not reason_match:
            return await ctx.send(
                "You must include `reason:<text>` in the command.", delete_after=8)
        reason = reason_match.group(1).strip()

        days_match  = re.search(r'days:(\d+)', "".join(args), re.IGNORECASE)
        delete_days = int(days_match.group(1)) if days_match else 0

        # Everything before the first reason:/days:/fake token is a user ref
        user_refs = []
        for a in args:
            if re.match(r'reason:', a, re.IGNORECASE): break
            if re.match(r'days:\d+', a, re.IGNORECASE): continue
            user_refs.append(a.strip("<@!>"))

        if not user_refs:
            return await ctx.send("No users specified.", delete_after=8)

        skipped: list[str] = []
        banned_count = 0
        for raw in user_refs:
            if not raw.isdigit():
                skipped.append(f"`{raw}` — not a valid ID, skipped")
                continue
            try:
                user = await ctx.bot.fetch_user(int(raw))
            except discord.NotFound:
                skipped.append(f"`{raw}` — user not found, skipped")
                continue
            except Exception:
                skipped.append(f"`{raw}` — fetch failed, skipped")
                continue

            # Rate-limit: wait 10s between each ban (skip delay before the first)
            if banned_count > 0:
                await asyncio.sleep(10)

            try:
                await _do_ban(ModContext(ctx), _mod, user, reason, delete_days, fake=fake)
                banned_count += 1
            except Exception:
                skipped.append(f"**{user}** (`{user.id}`) — ban failed")

        # Only report skipped/failed entries; successful bans already have their own embeds
        summary_parts = [f"**Multiban complete** — {banned_count} banned"
                         + ("(dry run)"if fake else "")]
        if skipped:
            summary_parts.append("\n".join(skipped))
        await ctx.send("\n".join(summary_parts))

    @bot.command(name="unban")
    async def prefix_unban(ctx, user_id: str = None, *, reason: str = "No reason provided"):
        if not user_id:
            return await ctx.send("Usage: `?unban <user_id> [reason]`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake"if parts else False
        if fake:
            reason = "".join(parts[:-1]) or "No reason provided"
        await _do_unban(ModContext(ctx), _mod, user_id, reason, fake=fake)

    @bot.command(name="kick")
    async def prefix_kick(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("Usage: `?kick @member <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake"if parts else False
        if fake:
            reason = "".join(parts[:-1])
        await _do_kick(ModContext(ctx), _mod, member, reason, fake=fake)

    @bot.command(name="timeout")
    async def prefix_timeout(ctx, member: discord.Member = None,
                              duration: int = None, *, reason: str = ""):
        if not member or duration is None:
            return await ctx.send(
                "Usage: `?timeout @member <minutes> <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake"if parts else False
        if fake:
            reason = "".join(parts[:-1])
        await _do_timeout(ModContext(ctx), _mod, member, duration, reason, fake=fake)

    @bot.command(name="untimeout")
    async def prefix_untimeout(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("Usage: `?untimeout @member`", delete_after=8)
        await _do_untimeout(ModContext(ctx), _mod, member, fake=fake.lower() == "fake")

    @bot.command(name="mute")
    async def prefix_mute(ctx, member: discord.Member = None, *, args: str = ""):
        if not member:
            return await ctx.send(
                "Usage: `?mute @member [duration] [reason]`", delete_after=8)
        duration = None
        reason   = args or "No reason provided"
        parts    = args.split(None, 1)
        if parts and re.match(r'^\d+[smhd]$', parts[0].lower()):
            duration = parts[0]
            reason   = parts[1] if len(parts) > 1 else "No reason provided"
        r_parts = reason.split()
        fake    = r_parts[-1].lower() == "fake"if r_parts else False
        if fake:
            reason = "".join(r_parts[:-1]) or "No reason provided"
        await _do_mute(ModContext(ctx), _mod, member, reason, duration, fake=fake)

    @bot.command(name="unmute")
    async def prefix_unmute(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("Usage: `?unmute @member`", delete_after=8)
        await _do_unmute(ModContext(ctx), _mod, member, fake=fake.lower() == "fake")

    @bot.command(name="softban")
    async def prefix_softban(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("Usage: `?softban @member <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake"if parts else False
        if fake:
            reason = "".join(parts[:-1])
        await _do_softban(ModContext(ctx), _mod, member, reason, fake=fake)

    @bot.command(name="warn")
    async def prefix_warn(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("Usage: `?warn @member <reason>`", delete_after=8)
        parts = reason.split()
        fake  = parts[-1].lower() == "fake"if parts else False
        if fake:
            reason = "".join(parts[:-1])
        await _do_warn(ModContext(ctx), _mod, member, reason, fake=fake)

    @bot.command(name="warnings")
    async def prefix_warnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("Usage: `?warnings @member`", delete_after=8)
        await _do_warnings(ModContext(ctx), _mod, member)

    @bot.command(name="clearwarnings")
    async def prefix_clearwarnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("Usage: `?clearwarnings @member`", delete_after=8)
        await _do_clearwarnings(ModContext(ctx), _mod, member)

    @bot.command(name="purge")
    async def prefix_purge(ctx, amount: int = None,
                            member: discord.Member = None, fake: str = ""):
        if amount is None:
            return await ctx.send("Usage: `?purge <amount> [@member]`", delete_after=8)
        await _do_purge(ModContext(ctx), _mod, amount, member,
                        fake=fake.lower() == "fake")

    @bot.command(name="slowmode")
    async def prefix_slowmode(ctx, seconds: int = None,
                               channel: discord.TextChannel = None):
        if seconds is None:
            return await ctx.send(
                "Usage: `?slowmode <seconds> [#channel]`", delete_after=8)
        await _do_slowmode(ModContext(ctx), _mod, seconds, channel)

    @bot.command(name="lock")
    async def prefix_lock(ctx, channel: Optional[discord.TextChannel] = None, *, reason: str = ""):
        parts = reason.split()
        fake  = parts[-1].lower() == "fake"if parts else False
        if fake:
            reason = "".join(parts[:-1])
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
                "This command is restricted to the bot owner.", ephemeral=True)
            return
        await interaction.response.send_message("Generating report...", ephemeral=True)
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
                        title="Bot-Log Deletion Warning — REPOSTED",
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
                    title="Attachment Removed from Message",
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
            label     = "audio"if has_audio else "file"
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
                "Rules file not found or could not be loaded.", ephemeral=True)
            return
        embed = rules_manager.build_embed(data)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="updaterules",
                      description="[Admin] Force-refresh the #rules channel embed")
    @app_commands.default_permissions(administrator=True)
    async def slash_updaterules(interaction: discord.Interaction):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        posted = await rules_manager.sync(interaction.guild, force=True)
        if posted:
            await interaction.followup.send(
                "Rules embed has been refreshed in the rules channel.", ephemeral=True)
        else:
            await interaction.followup.send(
                "ℹ Rules embed is already up to date.", ephemeral=True)

    _setup_suspicion(bot, _mod, _cfg)
    bot.logger.log(MODULE_NAME, "Moderation setup complete")

#  SUSPICION / FED-FINGERPRINT SYSTEM
#  Each signal below contributes a point value to a member's suspicion score.
#  If the score meets or exceeds SUSPICION_THRESHOLD the account is auto-flagged
#  and access to sensitive bot features (e.g. remaster downloads) is silently
#  denied. Mods can manually clear or flag any account.
#  Public API for other modules:
#      from moderation import is_flagged
#      if is_flagged(guild_id, user_id): ...
# Tune these freely. Score >= SUSPICION_THRESHOLD → auto-flagged.

SUSPICION_THRESHOLD = 6

SIGNAL_WEIGHTS: dict[str, int] = {
    # Account characteristics
    "account_age_under_7d":      4,   # created less than 7 days ago
    "account_age_under_30d":     2,   # created less than 30 days ago
    "default_avatar":            2,   # still using a Discord default avatar
    "no_bio":                    1,   # no profile bio / about me set
    "throwaway_username":        1,   # username matches random/throwaway patterns

    # Server behaviour
    "joined_recently_under_7d":  1,   # joined this server less than 7 days ago
    "no_messages":               2,   # zero messages ever recorded in server
    "only_releases_role":        2,   # only role is @everyone + releases (no others)

    # Join vector — most significant signal
    "invite_leaktracker":        5,   # joined via a known leak-tracker invite
    "invite_youtube":            1,   # joined via the YouTube description invite
    "invite_unknown":            2,   # joined via an invite we can't identify
    # "invite_custom"contributes 0 — trusted member invite, no penalty
}

import re as _re
_THROWAWAY_PATTERNS = [
    _re.compile(r'^[a-z]{3,6}\d{4,}$'),          # word + 4+ digits  e.g. "user2847"
    _re.compile(r'^\d{6,}$'),                      # all digits
    _re.compile(r'^[a-z0-9]{20,}$'),               # very long random alphanumeric
    _re.compile(r'^(user|account|member)\d+$', _re.I),
]

# Discord default avatars are served from /assets/ — we detect them by checking
# if display_avatar.key is one of the known default keys, or if the URL contains
# /embed/avatars/ (the legacy path) or /assets/ (the new path).
def _is_default_avatar(member: discord.Member) -> bool:
    url = str(member.display_avatar.url)
    return "/embed/avatars/"in url or "/assets/"in url and "a_"not in url

#  SuspicionEngine
class SuspicionEngine:
    """
    Scores members against a set of risk signals and maintains a flag/clear
    record in the moderation DB. Designed to be instantiated once in setup()
    and stored on bot.suspicion.
    """

    def __init__(self, bot: commands.Bot, db_path: str, cfg: "ModConfig"):
        self.bot      = bot
        self._db      = db_path
        self.cfg      = cfg

    def _exec(self, q, p=()):  _db_exec(self._db, q, p)
    def _one(self, q, p=()):   return _db_one(self._db, q, p)
    def _all(self, q, p=()):   return _db_all(self._db, q, p)

    def _label_invite(self, code: str) -> str:
        """
        Classify an invite code using config/moderation.json:
            "invite_labels": {
                "leaktracker": ["abc123", "xyz789"],
                "youtube":     ["yt4ever"]
            }
        If the code matches a leaktracker or youtube entry, that label is returned.
        Anything else is assumed to be a custom (member-created) invite — trusted.
        """
        labels: dict = self.cfg.get("invite_labels", {})
        for label, codes in labels.items():
            if code in (codes or []):
                return label
        return "custom"

    async def score_member(self, member: discord.Member,
                           invite_source: str = "custom") -> dict:
        """
        Evaluate all signals for a member and upsert their suspicion record.
        Returns the record dict.
        """
        gid = str(member.guild.id)
        uid = str(member.id)
        now = datetime.now(timezone.utc)

        signals: list[str] = []
        score = 0

        def add(signal: str):
            w = SIGNAL_WEIGHTS.get(signal, 0)
            signals.append(signal)
            return w

        acct_age = (now - member.created_at).days
        if acct_age < 7:
            score += add("account_age_under_7d")
        elif acct_age < 30:
            score += add("account_age_under_30d")

        if _is_default_avatar(member):
            score += add("default_avatar")

        # discord.py exposes bio via member.bio after fetch — we skip if absent
        # to avoid an extra HTTP call on every join. Instead we check lazily.
        # Mark it for deferred check.
        bio_signal_pending = True   # resolved below via fetch if possible

        uname = (member.name or "").lower()
        if any(p.match(uname) for p in _THROWAWAY_PATTERNS):
            score += add("throwaway_username")

        if member.joined_at:
            join_age = (now - member.joined_at).days
            if join_age < 7:
                score += add("joined_recently_under_7d")

        # We can only know this if moderation.py has been tracking messages.
        # We rely on the absence of any cached message history as a proxy.
        # This is intentionally conservative — if we can't tell, we don't score.
        # The no_messages signal is set to 0 for brand-new members; it updates
        # when score_member is called again after observation time.
        existing = self._one(
            "SELECT * FROM mod_suspicion WHERE guild_id=? AND user_id=?", (gid, uid))
        if existing:
            # Re-score: check if they've sent any messages since last scored
            # (message counting is tracked via on_message below)
            msg_count = self._one(
                "SELECT msg_count FROM mod_suspicion WHERE guild_id=? AND user_id=?", (gid, uid))
            if msg_count and msg_count["msg_count"] == 0:
                score += add("no_messages")

        releases_role_name = self.cfg.get("releases_role_name",
                                          self.bot.__dict__.get("_remasters_role_name",
                                                                "Emball Releases"))
        non_default_roles = [r for r in member.roles
                             if r.name != "@everyone"and r.name != releases_role_name]
        if not non_default_roles:
            score += add("only_releases_role")

        if invite_source == "leaktracker":
            score += add("invite_leaktracker")
        elif invite_source == "youtube":
            score += add("invite_youtube")
        # "custom"→ 0 points (trusted member invite)

        try:
            fetched = await member.guild.fetch_member(member.id)
            profile = await fetched.user.profile()   # type: ignore[attr-defined]
            if not profile.bio:
                score += add("no_bio")
        except Exception:
            pass  # Not available or rate-limited — skip

        auto_flagged  = score >= SUSPICION_THRESHOLD
        flagged_at    = now.isoformat() if auto_flagged else None

        # Don't overwrite a manual clear
        if existing and existing["cleared"]:
            auto_flagged = False
            flagged_at   = None

        self._exec(
            """
            INSERT INTO mod_suspicion
              (guild_id, user_id, score, flagged, cleared, invite_source,
               scored_at, flagged_at, signals)
            VALUES (?,?,?,?,0,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              score         = excluded.score,
              flagged       = CASE WHEN cleared=1 THEN flagged ELSE excluded.flagged END,
              invite_source = excluded.invite_source,
              scored_at     = excluded.scored_at,
              flagged_at    = CASE WHEN cleared=1 THEN flagged_at ELSE excluded.flagged_at END,
              signals       = excluded.signals
            """,
            (gid, uid, score, int(auto_flagged),
             invite_source,
             now.isoformat(), flagged_at,
             json.dumps(signals))
        )

        record = self._one(
            "SELECT * FROM mod_suspicion WHERE guild_id=? AND user_id=?", (gid, uid))
        record = dict(record)

        if auto_flagged and not (existing and existing["flagged"]):
            self.bot.logger.log(
                MODULE_NAME,
                f"AUTO-FLAGGED {member} (id={uid}) — score {score}/{SUSPICION_THRESHOLD} "
                f"signals: {signals}"
            )
            await self._notify_mods(member, record)

        return record

    async def _notify_mods(self, member: discord.Member, record: dict) -> None:
        """Post a quiet heads-up to the bot-logs channel."""
        bot_logs = None
        for guild in self.bot.guilds:
            ch_id = self.cfg.bot_logs_channel_id
            if ch_id:
                bot_logs = guild.get_channel(ch_id)
                if bot_logs:
                    break
        if not bot_logs:
            return

        signals = json.loads(record.get("signals", "[]"))
        score   = record.get("score", 0)

        embed = discord.Embed(
            title=" Suspicious Account Flagged",
            color=discord.Color.from_rgb(255, 160, 50),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(member),
            icon_url=member.display_avatar.url,
        )
        embed.add_field(name="User", value=member.mention, inline=True)
        embed.add_field(name="Score", value=f"`{score}` / threshold `{SUSPICION_THRESHOLD}`", inline=True)
        embed.add_field(name="Invite source", value=record.get('invite_source', 'custom'), inline=True)
        embed.add_field(
            name="Signals",
            value="\n".join(f"• `{s}`  (+{SIGNAL_WEIGHTS.get(s, 0)})"for s in signals) or "none",
            inline=False,
        )
        embed.set_footer(text=f"Use /fedcheck, /fedflag, or /fedclear to manage  ·  ID: {member.id}")
        await bot_logs.send(embed=embed)

    def manual_flag(self, guild_id: str, user_id: str, note: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._exec(
            """
            INSERT INTO mod_suspicion
              (guild_id, user_id, score, flagged, cleared, scored_at, flagged_at, signals, note)
            VALUES (?,?,0,1,0,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              flagged=1, cleared=0, flagged_at=excluded.flagged_at, note=excluded.note
            """,
            (guild_id, user_id, now, now, "[]", note)
        )

    def manual_clear(self, guild_id: str, user_id: str, cleared_by: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._exec(
            """
            INSERT INTO mod_suspicion
              (guild_id, user_id, score, flagged, cleared, scored_at, cleared_at, cleared_by, signals)
            VALUES (?,?,0,0,1,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              flagged=0, cleared=1, cleared_at=excluded.cleared_at, cleared_by=excluded.cleared_by
            """,
            (guild_id, user_id, now, now, cleared_by, "[]")
        )

    def is_flagged(self, guild_id: str, user_id: str) -> bool:
        """
        Returns True if the user is flagged and has NOT been manually cleared.
        This is the function remasters.py (and any other module) should call.
        """
        row = self._one(
            "SELECT flagged, cleared FROM mod_suspicion WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id))
        )
        if not row:
            return False
        return bool(row["flagged"]) and not bool(row["cleared"])

    def get_record(self, guild_id: str, user_id: str) -> dict | None:
        row = self._one(
            "SELECT * FROM mod_suspicion WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id))
        )
        return dict(row) if row else None

#  Module-level public API (imported by remasters.py and others)
def is_flagged(guild_id, user_id: str) -> bool:
    """
    Convenience wrapper so other modules can do:
        from moderation import is_flagged
        if is_flagged(guild_id, user_id): ...
    Requires bot.suspicion to have been set up (i.e. moderation module loaded).
    Returns False safely if the suspicion system is unavailable.
    """
    import sys as _sys
    mod = _sys.modules.get("moderation")
    if not mod:
        return False
    engine: SuspicionEngine | None = getattr(mod, "_suspicion_engine", None)
    if not engine:
        return False
    return engine.is_flagged(str(guild_id), str(user_id))

_suspicion_engine: SuspicionEngine | None = None

#  SUSPICION SYSTEM — wired into setup()
def _setup_suspicion(bot: commands.Bot, _mod: "ModerationSystem", _cfg: "ModConfig"):
    """Called at the end of setup() to attach the suspicion system."""
    global _suspicion_engine

    engine = SuspicionEngine(bot, _mod._db, _cfg)
    _suspicion_engine = engine
    bot.suspicion     = engine   # accessible as bot.suspicion from anywhere

    @bot.listen("on_member_join")
    async def _suspicion_on_member_join(member: discord.Member):
        # Try to identify which invite was used; requires Manage Guild perm.
        invite_source = "custom"
        try:
            invites = await member.guild.invites()
            labels: dict = _cfg.get("invite_labels", {})
            for inv in invites:
                for label, codes in labels.items():
                    if inv.code in (codes or []):
                        # We can't reliably diff without a pre-join snapshot,
                        # but if the server only has one leaktracker/youtube
                        # invite, seeing it in the list is sufficient signal.
                        invite_source = label
                        break
        except discord.Forbidden:
            pass
        await engine.score_member(member, invite_source=invite_source)

    @bot.listen("on_member_update")
    async def _suspicion_on_member_update(before: discord.Member, after: discord.Member):
        if [r.id for r in before.roles] != [r.id for r in after.roles]:
            # Only rescore if they already have a record (don't create on every update)
            existing = engine.get_record(str(after.guild.id), str(after.id))
            if existing and not existing.get("cleared"):
                await engine.score_member(after)

    @bot.listen("on_message")
    async def _suspicion_on_message(message: discord.Message):
        if message.author.bot or not message.guild:
            return
        gid = str(message.guild.id)
        uid = str(message.author.id)
        # Increment message counter — using an extra column we add lazily via ALTER
        try:
            engine._exec(
                "UPDATE mod_suspicion SET msg_count = COALESCE(msg_count, 0) + 1 "
                "WHERE guild_id=? AND user_id=?",
                (gid, uid)
            )
        except Exception:
            pass  # Column may not exist yet; migration handles it

    @bot.tree.command(name="fedcheck",
                      description="[Mod] Show suspicion report for a member")
    @app_commands.describe(member="Member to inspect")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_fedcheck(interaction: discord.Interaction, member: discord.Member):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return

        record = engine.get_record(str(interaction.guild_id), str(member.id))

        embed = discord.Embed(
            title=f" Suspicion Report — {member}",
            color=(discord.Color.red() if (record and record["flagged"] and not record["cleared"])
                   else discord.Color.green() if (record and record["cleared"])
                   else discord.Color.greyple()),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Status",
                        value=("Flagged"if record and record["flagged"] and not record["cleared"]
                               else "Cleared"if record and record["cleared"]
                               else "⬜ Unscored"),
                        inline=True)
        embed.add_field(name="Score",
                        value=f"`{record['score'] if record else '—'}` / `{SUSPICION_THRESHOLD}`",
                        inline=True)

        if record:
            signals = json.loads(record.get("signals") or "[]")
            embed.add_field(
                name="Triggered signals",
                value=("\n".join(f"• `{s}`  (+{SIGNAL_WEIGHTS.get(s, 0)})"for s in signals)
                       or "none"),
                inline=False,
            )
            embed.add_field(name="Invite source",
                            value=record.get('invite_source', 'custom'),
                            inline=True)
            embed.add_field(name="Scored at",
                            value=record.get("scored_at", "—")[:19].replace("T", ""),
                            inline=True)
            if record.get("cleared"):
                embed.add_field(name="Cleared by",
                                value=record.get("cleared_by", "unknown"),
                                inline=True)
            if record.get("note"):
                embed.add_field(name="Note", value=record["note"], inline=False)

        acct_age = (datetime.now(timezone.utc) - member.created_at).days
        join_age = ((datetime.now(timezone.utc) - member.joined_at).days
                    if member.joined_at else "?")
        embed.add_field(name="Account age",  value=f"{acct_age}d", inline=True)
        embed.add_field(name="Server tenure", value=f"{join_age}d", inline=True)
        embed.set_footer(text=f"ID: {member.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="fedflag",
                      description="[Mod] Manually flag a member as suspicious")
    @app_commands.describe(member="Member to flag", note="Optional note")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_fedflag(interaction: discord.Interaction,
                            member: discord.Member,
                            note: str = ""):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(ERROR_CANNOT_ACTION_SELF, ephemeral=True)
            return

        engine.manual_flag(str(interaction.guild_id), str(member.id), note=note)
        bot.logger.log(MODULE_NAME,
                       f"MANUAL FLAG: {member} flagged by {interaction.user} — {note or 'no note'}")
        await interaction.response.send_message(
            f"**{member}** has been flagged. They will be silently denied access to "
            f"protected features.",
            ephemeral=True
        )

    @bot.tree.command(name="fedclear",
                      description="[Mod] Clear a suspicion flag from a member")
    @app_commands.describe(member="Member to clear")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_fedclear(interaction: discord.Interaction, member: discord.Member):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return

        engine.manual_clear(str(interaction.guild_id), str(member.id),
                             cleared_by=str(interaction.user))
        bot.logger.log(MODULE_NAME,
                       f"CLEARED: {member} cleared by {interaction.user}")
        await interaction.response.send_message(
            f"Suspicion flag cleared for **{member}**. They now have normal access.",
            ephemeral=True
        )

    @bot.tree.command(name="fedscan",
                      description="[Admin] Re-score all members in the server")
    @app_commands.default_permissions(administrator=True)
    async def slash_fedscan(interaction: discord.Interaction):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild   = interaction.guild
        scanned = 0
        flagged = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            record = await engine.score_member(member)
            scanned += 1
            if record.get("flagged") and not record.get("cleared"):
                flagged += 1
        await interaction.followup.send(
            f"Scan complete — **{scanned}** members scored, **{flagged}** flagged.",
            ephemeral=True
        )

    @bot.tree.command(name="fedinvites",
                      description="[Admin] Show invite classification labels")
    @app_commands.default_permissions(administrator=True)
    async def slash_fedinvites(interaction: discord.Interaction):
        if not has_elevated_role(interaction.user, _cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        labels: dict = _cfg.get("invite_labels", {})
        if not labels:
            await interaction.response.send_message(
                "No invite labels configured yet. Add them to `config/moderation.json` under "
                "`invite_labels`: `{\"leaktracker\": [\"code1\"], \"youtube\": [\"code2\"]}`",
                ephemeral=True
            )
            return
        lines = []
        for label, codes in labels.items():
            for code in codes:
                lines.append(f"• `{code}` → **{label}**")
        await interaction.response.send_message(
            "**Invite label config:**\n"+ "\n".join(lines) or "Empty.",
            ephemeral=True
        )

    try:
        _db_exec(_mod._db,
                 "ALTER TABLE mod_suspicion ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # Column already exists — fine

    bot.logger.log(MODULE_NAME, "Suspicion engine loaded — commands: /fedcheck /fedflag /fedclear /fedscan /fedinvites")

