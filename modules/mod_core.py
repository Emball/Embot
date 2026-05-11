import discord
from discord import app_commands
from discord.ext import tasks
import re
import json
import os
import sys
import asyncio
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import sqlite3
import io
from collections import deque
import pytz
from _utils import script_dir, _now
import _messages as msg_cache

MODULE_NAME = "MODERATION"

ERROR_NO_PERMISSION      = "You need a moderation role (Moderator, Admin, or Owner) to use this command."
ERROR_REASON_REQUIRED    = "You must provide a reason for this action."
ERROR_CANNOT_ACTION_SELF = "You cannot perform this action on yourself."
ERROR_CANNOT_ACTION_BOT  = "I cannot perform this action on myself."
ERROR_HIGHER_ROLE        = "You cannot perform this action on someone with a higher or equal role."

def _db_path() -> str:
    p = script_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    old = p / "moderation.db"
    new = p / "mod.db"
    if old.exists() and not new.exists():
        old.rename(new)
    elif new.exists() and old.exists():
        old.unlink()
    return str(new)

DB_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS mod_member_roles (
    guild_id    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role_ids    TEXT NOT NULL,
    saved_at    TEXT NOT NULL,
    username    TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS mod_strikes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    reason      TEXT NOT NULL
);

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
    context_messages    TEXT,
    duration            TEXT,
    additional          TEXT,
    flags               TEXT,
    embed_id_inchat     INTEGER,
    embed_id_botlog     INTEGER,
    status              TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS mod_appeals (
    appeal_id           TEXT PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    guild_id            INTEGER NOT NULL,
    appeal_text         TEXT NOT NULL,
    submitted_at        TEXT NOT NULL,
    deadline            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    votes_for           TEXT NOT NULL DEFAULT '[]',
    votes_against       TEXT NOT NULL DEFAULT '[]',
    channel_message_id  INTEGER
);

