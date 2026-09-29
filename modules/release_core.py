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


class ReleaseError(Exception):
    pass


class Draft:
    def __init__(self, *, title, version, type_, ping, thread, voting, file,
                 original=None, sponsor=None, sponsor_kind=SPONSOR_KINDS[0], note=None):
        self.title = title.strip()
        self.version = version.strip()
        self.type = type_
        self.ping = ping
        self.thread = thread
        self.voting = voting
        self.file = file
        self.original = original
        self.sponsor = sponsor
        self.sponsor_kind = sponsor_kind
        self.note = (note or "").strip()
        self.changelog = ""


def changelog_bullets(raw: str) -> str:
    lines = [re.sub(r"^\s*[-•*]\s*", "", ln).strip() for ln in (raw or "").splitlines()]
    return "\n".join(f"- {ln}" for ln in lines if ln)


def post_items(d: Draft, vid: Optional[str] = None, role_mention: Optional[str] = None) -> list:
    items = []
    if role_mention:
        items.append(discord.ui.TextDisplay(role_mention))

    head = [f"## {d.title}", f"**{d.type}** · **{d.version}**"]
    if d.sponsor:
        head.append(f"-# {d.sponsor_kind} {d.sponsor.mention}")
    if d.note:
        head.append(f"\n{d.note}")
    bullets = changelog_bullets(d.changelog)
    if bullets:
        head.append(f"\n{bullets}")

    children = [discord.ui.TextDisplay("\n".join(head))]
    if vid:
        buttons = [discord.ui.Button(style=discord.ButtonStyle.primary, label="Download",
                                     emoji="⬇️", custom_id=f"rel:dl:{vid}")]
        if d.original:
            buttons.append(discord.ui.Button(style=discord.ButtonStyle.secondary,
                                             label="Original", custom_id=f"rel:orig:{vid}"))
        children += [discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                     discord.ui.ActionRow(*buttons)]
    hints = []
    if d.voting:
        hints.append("Vote with " + " ".join(VOTE_EMOJIS))
    if d.thread:
        hints.append("feedback goes in the thread")
    if hints:
        children += [discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=False),
                     discord.ui.TextDisplay("-# " + " · ".join(hints))]
    items.append(discord.ui.Container(*children, accent_color=0x1a1a2e))
    return items


def _mod_cfg(bot):
    ms = getattr(bot, "_mod_system", None)
    return ms.cfg if ms else None


def _role_name(bot) -> str:
    cfg = _mod_cfg(bot)
    return cfg.get("releases_role_name", "Emball Releases") if cfg else "Emball Releases"


async def _stash(bot, channel, att: discord.Attachment, label: str, limit: int) -> discord.Message:
    if att.size > limit:
        raise ReleaseError(
            f"`{att.filename}` is {att.size / 1048576:.1f} MB; the server upload limit is "
            f"{limit / 1048576:.0f} MB.")
    tmp_dir = script_dir() / "temp" / "release"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{new_id()}_{Path(att.filename).name}"
    try:
        await att.save(tmp)
        msg = await channel.send(content=label, file=discord.File(tmp, filename=att.filename))
        bot.logger.log(MODULE_NAME, f"Stored {att.filename} ({att.size} bytes) in #{channel.name}")
        return msg
    finally:
        tmp.unlink(missing_ok=True)


async def publish(bot, db: ReleaseDB, guild: discord.Guild, d: Draft):
    cfg = load_config()
    post_ch = discord.utils.get(guild.text_channels, name=cfg["releases_channel_name"])
    cache_ch = discord.utils.get(guild.text_channels, name=cfg["cache_channel_name"])
    if not post_ch:
        raise ReleaseError(f"Channel #{cfg['releases_channel_name']} not found.")
    if not cache_ch:
        raise ReleaseError(f"Private storage channel #{cfg['cache_channel_name']} not found.")
    if db.version_exists(d.title, d.version):
        raise ReleaseError(f"**{d.title}** already has a version **{d.version}**.")

    role = discord.utils.get(guild.roles, name=_role_name(bot)) if d.ping else None
    if d.ping and not role:
        raise ReleaseError(f"Role **{_role_name(bot)}** not found. Turn off `ping` or create it.")

    label = f"{d.title} — {d.version}"
    stored: list[discord.Message] = []
    vid = new_id()
    try:
        main_msg = await _stash(bot, cache_ch, d.file, label, guild.filesize_limit)
        stored.append(main_msg)
        orig_msg = None
        if d.original:
            orig_msg = await _stash(bot, cache_ch, d.original, f"{label} (original)", guild.filesize_limit)
            stored.append(orig_msg)

        row = dict(
            id=vid, version=d.version, type=d.type, note=d.note or None,
            changelog=d.changelog or None,
            sponsor_id=d.sponsor.id if d.sponsor else None,
            sponsor_kind=d.sponsor_kind if d.sponsor else None,
            file_name=d.file.filename, file_size=d.file.size,
            file_ch_id=cache_ch.id, file_msg_id=main_msg.id,
            orig_name=d.original.filename if d.original else None,
            orig_size=d.original.size if d.original else None,
            orig_ch_id=cache_ch.id if orig_msg else None,
            orig_msg_id=orig_msg.id if orig_msg else None,
            post_ch_id=post_ch.id, voting=int(d.voting),
        )
        db.add_version(d.title, row)

        view = discord.ui.LayoutView(timeout=None)
        for item in post_items(d, vid, role.mention if role else None):
            view.add_item(item)
        post = await post_ch.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=[role] if role else [],
                                                     users=False, everyone=False))
        db.update_version(vid, post_msg_id=post.id)
    except Exception:
        db.delete_version(vid)
        for m in stored:
            try:
                await m.delete()
            except Exception:
                pass
        raise

    thread = None
    if d.thread:
        try:
            thread = await post.create_thread(name=f"{d.title} — {d.type} {d.version}"[:100],
                                              auto_archive_duration=10080)
            db.update_version(vid, thread_id=thread.id)
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to create release thread", e)
    if d.voting:
        for emoji in VOTE_EMOJIS:
            try:
                await post.add_reaction(emoji)
            except Exception as e:
                bot.logger.error(MODULE_NAME, f"Failed to add reaction {emoji}", e)

    bot.logger.log(MODULE_NAME, f"Published {d.title!r} {d.version} ({d.type}) as {vid}")
    return post, thread
