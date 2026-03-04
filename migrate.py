#!/usr/bin/env python3
"""
migrate.py — One-shot migration from moderation.py's old JSON files → moderation.db

Run this ONCE before switching to the new moderation.py:

    python migrate.py

What it migrates
────────────────
  data/member_roles.json          → mod_member_roles
  data/moderation_strikes.json    → mod_strikes
  data/muted_users.json           → mod_mutes
  data/mod_oversight_data.json    → mod_pending_actions
  data/ban_appeals.json           → mod_appeals
  data/ban_reversal_invites.json  → mod_invites
  data/rules_state.json           → mod_rules_state  (single guild)

Config / word-list defaults are seeded automatically by the new moderation.py on
first startup — this script does NOT migrate them because there were no editable
JSON files for them in the old version (they were hardcoded constants).

If a JSON file is missing the script simply skips it and moves on, so it is safe
to run even on a fresh install.

All operations are wrapped in a single SQLite transaction per table; if anything
fails mid-way you can fix the problem and re-run — INSERT OR IGNORE / INSERT OR
REPLACE semantics prevent duplicate rows.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Locate data directory ──────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.absolute()
DATA_DIR   = SCRIPT_DIR / "data"
DB_PATH    = DATA_DIR / "moderation.db"

# ── Colours for terminal output ───────────────────────────────────────────────
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

def ok(msg):   print(f"  {_GREEN}✓{_RESET}  {msg}")
def warn(msg): print(f"  {_YELLOW}⚠{_RESET}  {msg}")
def err(msg):  print(f"  {_RED}✗{_RESET}  {msg}")
def hdr(msg):  print(f"\n{_BOLD}{msg}{_RESET}")


# ── Schema (copied verbatim from new moderation.py) ───────────────────────────
DB_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS mod_config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_elevated_roles (
    role_name   TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS mod_word_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    term        TEXT NOT NULL,
    UNIQUE(category, term)
);

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
"""

# ── Default seeds (same as new moderation.py) ─────────────────────────────────
_DEFAULT_CONFIG = {
    "owner_id":              "1328822521084117033",
    "join_logs_channel_id":  "1229868495307669608",
    "bot_logs_channel_id":   "1229871835978666115",
    "rules_channel_name":    "rules",
    "min_reason_length":     "10",
    "muted_role_name":       "Muted",
    "report_time_cst":       "00:00",
    "context_message_count": "30",
    "invite_cleanup_days":   "7",
}

_DEFAULT_ELEVATED_ROLES = ["Moderator", "Admin", "Owner"]

