import asyncio
import difflib
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands

from _utils import script_dir, migrate_config, _now, is_killswitch_active

MODULE_NAME = "RELEASE"

CONFIG_PATH = script_dir() / "config" / "release.json"
DB_PATH = script_dir() / "db" / "release.db"

CONFIG_DEFAULTS = {
    "releases_channel_name": "emball-remasters",
    "cache_channel_name": "release-cache",
    "preview_max_mb": 25,
    "preview_workers": 2,
    "preview_max_queue": 40,
    "preview_wait_seconds": 300,
    "preview_upload_seconds": 90,
    "cache_category_name": "",
}

TYPES = ["Remaster", "Edit", "Remaster & Edit"]
SPONSOR_KINDS = ["Sponsored by", "Paid request by"]
VOTE_EMOJIS = ["🔥", "😐", "🗑️"]
TEXT_LIMIT = 4000
STASH_ATTEMPTS = 3
TRANSIENT_ERRORS = (aiohttp.ClientError, OSError, asyncio.TimeoutError, discord.DiscordServerError)

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
    description     TEXT,
    changelog       TEXT,
    sponsor_id      INTEGER,
    sponsor_kind    TEXT,
    file_name       TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    file_ch_id      INTEGER NOT NULL,
    file_msg_id     INTEGER NOT NULL,
    orig_path       TEXT,
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
            cols = {r["name"] for r in c.execute("PRAGMA table_info(versions)")}
            if "orig_path" not in cols:
                c.execute("ALTER TABLE versions ADD COLUMN orig_path TEXT")
            if "note" in cols:
                c.execute("ALTER TABLE versions RENAME COLUMN note TO description")

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


def find_original(bot, title: str) -> Optional[dict]:
    idx = getattr(getattr(bot, "ARCHIVE_manager", None), "song_index", None)
    if not idx:
        return None
    try:
        from music_archive import normalize_title, select_best_candidate, FORMATS
    except ImportError as e:
        bot.logger.log(MODULE_NAME, f"Archive unavailable for original lookup: {e}", "WARNING")
        return None
    key = normalize_title(title)
    for fmt in FORMATS:
        songs = idx.get(fmt, {})
        match = key if key in songs else next(iter(difflib.get_close_matches(key, songs, n=1, cutoff=0.85)), None)
        cand = select_best_candidate(songs[match]) if match else None
        if cand:
            return cand
    return None


class ReleaseError(Exception):
    pass


class Draft:
    def __init__(self, *, title, version, type_, ping, thread, voting, path: Path,
                 original=None, sponsor=None, sponsor_kind=SPONSOR_KINDS[0], description=None,
                 channel=None):
        self.title = title.strip()
        self.version = version.strip()
        self.type = type_
        self.ping = ping
        self.thread = thread
        self.voting = voting
        self.path = path
        self.filename = path.name
        self.size = path.stat().st_size
        self.original = original
        self.sponsor = sponsor
        self.sponsor_kind = sponsor_kind
        self.description = (description or "").strip()
        self.changelog = ""
        self.channel = channel


def changelog_bullets(raw: str) -> str:
    lines = [re.sub(r"^\s*[-•*]\s*", "", ln).strip() for ln in (raw or "").splitlines()]
    return "\n".join(f"- {ln}" for ln in lines if ln)


def post_items(d: Draft, vid: Optional[str] = None, role_mention: Optional[str] = None,
               preview: bool = False) -> list:
    items = []
    if role_mention:
        items.append(discord.ui.TextDisplay(role_mention))

    head = [f"## {d.title}", f"**{d.type}** · **{d.version}**"]
    if d.sponsor:
        head.append(f"-# {d.sponsor_kind} {d.sponsor.mention}")
    if d.description:
        head.append(f"\n{d.description}")
    bullets = changelog_bullets(d.changelog)
    if bullets:
        head.append(f"\n{bullets}")

    children = [discord.ui.TextDisplay("\n".join(head))]
    if vid or preview:
        vid = vid or "preview"
        buttons = [discord.ui.Button(style=discord.ButtonStyle.primary, label="Download File",
                                     emoji="⬇️", custom_id=f"rel:dl:{vid}", disabled=preview),
                   discord.ui.Button(style=discord.ButtonStyle.secondary, label="Preview",
                                     emoji="▶️", custom_id=f"rel:pv:{vid}", disabled=preview)]
        if d.original:
            buttons.append(discord.ui.Button(style=discord.ButtonStyle.secondary, disabled=preview, emoji="💿",
                                             label="Download Original File", custom_id=f"rel:orig:{vid}"))
        children += [discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                     discord.ui.ActionRow(*buttons)]
    items.append(discord.ui.Container(*children, accent_color=0x1a1a2e))
    return items