CREATE TABLE IF NOT EXISTS mod_invites (
    invite_key  TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_rules_state (
    guild_id    TEXT PRIMARY KEY,
    message_id  INTEGER,
    rules_hash  TEXT
);

CREATE TABLE IF NOT EXISTS mod_deletion_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id          TEXT NOT NULL,
    deleter         TEXT NOT NULL,
    deleter_id      INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    original_title  TEXT,
    is_warning      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mod_startup_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    startup_time INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_suspicion (
    guild_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    score           INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    cleared         INTEGER NOT NULL DEFAULT 0,
    join_invite     TEXT,
    invite_source   TEXT,
    scored_at       TEXT NOT NULL,
    flagged_at      TEXT,
    cleared_at      TEXT,
    cleared_by      TEXT,
    note            TEXT,
    signals         TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (guild_id, user_id)
);
"""

def _migrate_logger_config():
    logger_cfg = script_dir() / "config" / "logger_config.json"
    if not logger_cfg.exists():
        return
    try:
        with open(logger_cfg, encoding="utf-8") as f:
            logger_data = json.load(f)
        mod_cfg = _load_config()
        mod_cfg.update(logger_data)
        _save_config(mod_cfg)
        logger_cfg.unlink()
    except Exception as e:
        print(f"[MODERATION] Failed to migrate logger_config.json: {e}", file=sys.stderr)

def _config_path() -> Path:
    p = script_dir() / "config"
    p.mkdir(parents=True, exist_ok=True)
    old = p / "moderation.json"
    new = p / "mod.json"
    if old.exists() and not new.exists():
        old.rename(new)
    elif new.exists() and old.exists():
        old.unlink()
    return new

def _load_config() -> dict:
    path = _config_path()
    defaults = {
        "owner_id": 0,
        "join_logs_channel_id": 0,
        "bot_logs_channel_id": 0,
        "log_message_edits": True,
        "log_message_deletes": True,
        "log_member_joins": True,
        "log_member_leaves": True,
        "log_bans": True,
        "log_unbans": True,
        "log_role_changes": True,
        "log_channel_changes": True,
        "log_voice_changes": True,
        "log_invite_changes": True,
        "log_nickname_changes": True,
        "rules_channel_name": "rules",
        "min_reason_length": 10,
        "muted_role_name": "Muted",
        "report_time_cst": "00:00",
        "context_message_count": 30,
        "invite_cleanup_days": 7,
        "elevated_roles": [],
        "rules": "",
        "strike_thresholds": {"warn": 3, "mute": 5, "kick": 7, "ban": 10},
        "invite_labels": {},
        "releases_role_name": "Emball Releases",
    }
    from _utils import migrate_config
    return migrate_config(path, defaults)

def _save_config(data: dict) -> None:
    from _utils import atomic_json_write
    atomic_json_write(_config_path(), data)

def _migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        needs_migration = bool(
            tables & {"mod_config", "mod_elevated_roles", "mod_word_lists"}
        )
        rules_file = script_dir() / "rules.json"
        has_rules_json = rules_file.exists()

        if not needs_migration and not has_rules_json:
            return

        cfg = _load_config()

        if "mod_config" in tables:
            for row in conn.execute("SELECT key, value FROM mod_config").fetchall():
                k, v = row["key"], row["value"]
                try:
                    if str(v) == str(int(v)):
                        v = int(v)
                except (ValueError, TypeError):
                    pass
                cfg[k] = v

        if "mod_elevated_roles" in tables:
            rows = conn.execute("SELECT role_name FROM mod_elevated_roles").fetchall()
            if rows:
                cfg["elevated_roles"] = [r["role_name"] for r in rows]

        if has_rules_json:
            try:
                with open(rules_file, "r", encoding="utf-8") as f:
                    rules_data = json.load(f)
                cfg["rules"] = rules_data
            except Exception as e:
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
        print("[MODERATION] Migration complete -> config/mod.json", file=sys.stderr)
    finally:
        conn.close()

def _init_db(db_path: str) -> None:
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

def _db_exec(db, query: str, params: tuple = ()):
    if isinstance(db, sqlite3.Connection):
        db.execute(query, params)
        db.commit()
        return
    c = _conn(db)
    try:
        c.execute(query, params)
        c.commit()
    finally:
        c.close()

def _db_one(db, query: str, params: tuple = ()):
    if isinstance(db, sqlite3.Connection):
        return db.execute(query, params).fetchone()
    c = _conn(db)
    try:
        return c.execute(query, params).fetchone()
    finally:
        c.close()

def _db_all(db, query: str, params: tuple = ()):
    if isinstance(db, sqlite3.Connection):
        return db.execute(query, params).fetchall()
    c = _conn(db)
    try:
        return c.execute(query, params).fetchall()
    finally:
        c.close()

SUSPICION_THRESHOLD = 6

SIGNAL_WEIGHTS: dict[str, int] = {
    "account_age_under_7d":      4,
    "account_age_under_30d":     2,
    "default_avatar":            2,
    "throwaway_username":        1,
    "joined_recently_under_7d":  1,
    "no_messages":               2,
    "only_releases_role":        2,
    "invite_leaktracker":        5,
    "invite_youtube":            1,
    "invite_unknown":            2,
}

_THROWAWAY_PATTERNS = [
    re.compile(r'^[a-z]{3,6}\d{4,}$'),
    re.compile(r'^\d{6,}$'),
    re.compile(r'^[a-z0-9]{20,}$'),
    re.compile(r'^(user|account|member)\d+$', re.I),
]

class ModConfig:

    def __init__(self):
        self._data: dict = _load_config()

    def reload(self) -> None:
        self._data = _load_config()

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
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
    def context_message_count(self) -> int:
        return self.get_int("context_message_count", 30)

    @property
    def invite_cleanup_days(self) -> int:
        return self.get_int("invite_cleanup_days", 7)

    def get_elevated_roles(self) -> List[str]:
        return list(self._data.get("elevated_roles", []))

    def get_rules(self) -> Optional[dict]:
        rules = self._data.get("rules")
        if rules and rules.get("rules"):
            return rules
        return None

    def save_rules(self, rules_data: dict) -> None:
        self._data["rules"] = rules_data
        _save_config(self._data)

def has_elevated_role(member: discord.Member, cfg: ModConfig) -> bool:
    if member.guild.owner_id == member.id:
        return True
    elevated = cfg.get_elevated_roles()
    return any(role.name in elevated for role in member.roles)

def has_owner_role(member: discord.Member, cfg: ModConfig) -> bool:
    if member.guild.owner_id == member.id:
        return True
    if member.id == cfg.owner_id:
        return True
    return any(role.name == "Owner" for role in member.roles)

def validate_reason(reason: Optional[str], min_len: int) -> tuple:
    if not reason or reason.strip() == "" or reason == "No reason provided":
        return False, ERROR_REASON_REQUIRED
    if len(reason) < min_len:
        return False, f"Reason must be at least {min_len} characters long."
    return True, None

def parse_duration(duration: str) -> tuple:
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

def _parse_fake_suffix(text: str) -> tuple:
    parts = text.rsplit(None, 1)
    if parts and parts[-1].lower() == "fake":
        return parts[0] if len(parts) > 1 else "", True
    return text, False

def get_event_logger(bot):
    return getattr(bot, '_logger_event_logger', None)

def _is_default_avatar(member: discord.Member) -> bool:
    url = str(member.display_avatar.url)
    return ("/embed/avatars/" in url) or ("/assets/" in url and "a_" not in url)

_cfg: Optional[ModConfig] = None

def is_owner(member: discord.Member) -> bool:
    return has_owner_role(member, _cfg)

class ModContext:
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


class ModerationSystem:

    def __init__(self, bot):
        self.bot     = bot
        self._db     = _db_path()
        _migrate(self._db)
        _init_db(self._db)
        _migrate_logger_config()
        self.cfg     = ModConfig()

        self._bot_log_cache: Dict[int, Dict] = {}
        self._bot_log_order: deque           = deque()
        self._bot_log_cache_size             = 500
        self._deletion_warnings: Dict[int, str] = {}
        self.tracked_embeds = {}

        _db_exec(self._db,
                 "INSERT INTO mod_startup_log (startup_time) VALUES (?)",
                 (int(_now().timestamp()),))

        self.check_expired_mutes.start()
        self.cleanup_invites.start()
        self.send_daily_report.start()
        self.resolve_expired_appeals.start()
        self.sync_config.start()

        bot.logger.log(MODULE_NAME, "Moderation system initialised (SQLite)")

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
            (gk, uk, role_ids, _now().isoformat(), str(member)),
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
            (str(user_id), _now().isoformat(), reason),
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
            expiry = (_now() + timedelta(seconds=duration_seconds)).isoformat()
        self._exec(
            "INSERT INTO mod_mutes (guild_id, user_id, reason, moderator, timestamp, "
            "duration_seconds, expiry_time) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "reason=excluded.reason, moderator=excluded.moderator, "
            "timestamp=excluded.timestamp, duration_seconds=excluded.duration_seconds, "
            "expiry_time=excluded.expiry_time",
            (str(guild_id), str(user_id), reason, str(moderator),
             _now().isoformat(), duration_seconds, expiry),
        )

    def remove_mute(self, guild_id, user_id):
        self._exec(
            "DELETE FROM mod_mutes WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )

    def get_expired_mutes(self) -> list:
        now  = _now().isoformat()
        rows = self._all(
            "SELECT guild_id, user_id FROM mod_mutes "
            "WHERE expiry_time IS NOT NULL AND expiry_time <= ?",
            (now,))
        return [{"guild_id": int(r["guild_id"]), "user_id": int(r["user_id"])} for r in rows]

    def get_context_messages(
        self, guild_id: int, channel_id: int,
        around_message_id: int, count: int = None
    ) -> List[Dict]:
        if count is None:
            count = self.cfg.context_message_count
        return msg_cache.get_context_messages(guild_id, channel_id, around_message_id, count)

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
            cutoff = (_now() - timedelta(days=cleanup_days)).isoformat()
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
            from mod_oversight import generate_daily_report
            await generate_daily_report(self)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send daily report", e)

    @send_daily_report.before_loop
    async def before_send_daily_report(self):
        await self.bot.wait_until_ready()
        cst    = pytz.timezone('America/Chicago')
        now    = datetime.now(cst)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)

    @tasks.loop(minutes=1)
    async def resolve_expired_appeals(self):
        try:
            from mod_appeals import resolve_expired_appeals_task
            await resolve_expired_appeals_task(self)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in appeal resolution task", e)

    @resolve_expired_appeals.before_loop
    async def before_resolve_expired_appeals(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def sync_config(self):
        try:
            self.cfg.reload()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Config sync error", e)

    @sync_config.before_loop
    async def before_sync_config(self):
        await self.bot.wait_until_ready()


def setup(bot):
    import mod_core as _self

    mod_system = ModerationSystem(bot)
    bot._mod_system = mod_system
    bot.moderation  = mod_system

    _self._cfg = mod_system.cfg

    from mod_appeals import BanAppealView, AppealVoteView
    from mod_oversight import ActionReviewView, action_row_to_dict
    from mod_actions import (
        _do_ban, _do_unban, _do_kick, _do_timeout, _do_untimeout,
        _do_mute, _do_unmute, _do_softban,
        _do_warn, _do_warnings, _do_clearwarnings,
        _do_purge, _do_slowmode, _do_lock, _do_unlock,
    )
    from mod_rules import RulesManager
    from mod_suspicion import _setup_suspicion

    bot.add_view(BanAppealView(guild_id=0))
    for appeal_row in mod_system._all("SELECT appeal_id FROM mod_appeals WHERE status='pending'"):
        bot.add_view(AppealVoteView(moderation_system=mod_system, appeal_id=appeal_row["appeal_id"]))
    for row in mod_system._all("SELECT * FROM mod_pending_actions WHERE status='pending'"):
        action = action_row_to_dict(row)
        bot.add_view(ActionReviewView(mod_system, action['id'], action))

    @bot.tree.command(name="ban", description="[Mod] Ban a user from the server")
    @app_commands.describe(
        user="User to ban",
        reason="Reason for the ban - not required if rule is provided",
        rule="Rule number violated",
        delete_days="Days of messages to delete (0-7)",
        fake="Simulate without executing",
    )
    async def slash_ban(interaction: discord.Interaction, user: discord.User,
                        reason: Optional[str] = None, rule: Optional[int] = None,
                        delete_days: Optional[int] = 0, fake: bool = False):
        if rule is None and not reason:
            await interaction.response.send_message(
                "You must provide either a reason or a rule number.", ephemeral=True)
            return
        await _do_ban(ModContext(interaction), mod_system, user, reason,
                      delete_days, fake=fake, rule_number=rule)

    @bot.tree.command(name="multiban", description="[Mod] Ban multiple users at once with one reason")
    @app_commands.describe(
        user_ids="Space-separated list of user IDs to ban",
        reason="Reason applied to all bans",
        delete_days="Days of messages to delete (0-7)",
        fake="Simulate without executing",
    )
    async def slash_multiban(interaction: discord.Interaction, user_ids: str,
                             reason: str, delete_days: Optional[int] = 0,
                             fake: bool = False):
        if not has_elevated_role(interaction.user, mod_system.cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        raw_ids = user_ids.split()
        if not raw_ids:
            await interaction.response.send_message(
                "Provide at least one user ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ban_ctx = ModContext(interaction)
        ban_ctx._replied = True
        skipped: list[str] = []
        banned_count = 0
        for raw in raw_ids:
            raw = raw.strip("<@!>")
            if not raw.isdigit():
                skipped.append(f"`{raw}` - not a valid ID, skipped")
                continue
            try:
                user = await interaction.client.fetch_user(int(raw))
            except discord.NotFound:
                skipped.append(f"`{raw}` - user not found, skipped")
                continue
            except Exception:
                skipped.append(f"`{raw}` - fetch failed, skipped")
                continue
            if banned_count > 0:
                await asyncio.sleep(10)
            try:
                await _do_ban(ban_ctx, mod_system, user, reason,
                              delete_days or 0, fake=fake, rule_number=None)
                banned_count += 1
            except Exception:
                skipped.append(f"**{user}** (`{user.id}`) - ban failed")
        summary_parts = [f"**Multiban complete** - {banned_count} banned"
                         + ("(dry run)" if fake else "")]
        if skipped:
            summary_parts.append("\n".join(skipped))
        await interaction.followup.send("\n".join(summary_parts), ephemeral=True)

    @bot.tree.command(name="unban", description="[Mod] Unban a user from the server")
    @app_commands.describe(user_id="User ID to unban", reason="Reason for unban",
                           fake="Simulate without executing")
    async def slash_unban(interaction: discord.Interaction, user_id: str,
                          reason: Optional[str] = "No reason provided", fake: bool = False):
        await _do_unban(ModContext(interaction), mod_system, user_id, reason, fake=fake)

    @bot.tree.command(name="kick", description="[Mod] Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    async def slash_kick(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_kick(ModContext(interaction), mod_system, member, reason, fake=fake)

    @bot.tree.command(name="timeout", description="[Mod] Timeout a member")
    @app_commands.describe(member="Member to timeout", duration="Duration in minutes",
                           reason="Reason (min 10 chars)", fake="Simulate without executing")
    async def slash_timeout(interaction: discord.Interaction, member: discord.Member,
                            duration: int, reason: str, fake: bool = False):
        await _do_timeout(ModContext(interaction), mod_system, member, duration, reason, fake=fake)

    @bot.tree.command(name="untimeout", description="[Mod] Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from",
                           fake="Simulate without executing")
    async def slash_untimeout(interaction: discord.Interaction, member: discord.Member,
                               fake: bool = False):
        await _do_untimeout(ModContext(interaction), mod_system, member, fake=fake)

    @bot.tree.command(name="mute", description="[Mod] Mute a member")
    @app_commands.describe(member="Member to mute", reason="Reason for mute",
                           duration="Duration e.g. 10m, 1h, 1d (empty = permanent)",
                           fake="Simulate without executing")
    async def slash_mute(interaction: discord.Interaction, member: discord.Member,
                         reason: str = "No reason provided",
                         duration: Optional[str] = None, fake: bool = False):
        await _do_mute(ModContext(interaction), mod_system, member, reason, duration, fake=fake)

    @bot.tree.command(name="unmute", description="[Mod] Unmute a member")
    @app_commands.describe(member="Member to unmute", fake="Simulate without executing")
    async def slash_unmute(interaction: discord.Interaction, member: discord.Member,
                            fake: bool = False):
        await _do_unmute(ModContext(interaction), mod_system, member, fake=fake)

    @bot.tree.command(name="softban",
                      description="[Mod] Softban a member (ban+unban to delete messages)")
    @app_commands.describe(member="Member to softban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 7)",
                           fake="Simulate without executing")
    async def slash_softban(interaction: discord.Interaction, member: discord.Member,
                            reason: str, delete_days: Optional[int] = 7,
                            fake: bool = False):
        await _do_softban(ModContext(interaction), mod_system, member, reason,
                          delete_days, fake=fake)

    @bot.tree.command(name="warn", description="[Mod] Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    async def slash_warn(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_warn(ModContext(interaction), mod_system, member, reason, fake=fake)

    @bot.tree.command(name="warnings", description="[Mod] View warnings for a member")
    @app_commands.describe(member="Member to check")
    async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
        await _do_warnings(ModContext(interaction), mod_system, member)

    @bot.tree.command(name="clearwarnings", description="[Owner only] Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    async def slash_clearwarnings(interaction: discord.Interaction, member: discord.Member):
        await _do_clearwarnings(ModContext(interaction), mod_system, member)

    @bot.tree.command(name="purge", description="[Mod] Bulk-delete messages in this channel")
    @app_commands.describe(amount="Number of messages to delete (1-100)",
                           user="Only delete messages from this user (optional)",
                           fake="Simulate without executing")
    async def slash_purge(interaction: discord.Interaction, amount: int,
                          user: Optional[discord.Member] = None, fake: bool = False):
        await _do_purge(ModContext(interaction), mod_system, amount, user, fake=fake)

    @bot.tree.command(name="slowmode", description="[Mod] Set slowmode delay for this channel")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)",
                           channel="Channel to apply to (default: current)")
    async def slash_slowmode(interaction: discord.Interaction, seconds: int,
                             channel: Optional[discord.TextChannel] = None):
        await _do_slowmode(ModContext(interaction), mod_system, seconds, channel)

    @bot.tree.command(name="lock", description="[Mod] Prevent members from sending messages")
    @app_commands.describe(reason="Reason for locking (min 10 chars)",
                           channel="Channel to lock (default: current)",
                           fake="Simulate without executing")
    async def slash_lock(interaction: discord.Interaction, reason: str,
                         channel: Optional[discord.TextChannel] = None, fake: bool = False):
        await _do_lock(ModContext(interaction), mod_system, reason, channel, fake=fake)

    @bot.tree.command(name="unlock", description="[Mod] Re-allow members to send messages")
    @app_commands.describe(channel="Channel to unlock (default: current)")
    async def slash_unlock(interaction: discord.Interaction,
                           channel: Optional[discord.TextChannel] = None):
        await _do_unlock(ModContext(interaction), mod_system, channel)

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
        await _do_ban(ModContext(ctx), mod_system, user, reason, delete_days,
                      fake=fake, rule_number=rule_number)

    @bot.command(name="multiban")
    async def prefix_multiban(ctx, *args):
        if not args:
            return await ctx.send(
                "Usage: `?multiban <id/@user> [id/@user ...] reason:<text> [days:<0-7>] [fake]`",
                delete_after=10)
        fake        = any(a.lower() == "fake" for a in args)
        args        = [a for a in args if a.lower() != "fake"]
        reason_match = re.search(r'reason:(.+)', "".join(args), re.IGNORECASE)
        if not reason_match:
            return await ctx.send(
                "You must include `reason:<text>` in the command.", delete_after=8)
        reason = reason_match.group(1).strip()
        days_match  = re.search(r'days:(\d+)', "".join(args), re.IGNORECASE)
        delete_days = int(days_match.group(1)) if days_match else 0
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
                skipped.append(f"`{raw}` - not a valid ID, skipped")
                continue
            try:
                user = await ctx.bot.fetch_user(int(raw))
            except discord.NotFound:
                skipped.append(f"`{raw}` - user not found, skipped")
                continue
            except Exception:
                skipped.append(f"`{raw}` - fetch failed, skipped")
                continue
            if banned_count > 0:
                await asyncio.sleep(10)
            try:
                await _do_ban(ModContext(ctx), mod_system, user, reason, delete_days, fake=fake)
                banned_count += 1
            except Exception:
                skipped.append(f"**{user}** (`{user.id}`) - ban failed")
        summary_parts = [f"**Multiban complete** - {banned_count} banned"
                         + ("(dry run)" if fake else "")]
        if skipped:
            summary_parts.append("\n".join(skipped))
        await ctx.send("\n".join(summary_parts))

    @bot.command(name="unban")
    async def prefix_unban(ctx, user_id: str = None, *, reason: str = "No reason provided"):
        if not user_id:
            return await ctx.send("Usage: `?unban <user_id> [reason]`", delete_after=8)
        reason, fake = _parse_fake_suffix(reason)
        await _do_unban(ModContext(ctx), mod_system, user_id, reason, fake=fake)

    @bot.command(name="kick")
    async def prefix_kick(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("Usage: `?kick @member <reason>`", delete_after=8)
        reason, fake = _parse_fake_suffix(reason)
        await _do_kick(ModContext(ctx), mod_system, member, reason, fake=fake)

    @bot.command(name="timeout")
    async def prefix_timeout(ctx, member: discord.Member = None,
                              duration: int = None, *, reason: str = ""):
        if not member or duration is None:
            return await ctx.send(
                "Usage: `?timeout @member <minutes> <reason>`", delete_after=8)
        reason, fake = _parse_fake_suffix(reason)
        await _do_timeout(ModContext(ctx), mod_system, member, duration, reason, fake=fake)

    @bot.command(name="untimeout")
    async def prefix_untimeout(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("Usage: `?untimeout @member`", delete_after=8)
        await _do_untimeout(ModContext(ctx), mod_system, member, fake=fake.lower() == "fake")

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
        reason, fake = _parse_fake_suffix(reason)
        await _do_mute(ModContext(ctx), mod_system, member, reason, duration, fake=fake)

    @bot.command(name="unmute")
    async def prefix_unmute(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("Usage: `?unmute @member`", delete_after=8)
        await _do_unmute(ModContext(ctx), mod_system, member, fake=fake.lower() == "fake")

    @bot.command(name="softban")
    async def prefix_softban(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("Usage: `?softban @member <reason>`", delete_after=8)
        reason, fake = _parse_fake_suffix(reason)
        await _do_softban(ModContext(ctx), mod_system, member, reason, fake=fake)

    @bot.command(name="warn")
    async def prefix_warn(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("Usage: `?warn @member <reason>`", delete_after=8)
        reason, fake = _parse_fake_suffix(reason)
        await _do_warn(ModContext(ctx), mod_system, member, reason, fake=fake)

    @bot.command(name="warnings")
    async def prefix_warnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("Usage: `?warnings @member`", delete_after=8)
        await _do_warnings(ModContext(ctx), mod_system, member)

    @bot.command(name="clearwarnings")
    async def prefix_clearwarnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("Usage: `?clearwarnings @member`", delete_after=8)
        await _do_clearwarnings(ModContext(ctx), mod_system, member)

    @bot.command(name="purge")
    async def prefix_purge(ctx, amount: int = None,
                            member: discord.Member = None, fake: str = ""):
        if amount is None:
            return await ctx.send("Usage: `?purge <amount> [@member]`", delete_after=8)
        await _do_purge(ModContext(ctx), mod_system, amount, member,
                        fake=fake.lower() == "fake")

    @bot.command(name="slowmode")
    async def prefix_slowmode(ctx, seconds: int = None,
                               channel: discord.TextChannel = None):
        if seconds is None:
            return await ctx.send(
                "Usage: `?slowmode <seconds> [#channel]`", delete_after=8)
        await _do_slowmode(ModContext(ctx), mod_system, seconds, channel)

    @bot.command(name="lock")
    async def prefix_lock(ctx, channel: Optional[discord.TextChannel] = None, *, reason: str = ""):
        reason, fake = _parse_fake_suffix(reason)
        await _do_lock(ModContext(ctx), mod_system, reason, channel, fake=fake)

    @bot.command(name="unlock")
    async def prefix_unlock(ctx, channel: discord.TextChannel = None):
        await _do_unlock(ModContext(ctx), mod_system, channel)

    @bot.tree.command(name="report",
                      description="[Owner only] Trigger the moderation report immediately")
    async def report_command(interaction: discord.Interaction):
        if interaction.user.id != mod_system.cfg.owner_id:
            await interaction.response.send_message(
                "This command is restricted to the bot owner.", ephemeral=True)
            return
        await interaction.response.send_message("Generating report...", ephemeral=True)
        try:
            from mod_oversight import generate_daily_report
            await generate_daily_report(mod_system)
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Manual report generation failed", e)

    @bot.listen()
    async def on_message(message):
        if message.author.bot or not message.guild:
            return
        await msg_cache.cache_message(message)

    @bot.listen()
    async def on_message_delete(message):
        from mod_oversight import embed_handle_deletion, handle_bot_log_deletion, bot_logs_channel
        await embed_handle_deletion(mod_system, message.id)

        if not message.guild:
            return

        bot_logs_ch = bot_logs_channel(mod_system, message.guild)
        if bot_logs_ch and message.channel.id == bot_logs_ch.id:
            if message.id in mod_system._bot_log_cache:
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
                    mod_system.bot.logger.log(
                        MODULE_NAME, f"Audit log fetch failed: {e}", "WARNING")

                if deleter and deleter.id == message.guild.me.id:
                    return

                if deleter is None or has_elevated_role(deleter, mod_system.cfg):
                    await handle_bot_log_deletion(
                        mod_system, message.id, deleter or message.guild.me, message.guild)
                elif message.id in mod_system._deletion_warnings:
                    original_log_id = mod_system._deletion_warnings.pop(message.id)
                    warning_embed = discord.Embed(
                        title="Bot-Log Deletion Warning - REPOSTED",
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
                        new_warn_msg = await bot_logs_ch.send(embed=warning_embed)
                        from mod_oversight import log_bot_register
                        log_bot_register(
                            mod_system, new_warn_msg.id, f"WARN-{original_log_id}",
                            warning_embed, is_warning=True,
                            warning_for_log_id=original_log_id)
                        mod_system._deletion_warnings[new_warn_msg.id] = original_log_id
                    except Exception as e:
                        mod_system.bot.logger.error(
                            MODULE_NAME, f"Failed to repost deletion warning: {e}")
            return

        if not message.author.bot and message.id in msg_cache.media_cache:
            guild_id   = str(message.guild.id)
            channel_id = str(message.channel.id)
            cached     = msg_cache.media_cache.get(message.id)
            rehosted   = []
            if cached:
                for f in cached['files']:
                    try:
                        data = msg_cache.decrypt(f['data'])
                        rehosted.append({'filename': f['filename'], 'data': data})
                    except Exception as e:
                        mod_system.bot.logger.log(
                            MODULE_NAME,
                            f"Failed to decrypt {f['filename']} for deletion log: {e}",
                            "WARNING")
            if not hasattr(bot, '_pending_rehosted_media'):
                bot._pending_rehosted_media = {}
            bot._pending_rehosted_media[message.id] = rehosted
            msg_cache.delete_media(message.id)
            channel_msgs = msg_cache.message_cache.get(guild_id, {}).get(channel_id, [])
            msg_cache.message_cache[guild_id][channel_id] = [
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

        channel_msgs = msg_cache.message_cache.get(guild_id, {}).get(channel_id, [])
        for msg in channel_msgs:
            if msg['id'] == after.id:
                msg['attachments'] = [att.url for att in after.attachments]
                break

        cached        = msg_cache.media_cache.get(after.id)
        removed_files = []
        if cached:
            removed_filenames = {
                att.filename for att in before.attachments if att.id in removed_ids}
            kept = []
            for f in cached['files']:
                if f['filename'] in removed_filenames:
                    try:
                        data = msg_cache.decrypt(f['data'])
                        removed_files.append({'filename': f['filename'], 'data': data})
                    except Exception as e:
                        mod_system.bot.logger.log(
                            MODULE_NAME,
                            f"Failed to decrypt removed attachment {f['filename']}: {e}",
                            "WARNING")
                else:
                    kept.append(f)
            if kept:
                msg_cache.media_cache[after.id]['files'] = kept
            else:
                msg_cache.media_cache.pop(after.id, None)

        image_exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        audio_exts = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus',
                      '.mp4', '.mov', '.webm')

        bot_logs_ch = bot_logs_channel(mod_system, after.guild)

        if not removed_files:
            if bot_logs_ch:
                embed = discord.Embed(
                    title="Attachment Removed from Message",
                    color=discord.Color.yellow(),
                    timestamp=_now())
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
                from mod_oversight import send_bot_log
                await send_bot_log(mod_system, after.guild, embed)
            return

        image_files = [f for f in removed_files
                       if f['filename'].lower().endswith(image_exts)]
        other_files  = [f for f in removed_files
                        if not f['filename'].lower().endswith(image_exts)]

        embed = discord.Embed(color=discord.Color.yellow(), timestamp=_now())
        embed.set_author(name=str(after.author),
                         icon_url=after.author.display_avatar.url)
        description = (f"**{after.author.mention} removed an attachment "
                       f"in {after.channel.mention}**")
        if after.content:
            description += f"\n{after.content}"
        embed.description = description
        embed.set_footer(text=f"Author: {after.author.id} | Message ID: {after.id}")

        if image_files and not other_files:
            embed.set_image(url=f"attachment://{image_files[0]['filename']}")
            from mod_oversight import send_bot_log
            await send_bot_log(mod_system, after.guild, embed, files_data=removed_files)
        elif other_files:
            has_audio = any(f['filename'].lower().endswith(audio_exts) for f in other_files)
            label     = "audio" if has_audio else "file"
            embed.add_field(name="Attachment",
                            value=f"*{label} hosted above*", inline=False)
            if bot_logs_ch:
                discord_files = [
                    discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                    for f in removed_files
                ]
                await bot_logs_ch.send(files=discord_files)
            from mod_oversight import send_bot_log
            await send_bot_log(mod_system, after.guild, embed)
        else:
            from mod_oversight import send_bot_log
            await send_bot_log(mod_system, after.guild, embed, files_data=removed_files)

    @bot.listen()
    async def on_member_remove(member):
        mod_system.save_member_roles(member)

    @bot.listen()
    async def on_member_join(member):
        await mod_system.restore_member_roles(member)

    rules_manager    = RulesManager(bot, mod_system._db, mod_system.cfg)
    bot.rules_manager = rules_manager

    @bot.listen("on_ready")
    async def _rules_on_ready():
        for guild in bot.guilds:
            await rules_manager.on_ready(guild)
            rules_manager.start_watcher(guild)
        bot.logger.log(MODULE_NAME, "Rules manager synced on ready")

    if bot.is_ready():
        async def _rules_late_start():
            for guild in bot.guilds:
                await rules_manager.on_ready(guild)
                rules_manager.start_watcher(guild)
            bot.logger.log(MODULE_NAME, "Rules manager synced (late start)")
        asyncio.ensure_future(_rules_late_start())

    @bot.tree.command(name="rules", description="List all server rules")
    async def slash_rules(interaction: discord.Interaction):
        data = rules_manager.load_rules()
        if not data:
            await interaction.response.send_message(
                "Rules file not found or could not be loaded.", ephemeral=True)
            return
        layout = rules_manager._build_layout(data)
        await interaction.response.send_message(view=layout, ephemeral=True)

    @bot.tree.command(name="updaterules",
                      description="[Owner only] Force-refresh the #rules channel embed")
    async def slash_updaterules(interaction: discord.Interaction):
        if not has_owner_role(interaction.user, mod_system.cfg):
            await interaction.response.send_message(ERROR_NO_PERMISSION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        posted = await rules_manager.sync(interaction.guild, force=True)
        if posted:
            await interaction.followup.send(
                "Rules embed has been refreshed in the rules channel.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Rules embed is already up to date.", ephemeral=True)

    _setup_suspicion(bot, mod_system, mod_system.cfg)

    # media cache TTL cleanup
    @tasks.loop(minutes=15)
    async def _cleanup_media_cache():
        try:
            evicted = msg_cache.evict_media_ttl()
            if evicted:
                bot.logger.log(MODULE_NAME, f"media_cache TTL eviction: removed {evicted} stale entry/entries")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "media_cache cleanup error", e)

    @_cleanup_media_cache.before_loop
    async def _before_cleanup():
        await bot.wait_until_ready()

    _cleanup_media_cache.start()

    # automod cache for VM transcript scanning
    _automod_keywords: list = []
    _automod_cache_time: float = 0.0

    async def _get_automod_keywords(guild: discord.Guild) -> list:
        nonlocal _automod_keywords, _automod_cache_time
        if time.time() - _automod_cache_time < 600 and _automod_keywords:
            return _automod_keywords
        try:
            rules = await guild.fetch_automod_rules()
            keywords = []
            for rule in rules:
                if not rule.enabled:
                    continue
                meta = rule.trigger
                if meta and meta.keyword_filter:
                    keywords.extend(meta.keyword_filter)
            _automod_keywords = keywords
            _automod_cache_time = time.time()
            bot.logger.log(MODULE_NAME, f"Automod keyword cache refreshed ({len(keywords)} keywords)")
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Failed to fetch automod rules: {e}", "WARNING")
        return _automod_keywords

    def _transcript_violates(transcript: str, keywords: list):
        if not transcript or not keywords:
            return None
        normalized = transcript.lower()
        for kw in keywords:
            kw = kw.strip().lower()
            if not kw:
                continue
            parts = kw.split('*')
            pattern = r'\w*'.join(re.escape(p) for p in parts)
            prefix = r'\b' if re.match(r'\w', pattern) else ''
            suffix = r'\b' if re.search(r'\w$', pattern) else ''
            try:
                if re.search(f'{prefix}{pattern}{suffix}', normalized):
                    return kw
            except re.error:
                continue
        return None

    @bot.listen()
    async def on_vm_transcribed(vm_id, transcript, vm_message, reply_message, guild):
        try:
            if not transcript or not guild:
                return
            keywords = await _get_automod_keywords(guild)
            matched = _transcript_violates(transcript, keywords)
            if not matched:
                return
            bot.logger.log(MODULE_NAME,
                f"VM #{vm_id} flagged by automod (matched: {matched!r}) - purging", "WARNING")
            # delete transcript reply
            if reply_message:
                try:
                    await reply_message.delete()
                except Exception:
                    pass
            # delete original VM message (suppress logger boilerplate)
            if vm_message:
                suppressed = set(getattr(bot, '_automod_purged_ids', ()))
                suppressed.add(vm_message.id)
                bot._automod_purged_ids = suppressed
                try:
                    await vm_message.delete()
                except Exception:
                    pass
            # purge from DB and disk via VMS manager if available
            vms = getattr(bot, 'vms_manager', None)
            if vms:
                try:
                    row = vms._db_one("SELECT filename FROM vms WHERE id=?", (vm_id,))
                    if row:
                        fp = vms._resolve_path(row[0])
                        if fp and fp.exists():
                            fp.unlink()
                    vms._db_exec("DELETE FROM vms_playback WHERE vm_id=?", (vm_id,))
                    vms._db_exec("DELETE FROM vms WHERE id=?", (vm_id,))
                except Exception as e:
                    bot.logger.log(MODULE_NAME, f"VM #{vm_id} DB/file purge error: {e}", "WARNING")
            # send automod embed to bot-logs
            if vm_message and vm_message.author:
                from mod_oversight import bot_logs_channel
                ch = bot_logs_channel(bot._mod_system, guild)
                if ch:
                    embed = discord.Embed(title="Auto-mod blocked a voice message", color=0xED4245)
                    embed.add_field(name="rule_name", value="Block Banned Words", inline=False)
                    embed.add_field(name="channel_id", value=str(vm_message.channel.id), inline=True)
                    embed.add_field(name="keyword", value=matched, inline=True)
                    embed.add_field(name="keyword_matched_content", value=transcript[:256], inline=False)
                    embed.add_field(name="flagged_message_id", value=str(vm_message.id), inline=True)
                    embed.add_field(name="author", value=f"{vm_message.author.name} ({vm_message.author.id})", inline=True)
                    embed.add_field(name="decision_outcome", value="blocked", inline=True)
                    embed.set_footer(text=f"VM #{vm_id}")
                    try:
                        await ch.send(embed=embed)
                    except Exception:
                        pass
        except Exception as e:
            bot.logger.error(MODULE_NAME, "on_vm_transcribed handler error", e)

    bot.logger.log(MODULE_NAME, "Moderation setup complete")