_DEFAULT_WORD_LISTS = {
    "child_safety": ["child porn", "Teen leaks"],
    "racial_slurs": [
        "chink", "beaner", "n i g g e r", "nigger", "nigger'", "Nigger",
        "niggers", "niiger", "niigger",
    ],
    "tos_violations": [],
    "banned_words": [
        "embis", "embis'", "Embis", "embis!", "Embis!", "embis's", "embiss", "embiz",
        "https://www.youtube.com/watch?v=fXvOrWWB3Vg",
        "https://youtu.be/fXvOrWWB3Vg",
        "https://youtu.be/fXvOrWWB3Vg?si=rSS11Yf2si_MVauu",
        "leaked porn", "nudes leak",
        "mbis", "m'bis", "Mbis", "mbs", "mebis",
        "Michael Blake Sinclair", "Michael Sinclair",
        "montear",
        "www.youtube.com/watch?v=fXvOrWWB3Vg",
        "youtube.com/watch?v=fXvOrWWB3Vg",
    ],
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        warn(f"Could not read {path.name}: {e} — skipping")
        return None


def _isofix(val):
    """Return val as-is if it looks like an ISO timestamp, else now()."""
    if not val:
        return datetime.utcnow().isoformat()
    try:
        datetime.fromisoformat(str(val))
        return str(val)
    except ValueError:
        return datetime.utcnow().isoformat()


# ── Migration functions ────────────────────────────────────────────────────────

def migrate_member_roles(conn: sqlite3.Connection):
    hdr("member_roles.json  →  mod_member_roles")
    path = DATA_DIR / "member_roles.json"
    data = load_json(path, None)
    if data is None:
        warn("member_roles.json not found — skipping")
        return

    rows    = []
    skipped = 0
    # Format: { guild_id: { user_id: { role_ids, saved_at, username } } }
    for guild_id, users in data.items():
        if not isinstance(users, dict):
            skipped += 1
            continue
        for user_id, rec in users.items():
            if not isinstance(rec, dict):
                skipped += 1
                continue
            role_ids = json.dumps(rec.get("role_ids", []))
            saved_at = _isofix(rec.get("saved_at"))
            username = rec.get("username", "")
            rows.append((guild_id, user_id, role_ids, saved_at, username))

    conn.executemany(
        "INSERT OR REPLACE INTO mod_member_roles "
        "(guild_id, user_id, role_ids, saved_at, username) VALUES (?,?,?,?,?)",
        rows,
    )
    ok(f"Migrated {len(rows)} member role record(s)" +
       (f" ({skipped} skipped — bad format)" if skipped else ""))


def migrate_strikes(conn: sqlite3.Connection):
    hdr("moderation_strikes.json  →  mod_strikes")
    path = DATA_DIR / "moderation_strikes.json"
    data = load_json(path, None)
    if data is None:
        warn("moderation_strikes.json not found — skipping")
        return

    rows    = []
    skipped = 0
    # Format: { user_id: [ { timestamp, reason }, ... ] }
    for user_id, strike_list in data.items():
        if not isinstance(strike_list, list):
            skipped += 1
            continue
        for strike in strike_list:
            if not isinstance(strike, dict):
                skipped += 1
                continue
            rows.append((
                str(user_id),
                _isofix(strike.get("timestamp")),
                strike.get("reason", ""),
            ))

    conn.executemany(
        "INSERT INTO mod_strikes (user_id, timestamp, reason) VALUES (?,?,?)",
        rows,
    )
    ok(f"Migrated {len(rows)} strike record(s)" +
       (f" ({skipped} skipped)" if skipped else ""))


def migrate_mutes(conn: sqlite3.Connection):
    hdr("muted_users.json  →  mod_mutes")
    path = DATA_DIR / "muted_users.json"
    data = load_json(path, None)
    if data is None:
        warn("muted_users.json not found — skipping")
        return

    rows    = []
    skipped = 0
    # Format: { guild_id: { user_id: { reason, moderator, timestamp,
    #                                   duration_seconds, expiry_time } } }
    for guild_id, users in data.items():
        if not isinstance(users, dict):
            skipped += 1
            continue
        for user_id, rec in users.items():
            if not isinstance(rec, dict):
                skipped += 1
                continue
            rows.append((
                str(guild_id),
                str(rec.get("user_id", user_id)),
                rec.get("reason", ""),
                str(rec.get("moderator", "")),
                _isofix(rec.get("timestamp")),
                rec.get("duration_seconds"),
                rec.get("expiry_time"),
            ))

    conn.executemany(
        "INSERT OR REPLACE INTO mod_mutes "
        "(guild_id, user_id, reason, moderator, timestamp, duration_seconds, expiry_time) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    ok(f"Migrated {len(rows)} mute record(s)" +
       (f" ({skipped} skipped)" if skipped else ""))


def migrate_pending_actions(conn: sqlite3.Connection):
    hdr("mod_oversight_data.json  →  mod_pending_actions")
    path = DATA_DIR / "mod_oversight_data.json"
    data = load_json(path, None)
    if data is None:
        warn("mod_oversight_data.json not found — skipping")
        return

    rows    = []
    skipped = 0
    # Format: { action_id: { id, action, moderator_id, moderator, user_id, user,
    #                         reason, guild_id, channel_id, message_id, timestamp,
    #                         context_messages, duration, additional, flags,
    #                         embed_ids: {inchat, botlog}, status } }
    for action_id, rec in data.items():
        if not isinstance(rec, dict):
            skipped += 1
            continue

        embed_ids      = rec.get("embed_ids") or {}
        embed_inchat   = embed_ids.get("inchat")
        embed_botlog   = embed_ids.get("botlog")
        flags          = rec.get("flags", [])
        context_msgs   = rec.get("context_messages", [])
        additional     = rec.get("additional", {})

        # guild_id is required — skip if missing
        guild_id = rec.get("guild_id")
        if guild_id is None:
            skipped += 1
            continue

        rows.append((
            action_id,
            rec.get("action", "unknown"),
            int(rec.get("moderator_id") or 0),
            str(rec.get("moderator", "")),
            rec.get("user_id"),
            rec.get("user"),
            rec.get("reason", ""),
            int(guild_id),
            rec.get("channel_id"),
            rec.get("message_id"),
            _isofix(rec.get("timestamp")),
            json.dumps(context_msgs),
            rec.get("duration"),
            json.dumps(additional),
            json.dumps(flags),
            embed_inchat,
            embed_botlog,
            rec.get("status", "pending"),
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO mod_pending_actions "
        "(action_id, action, moderator_id, moderator, user_id, user_name, reason, "
        " guild_id, channel_id, message_id, timestamp, context_messages, duration, "
        " additional, flags, embed_id_inchat, embed_id_botlog, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    ok(f"Migrated {len(rows)} pending action(s)" +
       (f" ({skipped} skipped)" if skipped else ""))


def migrate_appeals(conn: sqlite3.Connection):
    hdr("ban_appeals.json  →  mod_appeals")
    path = DATA_DIR / "ban_appeals.json"
    data = load_json(path, None)
    if data is None:
        warn("ban_appeals.json not found — skipping")
        return

    rows    = []
    skipped = 0
    # Format: { appeal_id: { id, user_id, guild_id, appeal_text, submitted_at,
    #                         deadline, status, votes_for, votes_against,
    #                         channel_message_id } }
    for appeal_id, rec in data.items():
        if not isinstance(rec, dict):
            skipped += 1
            continue

        user_id  = rec.get("user_id")
        guild_id = rec.get("guild_id")
        if user_id is None or guild_id is None:
            skipped += 1
            continue

        rows.append((
            appeal_id,
            int(user_id),
            int(guild_id),
            rec.get("appeal_text", ""),
            _isofix(rec.get("submitted_at")),
            _isofix(rec.get("deadline")),
            rec.get("status", "pending"),
            json.dumps(rec.get("votes_for", [])),
            json.dumps(rec.get("votes_against", [])),
            rec.get("channel_message_id"),
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO mod_appeals "
        "(appeal_id, user_id, guild_id, appeal_text, submitted_at, deadline, "
        " status, votes_for, votes_against, channel_message_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    ok(f"Migrated {len(rows)} appeal(s)" +
       (f" ({skipped} skipped)" if skipped else ""))


def migrate_invites(conn: sqlite3.Connection):
    hdr("ban_reversal_invites.json  →  mod_invites")
    path = DATA_DIR / "ban_reversal_invites.json"
    data = load_json(path, None)
    if data is None:
        warn("ban_reversal_invites.json not found — skipping")
        return

    rows    = []
    skipped = 0
    # Format: { "{guild_id}_{user_id}": { code, user_id, guild_id, created_at } }
    for key, rec in data.items():
        if not isinstance(rec, dict):
            skipped += 1
            continue
        user_id  = rec.get("user_id")
        guild_id = rec.get("guild_id")
        code     = rec.get("code", "")
        if user_id is None or guild_id is None or not code:
            skipped += 1
            continue
        rows.append((
            str(key),
            code,
            int(user_id),
            int(guild_id),
            _isofix(rec.get("created_at")),
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO mod_invites "
        "(invite_key, code, user_id, guild_id, created_at) VALUES (?,?,?,?,?)",
        rows,
    )
    ok(f"Migrated {len(rows)} invite(s)" +
       (f" ({skipped} skipped)" if skipped else ""))


def migrate_rules_state(conn: sqlite3.Connection):
    hdr("data/rules_state.json  →  mod_rules_state")
    # Old path: data/rules_state.json
    path = DATA_DIR / "rules_state.json"
    data = load_json(path, None)
    if data is None:
        warn("rules_state.json not found — skipping")
        return

    # Old format stored a single guild's state (no guild_id key).
    # We can't know which guild it belonged to, so we use a placeholder.
    # The RulesManager will re-sync on next on_ready and replace this.
    message_id  = data.get("message_id")
    rules_hash  = data.get("rules_hash", "")
    guild_id    = data.get("guild_id", "UNKNOWN")   # placeholder

    conn.execute(
        "INSERT OR REPLACE INTO mod_rules_state (guild_id, message_id, rules_hash) "
        "VALUES (?,?,?)",
        (str(guild_id), message_id, rules_hash),
    )
    ok(f"Migrated rules state (message_id={message_id}, "
       f"hash={rules_hash[:12] if rules_hash else 'none'}…)")
    if guild_id == "UNKNOWN":
        warn("guild_id unknown — RulesManager will re-sync this on next startup "
             "and update the row with the real guild_id")


def seed_defaults(conn: sqlite3.Connection):
    hdr("Seeding default config / elevated roles / word lists")

    conn.executemany(
        "INSERT OR IGNORE INTO mod_config (key, value) VALUES (?, ?)",
        list(_DEFAULT_CONFIG.items()),
    )
    ok(f"Config: {len(_DEFAULT_CONFIG)} key(s) seeded (existing values preserved)")

    conn.executemany(
        "INSERT OR IGNORE INTO mod_elevated_roles (role_name) VALUES (?)",
        [(r,) for r in _DEFAULT_ELEVATED_ROLES],
    )
    ok(f"Elevated roles: {len(_DEFAULT_ELEVATED_ROLES)} seeded")

    total = 0
    for category, terms in _DEFAULT_WORD_LISTS.items():
        conn.executemany(
            "INSERT OR IGNORE INTO mod_word_lists (category, term) VALUES (?,?)",
            [(category, t) for t in terms],
        )
        total += len(terms)
    ok(f"Word lists: {total} term(s) seeded across "
       f"{len(_DEFAULT_WORD_LISTS)} categories")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  {_BOLD}Moderation DB Migration{_RESET}")
    print(f"  Source: {DATA_DIR}")
    print(f"  Target: {DB_PATH}")
    print(f"{'='*60}")

    # Ensure data directory exists
    DATA_DIR.mkdir(exist_ok=True)

    # Connect and create schema
    hdr("Initialising database schema")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    conn.commit()
    ok(f"Schema ready at {DB_PATH}")

    # Run all migrations inside one big transaction so a failure rolls back cleanly
    try:
        seed_defaults(conn)
        migrate_member_roles(conn)
        migrate_strikes(conn)
        migrate_mutes(conn)
        migrate_pending_actions(conn)
        migrate_appeals(conn)
        migrate_invites(conn)
        migrate_rules_state(conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        err(f"Migration failed — all changes rolled back.\n  {e}")
        import traceback; traceback.print_exc()
        conn.close()
        sys.exit(1)

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  {_GREEN}{_BOLD}Migration complete.{_RESET}")
    print(f"{'='*60}")
    print("""
  Next steps
  ──────────
  1. Replace the old moderation.py with the new one.
  2. Start the bot as normal — it will use moderation.db automatically.
  3. The old JSON files are left untouched in data/ as a backup.
     Once you're happy everything works you can delete them:

       data/member_roles.json
       data/moderation_strikes.json
       data/muted_users.json
       data/mod_oversight_data.json
       data/ban_appeals.json
       data/ban_reversal_invites.json
       data/rules_state.json
""")


if __name__ == "__main__":
    main()