def text_length(items) -> int:
    n = 0
    for it in items:
        if isinstance(it, discord.ui.TextDisplay):
            n += len(it.content)
        n += text_length(getattr(it, "children", None) or [])
    return n


def _mod_cfg(bot):
    ms = getattr(bot, "_mod_system", None)
    return ms.cfg if ms else None


def _role_name(bot) -> str:
    cfg = _mod_cfg(bot)
    return cfg.get("releases_role_name", "Emball Releases") if cfg else "Emball Releases"


async def _stash(bot, channel, path: Path, label: str, limit: int) -> discord.Message:
    size = path.stat().st_size
    if size > limit:
        raise ReleaseError(
            f"`{path.name}` is {size / 1048576:.1f} MB; the server upload limit is "
            f"{limit / 1048576:.0f} MB.")
    last = None
    for attempt in range(1, STASH_ATTEMPTS + 1):
        try:
            msg = await channel.send(content=label, file=discord.File(str(path), filename=path.name))
            bot.logger.log(MODULE_NAME, f"Stored {path.name} ({size} bytes) in #{channel.name}")
            return msg
        except TRANSIENT_ERRORS as e:
            last = e
            bot.logger.log(MODULE_NAME, f"Upload attempt {attempt} failed: {type(e).__name__}: {e}", "WARNING")
            try:
                async for m in channel.history(limit=5):
                    if (m.author.id == bot.user.id and m.content == label
                            and any(a.filename == path.name for a in m.attachments)):
                        bot.logger.log(MODULE_NAME, f"Upload had landed despite error, reusing message {m.id}")
                        return m
            except Exception:
                pass
            await asyncio.sleep(2 * attempt)
    raise ReleaseError("Network error while uploading to Discord. Press Post to try again.") from last


async def _ensure_cache_channel(bot, guild: discord.Guild, name: str) -> discord.TextChannel:
    ch = discord.utils.get(guild.text_channels, name=name)
    if ch:
        return ch
    cfg = load_config()
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              attach_files=True, read_message_history=True),
    }
    mc = _mod_cfg(bot)
    owner = guild.get_member(mc.owner_id) if mc and mc.owner_id else None
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
    cat_name = cfg.get("cache_category_name") or ""
    category = discord.utils.get(guild.categories, name=cat_name) if cat_name else None
    try:
        ch = await guild.create_text_channel(name, overwrites=overwrites, category=category,
                                             reason="Release file storage")
    except discord.Forbidden:
        raise ReleaseError(f"#{name} doesn't exist and I lack permission to create it.")
    bot.logger.log(MODULE_NAME, f"Created private storage channel #{name}")
    return ch


async def publish(bot, db: ReleaseDB, guild: discord.Guild, d: Draft):
    cfg = load_config()
    post_ch = d.channel or discord.utils.get(guild.text_channels, name=cfg["releases_channel_name"])
    if not post_ch:
        raise ReleaseError(f"Channel #{cfg['releases_channel_name']} not found.")
    if db.version_exists(d.title, d.version):
        raise ReleaseError(f"**{d.title}** already has a version **{d.version}**.")

    role = discord.utils.get(guild.roles, name=_role_name(bot)) if d.ping else None
    if d.ping and not role:
        raise ReleaseError(f"Role **{_role_name(bot)}** not found. Turn off `ping` or create it.")
    over = text_length(post_items(d, "x", role.mention if role else None)) - TEXT_LIMIT
    if over > 0:
        raise ReleaseError(f"Description and changelog are {over} characters over Discord's limit.")

    cache_ch = await _ensure_cache_channel(bot, guild, cfg["cache_channel_name"])
    label = f"{d.title} — {d.version}"
    stored: list[discord.Message] = []
    vid = new_id()
    try:
        main_msg = await _stash(bot, cache_ch, d.path, label, guild.filesize_limit)
        stored.append(main_msg)

        row = dict(
            id=vid, version=d.version, type=d.type, description=d.description or None,
            changelog=d.changelog or None,
            sponsor_id=d.sponsor.id if d.sponsor else None,
            sponsor_kind=d.sponsor_kind if d.sponsor else None,
            file_name=d.filename, file_size=d.size,
            file_ch_id=cache_ch.id, file_msg_id=main_msg.id,
            orig_path=d.original["path"] if d.original else None,
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


class EditModal(discord.ui.Modal):
    def __init__(self, view: "DraftView", field: str, title: str, label: str,
                 limit: int, placeholder: str = ""):
        super().__init__(title=title)
        self.draft_view, self.field = view, field
        self.text = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph,
                                         required=False, max_length=limit, placeholder=placeholder,
                                         default=getattr(view.d, field) or None)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        setattr(self.draft_view.d, self.field, str(self.text.value or "").strip())
        self.draft_view.error = None
        self.draft_view.render()
        await interaction.response.edit_message(view=self.draft_view)


