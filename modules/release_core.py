import asyncio
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from _utils import script_dir, migrate_config, _now

MODULE_NAME = "RELEASE"

CONFIG_PATH = script_dir() / "config" / "release.json"
DB_PATH = script_dir() / "db" / "release.db"

CONFIG_DEFAULTS = {
    "releases_channel_name": "emball-remasters",
    "cache_channel_name": "release-cache",
}

TYPES = ["Remaster", "Edit", "Remaster & Edit"]
SPONSOR_KINDS = ["Sponsored by", "Paid request by"]
VOTE_EMOJIS = ["🔥", "😐", "🗑️"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    title_key  TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id              TEXT PRIMARY KEY,
    release_id      TEXT NOT NULL REFERENCES releases(id),
    version         TEXT NOT NULL,
    type            TEXT NOT NULL,
    note            TEXT,
    changelog       TEXT,
    sponsor_id      INTEGER,
    sponsor_kind    TEXT,
    file_name       TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    file_ch_id      INTEGER NOT NULL,
    file_msg_id     INTEGER NOT NULL,
    orig_name       TEXT,
    orig_size       INTEGER,
    orig_ch_id      INTEGER,
    orig_msg_id     INTEGER,
    post_ch_id      INTEGER,
    post_msg_id     INTEGER,
    thread_id       INTEGER,
    voting          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE(release_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ver_release ON versions(release_id);
CREATE INDEX IF NOT EXISTS idx_ver_post    ON versions(post_msg_id);

CREATE TABLE IF NOT EXISTS votes (
    version_id TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    emoji      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (version_id, user_id)
);
"""


def load_config() -> dict:
    return migrate_config(CONFIG_PATH, CONFIG_DEFAULTS)


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def new_id() -> str:
    return uuid.uuid4().hex[:10]


class ReleaseDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def find_release(self, title: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM releases WHERE title_key=?",
                             (title_key(title),)).fetchone()

    def versions_for(self, release_id: str) -> list:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM versions WHERE release_id=? ORDER BY created_at",
                (release_id,)).fetchall()

    def next_version_label(self, title: str) -> str:
        rel = self.find_release(title)
        return f"V{len(self.versions_for(rel['id'])) + 1}" if rel else "V1"

    def version_exists(self, title: str, version: str) -> bool:
        rel = self.find_release(title)
        if not rel:
            return False
        return any(v["version"].lower() == version.lower()
                   for v in self.versions_for(rel["id"]))

    def add_version(self, title: str, row: dict) -> str:
        now = _now().isoformat()
        with self._conn() as c:
            rel = c.execute("SELECT id FROM releases WHERE title_key=?",
                            (title_key(title),)).fetchone()
            if rel:
                rid = rel["id"]
            else:
                rid = new_id()
                c.execute("INSERT INTO releases (id, title, title_key, created_at) VALUES (?,?,?,?)",
                          (rid, title, title_key(title), now))
            row = {**row, "release_id": rid, "created_at": now}
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            c.execute(f"INSERT INTO versions ({cols}) VALUES ({marks})", tuple(row.values()))
        return rid

    def update_version(self, vid: str, **kw) -> None:
        sets = ", ".join(f"{k}=?" for k in kw)
        with self._conn() as c:
            c.execute(f"UPDATE versions SET {sets} WHERE id=?", (*kw.values(), vid))

    def get_version(self, vid: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT v.*, r.title FROM versions v JOIN releases r ON r.id=v.release_id "
                "WHERE v.id=?", (vid,)).fetchone()

    def version_by_post(self, msg_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM versions WHERE post_msg_id=?", (msg_id,)).fetchone()

    def delete_version(self, vid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM votes WHERE version_id=?", (vid,))
            c.execute("DELETE FROM versions WHERE id=?", (vid,))

    def upsert_vote(self, vid: str, user_id: int, emoji: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            old = c.execute("SELECT * FROM votes WHERE version_id=? AND user_id=?",
                            (vid, user_id)).fetchone()
            c.execute(
                "INSERT INTO votes (version_id, user_id, emoji, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(version_id, user_id) DO UPDATE SET emoji=excluded.emoji, "
                "created_at=excluded.created_at",
                (vid, user_id, emoji, _now().isoformat()))
        return old

    def remove_vote(self, vid: str, user_id: int, emoji: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM votes WHERE version_id=? AND user_id=? AND emoji=?",
                            (vid, user_id, emoji))
            return cur.rowcount > 0

    def tally(self, vid: str) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT emoji, COUNT(*) n FROM votes WHERE version_id=? GROUP BY emoji",
                             (vid,)).fetchall()
        return {r["emoji"]: r["n"] for r in rows}