class DraftView(discord.ui.LayoutView):
    def __init__(self, bot, db: ReleaseDB, guild: discord.Guild, d: Draft, owner_id: int):
        super().__init__(timeout=900)
        self.bot, self.db, self.guild, self.d, self.owner_id = bot, db, guild, d, owner_id
        self.error: Optional[str] = None
        self.busy = False
        self.render()

    def render(self):
        self.clear_items()
        for item in post_items(self.d, preview=True):
            self.add_item(item)

        desc = discord.ui.Button(label="Edit description", style=discord.ButtonStyle.secondary)
        edit = discord.ui.Button(label="Edit changelog", style=discord.ButtonStyle.secondary)
        post = discord.ui.Button(label="Post", style=discord.ButtonStyle.success, disabled=self.busy)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, disabled=self.busy)
        desc.callback, edit.callback = self._edit_description, self._edit_changelog
        post.callback, cancel.callback = self._post, self._cancel

        flags = (f"ping {'on' if self.d.ping else 'off'} · thread {'on' if self.d.thread else 'off'} · "
                 f"voting {'on' if self.d.voting else 'off'}")
        files = f"`{self.d.filename}`"
        orig = (f"Original: `{self.d.original['original_title']}`" if self.d.original
                else "No archive match, so no Original button")
        lines = ["-# Draft preview. Nothing is posted until you press Post.",
                 f"-# {flags} · {files}", f"-# {orig}"]
        over = text_length(post_items(self.d, preview=True)) - TEXT_LIMIT
        if over > 0:
            lines.insert(0, f"**Too long:** {over} characters over Discord's 4000 limit. Trim the description or changelog.")
        if self.error:
            lines.insert(0, f"**Error:** {self.error}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.ActionRow(desc, edit, post, cancel),
            accent_color=0xe74c3c if self.error else 0x5865f2))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return False
        return True

    async def _edit_description(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(
            self, "description", "Description", "Description (markdown supported)", 2600))

    async def _edit_changelog(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(
            self, "changelog", "Changelog", "Changes (one per line)", 1200,
            "Compression restored\nMixed the vocals properly"))

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=_notice("Draft discarded."))

    async def _post(self, interaction: discord.Interaction):
        if self.busy:
            return
        self.busy, self.error = True, None
        self.render()
        await interaction.response.edit_message(view=self)
        try:
            post, thread = await publish(self.bot, self.db, self.guild, self.d)
        except ReleaseError as e:
            self.busy, self.error = False, str(e)
            self.render()
            await interaction.edit_original_response(view=self)
            return
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Release publish failed", e)
            self.busy, self.error = False, "Publish failed. Check the bot console."
            self.render()
            await interaction.edit_original_response(view=self)
            return
        self.stop()
        extra = f" · [thread]({thread.jump_url})" if thread else ""
        await interaction.edit_original_response(
            view=_notice(f"Posted **{self.d.title}** {self.d.version}: [jump to release]({post.jump_url}){extra}"))


def _notice(text: str) -> discord.ui.LayoutView:
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=0x2ecc71))
    return v


def is_fed(guild_id, user_id) -> bool:
    try:
        from mod_suspicion import is_flagged
        if guild_id:
            return is_flagged(str(guild_id), str(user_id))
    except Exception:
        pass
    return False


async def fresh_url(bot, ch_id: int, msg_id: int) -> Optional[str]:
    try:
        ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        msg = await ch.fetch_message(msg_id)
        return msg.attachments[0].url if msg.attachments else None
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Fresh URL fetch failed for message {msg_id}: {e}", "WARNING")
        return None


async def _log_delivery(bot, guild, user, title: str, version: str, name: str, which: str):
    try:
        ch = discord.utils.get(guild.text_channels, name="bot-logs")
        if not ch:
            return
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(
            f"# Release Delivery\n**User**\n{user} ({user.id})\n\n**Release**\n{title} {version}"
            f"\n\n**File**\n`{name}` ({which})"), accent_color=0x5865f2))
        await ch.send(view=view)
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to log delivery", e)


PREVIEW_DIR = script_dir() / "cache" / "release_preview"
PREVIEW_TTL = 7 * 86400
_preview_locks: dict = {}
_waiting: list = []
_active_users: set = set()
_last_text: dict = {}
_edit_locks: dict = {}
_live: set = set()
_refresh_pending = False
_sem: Optional[asyncio.Semaphore] = None


async def _fetch_to_disk(url: str, dest: Path, limit: int) -> bool:
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url) as r:
            if r.status != 200 or (r.content_length and r.content_length > limit):
                return False
            data = await r.read()
    if len(data) > limit:
        return False
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True


def _prune_preview_cache():
    cutoff = _now().timestamp() - PREVIEW_TTL
    for f in PREVIEW_DIR.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def _cached_file(key: str, name: str, url: str, limit: int) -> Optional[Path]:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    dest = PREVIEW_DIR / f"{key}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}"
    lock = _preview_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not dest.exists():
            if not await _fetch_to_disk(url, dest, limit):
                return None
            _prune_preview_cache()
    return dest


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, int(load_config().get("preview_workers", 2))))
    return _sem


async def _edit(interaction: discord.Interaction, **kw):
    if interaction.id not in _live:
        return
    lock = _edit_locks.setdefault(interaction.id, asyncio.Lock())
    async with lock:
        await interaction.edit_original_response(**kw)


async def _set_status(bot, interaction: discord.Interaction, text: str, only_waiting: bool = False):
    if interaction.id not in _live or (only_waiting and interaction not in _waiting):
        return
    lock = _edit_locks.setdefault(interaction.id, asyncio.Lock())
    async with lock:
        if interaction.id not in _live or (only_waiting and interaction not in _waiting):
            return
        if _last_text.get(interaction.id) == text:
            return
        _last_text[interaction.id] = text
        try:
            await interaction.edit_original_response(content=text)
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Status update failed: {e}", "WARNING")


async def _refresh_queue(bot):
    global _refresh_pending
    await asyncio.sleep(1.5)
    _refresh_pending = False
    waiting = list(_waiting)
    await asyncio.gather(*[
        _set_status(bot, i, f"⏳ Preview queued: position {_waiting.index(i) + 1}", only_waiting=True)
        for i in waiting if i in _waiting])


def _schedule_refresh(bot):
    global _refresh_pending
    if not _refresh_pending and _waiting:
        _refresh_pending = True
        asyncio.ensure_future(_refresh_queue(bot))


async def _link_fallback(bot, interaction: discord.Interaction, text: str):
    try:
        await _edit(interaction, content=text, attachments=[])
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Fallback reply failed: {e}", "WARNING")


async def _send_preview(bot, interaction: discord.Interaction, key: str, name: str, url: str) -> bool:
    _live.add(interaction.id)
    try:
        return await _send_preview_inner(bot, interaction, key, name, url)
    finally:
        _live.discard(interaction.id)
        _last_text.pop(interaction.id, None)
        _edit_locks.pop(interaction.id, None)


async def _send_preview_inner(bot, interaction: discord.Interaction, key: str, name: str, url: str) -> bool:
    cfg = load_config()
    limit = int(cfg.get("preview_max_mb", 25)) * 1024 * 1024
    link = f"[Download {name}]({url})"
    uid = interaction.user.id
    if uid in _active_users:
        await _set_status(bot, interaction, "You already have a preview loading. Check your earlier reply.")
        return True
    if len(_waiting) >= int(cfg.get("preview_max_queue", 40)):
        bot.logger.log(MODULE_NAME, "Preview queue full, sending link instead", "WARNING")
        await _link_fallback(bot, interaction, f"Preview is very busy right now. {link}")
        return True

    _active_users.add(uid)
    sem = _get_sem()
    acquired = False
    try:
        if sem.locked():
            _waiting.append(interaction)
            await _set_status(bot, interaction, f"⏳ Preview queued: position {len(_waiting)}")
        try:
            await asyncio.wait_for(sem.acquire(), float(cfg.get("preview_wait_seconds", 300)))
            acquired = True
        except asyncio.TimeoutError:
            bot.logger.log(MODULE_NAME, "Preview wait timed out, sending link instead", "WARNING")
            await _link_fallback(bot, interaction, f"Preview is taking too long. {link}")
            return True
        if interaction in _waiting:
            _waiting.remove(interaction)
            _schedule_refresh(bot)
        await _set_status(bot, interaction, "⬆️ Uploading your preview…")
        path = await _cached_file(key, name, url, limit)
        if not path:
            bot.logger.log(MODULE_NAME, f"Preview skipped for {name!r} (over limit or fetch failed)", "WARNING")
            await _link_fallback(bot, interaction, f"Too large to preview here. {link}")
            return True
        await asyncio.wait_for(
            _edit(interaction, content=f"-# [Direct download link]({url})",
                  attachments=[discord.File(str(path), filename=name)]),
            float(cfg.get("preview_upload_seconds", 90)))
        bot.logger.log(MODULE_NAME, f"Sent preview of {name!r}")
        return True
    except Exception as e:
        bot.logger.error(MODULE_NAME, f"Preview failed for {name!r}", e)
        await _link_fallback(bot, interaction, f"Preview failed. {link}")
        return True
    finally:
        if acquired:
            sem.release()
        if interaction in _waiting:
            _waiting.remove(interaction)
            _schedule_refresh(bot)
        _active_users.discard(uid)
        _last_text.pop(interaction.id, None)
        _edit_locks.pop(interaction.id, None)


async def _handle_download(bot, db: ReleaseDB, interaction: discord.Interaction, which: str, vid: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    fail = "Failed to retrieve file."
    if is_killswitch_active(bot, "release_core") or is_fed(interaction.guild_id, interaction.user.id):
        bot.logger.log(MODULE_NAME, f"Download denied for {interaction.user} ({interaction.user.id})")
        await interaction.edit_original_response(content=fail)
        return
    v = db.get_version(vid)
    if not v:
        await interaction.edit_original_response(content=fail)
        return
    if which == "orig" and v["orig_path"]:
        from music_archive import _get_or_upload_cache, LARGE_FILE_MSG
        name = Path(v["orig_path"]).name
        url = await _get_or_upload_cache(bot, v["orig_path"])
        if url == "FILE_TOO_LARGE":
            await interaction.edit_original_response(content=LARGE_FILE_MSG)
            return
    else:
        name = v["file_name"]
        url = await fresh_url(bot, v["file_ch_id"], v["file_msg_id"])
    if not url:
        await interaction.edit_original_response(content=fail)
        return
    if which == "pv":
        if not await _send_preview(bot, interaction, f"pv-{vid}", name, url):
            await interaction.edit_original_response(content=fail)
            return
    else:
        await interaction.edit_original_response(content=f"[{name}]({url})")
    bot.logger.log(MODULE_NAME, f"Delivered {name!r} to {interaction.user} ({which})")
    if interaction.guild:
        await _log_delivery(bot, interaction.guild, interaction.user, v["title"], v["version"], name, which)


async def _remove_reaction(bot, channel_id: int, message_id: int, emoji, user_id: int):
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await ch.get_partial_message(message_id).remove_reaction(emoji, discord.Object(id=user_id))
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Could not remove reaction: {e}", "WARNING")


async def _fail_reply(interaction: discord.Interaction):
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(content="Failed to retrieve file.", attachments=[])
        else:
            await interaction.response.send_message("Failed to retrieve file.", ephemeral=True)
    except Exception:
        pass


def register_listeners(bot, db: ReleaseDB, owner_id):
    @bot.listen("on_interaction")
    async def on_interaction(interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("rel:"):
            return
        seen = bot.__dict__.setdefault("_release_seen", {})
        if interaction.id in seen:
            return
        seen[interaction.id] = True
        while len(seen) > 500:
            seen.pop(next(iter(seen)))
        try:
            _, which, vid = cid.split(":", 2)
            await _handle_download(bot, db, interaction, which, vid)
        except discord.HTTPException as e:
            if e.code == 40060:
                bot.logger.log(MODULE_NAME, f"Interaction {interaction.id} already acknowledged, ignoring", "WARNING")
                return
            bot.logger.error(MODULE_NAME, f"Download handler failed for {cid}", e)
            await _fail_reply(interaction)
        except Exception as e:
            bot.logger.error(MODULE_NAME, f"Download handler failed for {cid}", e)
            await _fail_reply(interaction)

    @bot.listen("on_raw_reaction_add")
    async def on_reaction_add(payload: discord.RawReactionActionEvent):
        emoji = str(payload.emoji)
        if not payload.guild_id or payload.user_id == bot.user.id or emoji not in VOTE_EMOJIS:
            return
        v = db.version_by_post(payload.message_id)
        if not v or not v["voting"]:
            return
        if payload.user_id == owner_id():
            await _remove_reaction(bot, payload.channel_id, payload.message_id, payload.emoji, payload.user_id)
            return
        if is_fed(payload.guild_id, payload.user_id):
            bot.logger.log(MODULE_NAME, f"Vote from flagged user {payload.user_id} not recorded")
            return
        old = db.upsert_vote(v["id"], payload.user_id, emoji)
        if old and old["emoji"] != emoji:
            await _remove_reaction(bot, payload.channel_id, payload.message_id, old["emoji"], payload.user_id)
        bot.logger.log(MODULE_NAME, f"Vote {emoji} by {payload.user_id} on {v['id']}")

    @bot.listen("on_raw_reaction_remove")
    async def on_reaction_remove(payload: discord.RawReactionActionEvent):
        emoji = str(payload.emoji)
        if not payload.guild_id or emoji not in VOTE_EMOJIS:
            return
        v = db.version_by_post(payload.message_id)
        if v and db.remove_vote(v["id"], payload.user_id, emoji):
            bot.logger.log(MODULE_NAME, f"Vote {emoji} removed by {payload.user_id} on {v['id']}")


def setup(bot):
    db = ReleaseDB()
    load_config()
    bot._release_system = type("ReleaseSystem", (), {"db": db})()

    def owner_id() -> int:
        cfg = _mod_cfg(bot)
        return cfg.owner_id if cfg else 0

    async def title_autocomplete(interaction: discord.Interaction, current: str):
        with db._conn() as c:
            rows = c.execute("SELECT title FROM releases WHERE title LIKE ? ORDER BY title LIMIT 25",
                             (f"%{current}%",)).fetchall()
        return [app_commands.Choice(name=r["title"][:100], value=r["title"][:100]) for r in rows]

    @bot.tree.command(name="release", description="[Owner only] Draft and post a remaster/edit release")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.rename(type_="type")
    @app_commands.describe(
        title="Song title. Reusing an existing title adds a new version to it",
        path="Full path to the release file on the bot's machine",
        version="Version label (default: next in sequence, V1 for new titles)",
        type_="Kind of release",
        ping="Ping the releases role",
        thread="Create a feedback thread",
        voting="Enable reaction voting",
        sponsor="Member to credit",
        sponsor_kind="How to credit the sponsor",
        channel="Post here instead of the releases channel (for testing)",
    )
    @app_commands.choices(
        type_=[app_commands.Choice(name=t, value=t) for t in TYPES],
        sponsor_kind=[app_commands.Choice(name=s, value=s) for s in SPONSOR_KINDS],
    )
    async def release(interaction: discord.Interaction, title: str, path: str,
                      version: Optional[str] = None, type_: str = TYPES[0],
                      ping: bool = True, thread: bool = True, voting: bool = True,
                      sponsor: Optional[discord.User] = None,
                      sponsor_kind: str = SPONSOR_KINDS[0],
                      channel: Optional[discord.TextChannel] = None):
        if interaction.user.id != owner_id():
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        if is_killswitch_active(bot, "release_core"):
            await interaction.response.send_message("Kill switch is active.", ephemeral=True)
            return
        guild = interaction.guild or (bot.guilds[0] if bot.guilds else None)
        if not guild:
            await interaction.response.send_message("No guild available.", ephemeral=True)
            return
        p = Path(path.strip().strip("\"'")).expanduser()
        if not p.is_file():
            await interaction.response.send_message(
                f"File not found on the bot's machine: `{p}`", ephemeral=True)
            return
        d = Draft(title=title, version=version or db.next_version_label(title), type_=type_,
                  ping=ping, thread=thread, voting=voting, path=p,
                  original=find_original(bot, title), sponsor=sponsor, sponsor_kind=sponsor_kind,
                  channel=channel)
        bot.logger.log(MODULE_NAME, f"Draft started: {d.title!r} {d.version} ({d.type})")
        await interaction.response.send_message(
            view=DraftView(bot, db, guild, d, interaction.user.id), ephemeral=True)

    release.autocomplete("title")(title_autocomplete)
    register_listeners(bot, db, owner_id)
    bot.logger.log(MODULE_NAME, "Release system loaded")
