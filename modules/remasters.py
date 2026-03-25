"""
remasters.py — Remaster Release System for Embot

Console command: /remaster  (subcommands: manage, list)
"""

import discord
from discord.ext import commands
import sqlite3
import asyncio
import threading
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import json
import re
import uuid as _uuid

try:
    import curses
    HAS_CURSES = True
except ImportError:
    HAS_CURSES = False

MODULE_NAME = "REMASTERS"


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _script_dir() -> Path:
    return Path(__file__).parent.parent.absolute()

def _load_config() -> dict:
    config_path = _script_dir() / "config" / "remasters_config.json"
    defaults = {
        "announcements_channel_name": "announcements",
        "offtopic_channel_name": "off-topic",
        "releases_role_name": "Emball Releases",
        "info_channel_name": "info",
        "info_embed_msg_id": None,
    }
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults.update(data)
        except Exception as e:
            print(f"[REMASTERS] Failed to load remasters_config.json: {e}")
    return defaults

def _save_config(data: dict) -> None:
    config_path = _script_dir() / "config" / "remasters_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing.update(data)
        tmp = str(config_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(config_path))
    except Exception as e:
        print(f"[REMASTERS] Failed to save config: {e}")

CONFIG: dict = {}  # filled in setup()


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE  (no token table — ephemeral delivery is stateless)
# ══════════════════════════════════════════════════════════════════════════════

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS remasters (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    latest_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remaster_versions (
    id                  TEXT PRIMARY KEY,
    remaster_id         TEXT NOT NULL REFERENCES remasters(id),
    version             TEXT NOT NULL,
    cdn_url             TEXT NOT NULL,
    filename            TEXT NOT NULL,
    image_cdn_url       TEXT,
    announcement_msg_id TEXT,
    announcement_ch_id  TEXT,
    is_latest           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    UNIQUE(remaster_id, version)
);
"""

def _db_path() -> Path:
    return _script_dir() / "db" / "remasters.db"

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def _init_db() -> None:
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript(DB_SCHEMA)
    conn.commit()
    conn.close()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _new_id() -> str:
    return str(_uuid.uuid4())

# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_all_remasters() -> list:
    with _get_conn() as c:
        rows = c.execute("SELECT * FROM remasters ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]

def _db_get_remaster(rid: str) -> Optional[dict]:
    with _get_conn() as c:
        r = c.execute("SELECT * FROM remasters WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None

def _db_versions_for(rid: str) -> list:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT * FROM remaster_versions WHERE remaster_id=? ORDER BY created_at DESC",
            (rid,)
        ).fetchall()
    return [dict(r) for r in rows]

def _db_get_version(vid: str) -> Optional[dict]:
    with _get_conn() as c:
        r = c.execute("SELECT * FROM remaster_versions WHERE id=?", (vid,)).fetchone()
    return dict(r) if r else None

def _db_latest_version(rid: str) -> Optional[dict]:
    with _get_conn() as c:
        r = c.execute(
            "SELECT * FROM remaster_versions WHERE remaster_id=? AND is_latest=1", (rid,)
        ).fetchone()
    return dict(r) if r else None

def _db_create_remaster(title: str, desc: str, version: str) -> str:
    rid = _new_id()
    now = _now_iso()
    with _get_conn() as c:
        c.execute(
            "INSERT INTO remasters (id,title,description,latest_version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (rid, title, desc, version, now, now)
        )
        c.commit()
    return rid

def _db_add_version(rid: str, version: str, cdn_url: str, filename: str,
                    image_url: Optional[str]) -> str:
    vid = _new_id()
    now = _now_iso()
    with _get_conn() as c:
        c.execute("UPDATE remaster_versions SET is_latest=0 WHERE remaster_id=?", (rid,))
        c.execute(
            "INSERT INTO remaster_versions "
            "(id,remaster_id,version,cdn_url,filename,image_cdn_url,is_latest,created_at) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (vid, rid, version, cdn_url, filename, image_url, now)
        )
        c.execute("UPDATE remasters SET latest_version=?, updated_at=? WHERE id=?",
                  (version, now, rid))
        c.commit()
    return vid

def _db_set_announcement(vid: str, msg_id: str, ch_id: str) -> None:
    with _get_conn() as c:
        c.execute(
            "UPDATE remaster_versions SET announcement_msg_id=?, announcement_ch_id=? WHERE id=?",
            (msg_id, ch_id, vid)
        )
        c.commit()

def _db_update_remaster_meta(rid: str, title: str, desc: str) -> None:
    with _get_conn() as c:
        c.execute(
            "UPDATE remasters SET title=?, description=?, updated_at=? WHERE id=?",
            (title, desc, _now_iso(), rid)
        )
        c.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  EMBED BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _release_embed(remaster: dict, version_row: dict, role_mention: str = "",
                   is_update: bool = False) -> discord.Embed:
    title = remaster["title"]
    version = version_row["version"]
    desc = remaster["description"]
    if is_update:
        embed = discord.Embed(
            title=f"🔄  {title}  ·  {version}",
            description=f"*A new version of **{title}** is now available.*\n\n{desc}",
            color=discord.Color.from_rgb(90, 160, 255),
        )
        embed.set_footer(text=f"Updated release  ·  {version}  ·  Click Download to get your copy")
    else:
        embed = discord.Embed(
            title=f"✦  {title}  ·  {version}",
            description=desc,
            color=discord.Color.from_rgb(255, 215, 80),
        )
        embed.set_footer(text=f"New release  ·  {version}  ·  Click Download to listen")
    if version_row.get("image_cdn_url"):
        embed.set_image(url=version_row["image_cdn_url"])
    embed.add_field(name="Version", value=f"`{version}`", inline=True)
    embed.add_field(name="File", value=f"`{version_row['filename']}`", inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    return embed

def _outdated_embed(old_embed: discord.Embed, latest_version: str) -> discord.Embed:
    new_embed = old_embed.copy()
    notice = f"⚠️  *A newer version (`{latest_version}`) is available. This embed is archived.*\n\n"
    new_embed.description = notice + (old_embed.description or "")
    new_embed.color = discord.Color.from_rgb(120, 120, 120)
    return new_embed


# ══════════════════════════════════════════════════════════════════════════════
#  CONSOLE TUI  (unchanged from original)
# ══════════════════════════════════════════════════════════════════════════════

class _RemasterTUI:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._result = None

    def run_new_release(self) -> Optional[dict]:
        if HAS_CURSES:
            curses.wrapper(self._curses_new_release)
        else:
            self._fallback_new_release()
        return self._result

    def run_manage(self) -> Optional[dict]:
        if HAS_CURSES:
            curses.wrapper(self._curses_manage)
        else:
            self._fallback_manage()
        return self._result

    def _curses_new_release(self, stdscr):
        curses.curs_set(1)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)
        curses.init_pair(2, curses.COLOR_CYAN,   -1)
        curses.init_pair(3, curses.COLOR_GREEN,  -1)
        curses.init_pair(4, curses.COLOR_RED,    -1)
        fields = [
            ("Title",       "", False),
            ("Version",     "", False),
            ("Description", "", False),
            ("File path",   "", False),
            ("Image path",  "", True),
        ]
        idx = 0
        error = ""
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            header = "  ✦  EMBOT REMASTER RELEASE  ✦  New Release"
            stdscr.addstr(1, max(0, (w - len(header)) // 2), header,
                          curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(2, 2, "─" * (w - 4), curses.color_pair(1))
            stdscr.addstr(3, 2, "Tab/↓ next field  ↑ prev  Enter submit  Ctrl+C cancel",
                          curses.color_pair(3))
            for i, (label, value, optional) in enumerate(fields):
                y = 5 + i * 3
                opt_str = "  (optional)" if optional else ""
                stdscr.addstr(y, 2, f"  {label}{opt_str}:",
                              curses.color_pair(2) | (curses.A_BOLD if i == idx else 0))
                box_w = w - 6
                val_display = value[-box_w:] if len(value) > box_w else value
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                stdscr.addstr(y + 1, 3, val_display.ljust(box_w)[:box_w], attr)
            if error:
                stdscr.addstr(5 + len(fields) * 3, 2, f"  ✗  {error}", curses.color_pair(4))
            stdscr.addstr(h - 2, 2, "  [ SUBMIT ]  Ctrl+C to cancel", curses.color_pair(3))
            stdscr.refresh()
            _, value, _ = fields[idx]
            cy = 5 + idx * 3 + 1
            cx = min(3 + len(value), w - 4)
            try:
                stdscr.move(cy, cx)
            except curses.error:
                pass
            try:
                ch = stdscr.get_wch()
            except KeyboardInterrupt:
                self._result = None
                return
            label, value, optional = fields[idx]
            if isinstance(ch, str):
                if ch in ('\n', '\r'):
                    vals = {f[0]: f[1] for f in fields}
                    if not vals["Title"].strip():
                        error = "Title is required."; continue
                    if not vals["Version"].strip():
                        error = "Version is required."; continue
                    if not vals["Description"].strip():
                        error = "Description is required."; continue
                    if not vals["File path"].strip():
                        error = "File path is required."; continue
                    fp = Path(vals["File path"].strip())
                    if not fp.exists():
                        error = f"File not found: {fp}"; continue
                    ip = vals["Image path"].strip()
                    img_path = None
                    if ip:
                        imgp = Path(ip)
                        if not imgp.exists():
                            error = f"Image not found: {imgp}"; continue
                        img_path = str(imgp)
                    self._result = {
                        "action": "new",
                        "title": vals["Title"].strip(),
                        "version": vals["Version"].strip(),
                        "description": vals["Description"].strip(),
                        "file_path": str(fp),
                        "image_path": img_path,
                    }
                    return
                elif ch == '\t':
                    idx = (idx + 1) % len(fields)
                elif ch in ('\x7f', '\b'):
                    fields[idx] = (label, value[:-1], optional)
                elif ord(ch) == 3:
                    self._result = None
                    return
                elif ch.isprintable():
                    fields[idx] = (label, value + ch, optional)
            else:
                if ch in (curses.KEY_DOWN, 9):
                    idx = (idx + 1) % len(fields)
                elif ch == curses.KEY_UP:
                    idx = (idx - 1) % len(fields)
                elif ch in (curses.KEY_BACKSPACE, 127):
                    fields[idx] = (label, value[:-1], optional)

    def _curses_manage(self, stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)
        curses.init_pair(2, curses.COLOR_CYAN,   -1)
        curses.init_pair(3, curses.COLOR_GREEN,  -1)
        curses.init_pair(4, curses.COLOR_RED,    -1)
        curses.init_pair(5, curses.COLOR_WHITE,  curses.COLOR_BLUE)
        remasters = _db_all_remasters()
        if not remasters:
            stdscr.addstr(2, 2, "  No releases yet.", curses.color_pair(4))
            stdscr.addstr(4, 2, "  Press any key to exit.")
            stdscr.refresh()
            stdscr.getch()
            self._result = None
            return
        sel = 0
        page = "list"
        detail_rem = None
        detail_ver_sel = 0
        fields = []
        field_idx = 0
        error = ""
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            if page == "list":
                header = "  ✦  EMBOT REMASTERS  ✦  Manage Releases"
                stdscr.addstr(1, max(0, (w - len(header)) // 2), header,
                              curses.color_pair(1) | curses.A_BOLD)
                stdscr.addstr(2, 2, "─" * (w - 4), curses.color_pair(1))
                stdscr.addstr(3, 2, "↑↓ navigate  Enter select  Q quit", curses.color_pair(3))
                for i, rem in enumerate(remasters):
                    y = 5 + i
                    label = f"  {rem['title']:40s}  v{rem['latest_version']:12s}  {rem['updated_at'][:10]}"
                    if i == sel:
                        stdscr.addstr(y, 2, label[:w-4].ljust(w-4), curses.color_pair(5))
                    else:
                        stdscr.addstr(y, 2, label[:w-4])
                stdscr.refresh()
                ch = stdscr.getch()
                if ch == curses.KEY_UP:
                    sel = max(0, sel - 1)
                elif ch == curses.KEY_DOWN:
                    sel = min(len(remasters) - 1, sel + 1)
                elif ch in (curses.KEY_ENTER, 10, 13):
                    detail_rem = remasters[sel]
                    detail_ver_sel = 0
                    page = "detail"
                elif ch in (ord('q'), ord('Q')):
                    self._result = None
                    return
            elif page == "detail":
                versions = _db_versions_for(detail_rem["id"])
                header = f"  ✦  {detail_rem['title']}  ✦  v{detail_rem['latest_version']}"
                stdscr.addstr(1, 2, header, curses.color_pair(1) | curses.A_BOLD)
                stdscr.addstr(2, 2, "─" * (w - 4), curses.color_pair(1))
                stdscr.addstr(3, 2, "A add version  E edit metadata  B back  Q quit",
                              curses.color_pair(3))
                stdscr.addstr(5, 2, "Versions:", curses.color_pair(2))
                for i, ver in enumerate(versions):
                    y = 6 + i
                    tag = " ← latest" if ver["is_latest"] else ""
                    label = f"  {ver['version']:15s}  {ver['created_at'][:10]}{tag}"
                    if i == detail_ver_sel:
                        stdscr.addstr(y, 2, label[:w-4].ljust(w-4), curses.color_pair(5))
                    else:
                        stdscr.addstr(y, 2, label[:w-4])
                stdscr.refresh()
                ch = stdscr.getch()
                if ch == curses.KEY_UP:
                    detail_ver_sel = max(0, detail_ver_sel - 1)
                elif ch == curses.KEY_DOWN:
                    detail_ver_sel = min(len(versions) - 1, detail_ver_sel + 1)
                elif ch in (ord('a'), ord('A')):
                    fields = [["Version", "", False], ["File path", "", False], ["Image path", "", True]]
                    field_idx = 0
                    error = ""
                    page = "add_version"
                elif ch in (ord('e'), ord('E')):
                    fields = [
                        ["Title",       detail_rem["title"],       False],
                        ["Description", detail_rem["description"], False],
                    ]
                    field_idx = 0
                    error = ""
                    page = "edit_meta"
                elif ch in (ord('b'), ord('B')):
                    remasters = _db_all_remasters()
                    page = "list"
                elif ch in (ord('q'), ord('Q')):
                    self._result = None
                    return
            elif page in ("add_version", "edit_meta"):
                is_add = page == "add_version"
                header = f"  ✦  {detail_rem['title']}  ✦  {'Add Version' if is_add else 'Edit Metadata'}"
                stdscr.addstr(1, 2, header, curses.color_pair(1) | curses.A_BOLD)
                stdscr.addstr(2, 2, "─" * (w - 4), curses.color_pair(1))
                stdscr.addstr(3, 2, "Tab/↓ next  ↑ prev  Enter submit  B back",
                              curses.color_pair(3))
                for i, (label, value, optional) in enumerate(fields):
                    y = 5 + i * 3
                    opt_str = "  (optional)" if optional else ""
                    stdscr.addstr(y, 2, f"  {label}{opt_str}:",
                                  curses.color_pair(2) | (curses.A_BOLD if i == field_idx else 0))
                    box_w = w - 6
                    val_display = value[-(box_w):] if len(value) > box_w else value
                    attr = curses.A_REVERSE if i == field_idx else curses.A_NORMAL
                    stdscr.addstr(y + 1, 3, val_display.ljust(box_w)[:box_w], attr)
                if error:
                    stdscr.addstr(5 + len(fields) * 3, 2, f"  ✗  {error}", curses.color_pair(4))
                _, value, _ = fields[field_idx]
                cy = 5 + field_idx * 3 + 1
                cx = min(3 + len(value), w - 4)
                curses.curs_set(1)
                try:
                    stdscr.move(cy, cx)
                except Exception:
                    pass
                stdscr.refresh()
                try:
                    ch = stdscr.get_wch()
                except KeyboardInterrupt:
                    self._result = None
                    return
                label, value, optional = fields[field_idx]
                if isinstance(ch, str):
                    if ch in ('\n', '\r'):
                        vals = {f[0]: f[1] for f in fields}
                        if is_add:
                            if not vals["Version"].strip():
                                error = "Version required."; continue
                            if not vals["File path"].strip():
                                error = "File path required."; continue
                            fp = Path(vals["File path"].strip())
                            if not fp.exists():
                                error = f"File not found: {fp}"; continue
                            ip = vals["Image path"].strip()
                            img = None
                            if ip:
                                imgp = Path(ip)
                                if not imgp.exists():
                                    error = f"Image not found: {imgp}"; continue
                                img = str(imgp)
                            self._result = {
                                "action": "add_version",
                                "remaster_id": detail_rem["id"],
                                "version": vals["Version"].strip(),
                                "file_path": str(fp),
                                "image_path": img,
                            }
                        else:
                            if not vals["Title"].strip():
                                error = "Title required."; continue
                            self._result = {
                                "action": "edit_meta",
                                "remaster_id": detail_rem["id"],
                                "title": vals["Title"].strip(),
                                "description": vals["Description"].strip(),
                            }
                        return
                    elif ch == '\t':
                        field_idx = (field_idx + 1) % len(fields)
                    elif ch in ('\x7f', '\b'):
                        fields[field_idx] = [label, value[:-1], optional]
                    elif ch in (ord('b'), ord('B')) and field_idx == 0 and not value:
                        curses.curs_set(0)
                        page = "detail"
                    elif ord(ch) == 3:
                        self._result = None
                        return
                    elif ch.isprintable():
                        fields[field_idx] = [label, value + ch, optional]
                else:
                    if ch in (curses.KEY_DOWN, 9):
                        field_idx = (field_idx + 1) % len(fields)
                    elif ch == curses.KEY_UP:
                        field_idx = (field_idx - 1) % len(fields)
                    elif ch in (curses.KEY_BACKSPACE, 127):
                        fields[field_idx] = [label, value[:-1], optional]

    def _fallback_new_release(self):
        print("\n  ✦  EMBOT REMASTER RELEASE  ✦  New Release")
        print("  Leave blank and press Enter to cancel.\n")
        try:
            title = input("  Title: ").strip()
            if not title:
                self._result = None; return
            version = input("  Version: ").strip()
            if not version:
                self._result = None; return
            desc = input("  Description: ").strip()
            if not desc:
                self._result = None; return
            file_path = input("  File path: ").strip()
            if not file_path or not Path(file_path).exists():
                print("  File not found."); self._result = None; return
            image_path = input("  Image path (optional, Enter to skip): ").strip() or None
            if image_path and not Path(image_path).exists():
                print("  Image not found."); self._result = None; return
            self._result = {
                "action": "new",
                "title": title,
                "version": version,
                "description": desc,
                "file_path": file_path,
                "image_path": image_path,
            }
        except (EOFError, KeyboardInterrupt):
            self._result = None

    def _fallback_manage(self):
        remasters = _db_all_remasters()
        if not remasters:
            print("  No releases yet.\n")
            self._result = None
            return
        print("\n  ✦  EMBOT REMASTERS  ✦  Manage Releases\n")
        for i, rem in enumerate(remasters):
            print(f"  [{i+1}] {rem['title']}  v{rem['latest_version']}")
        try:
            choice = input("\n  Select number (or 0 to cancel): ").strip()
            if not choice or choice == "0":
                self._result = None; return
            idx = int(choice) - 1
            if not (0 <= idx < len(remasters)):
                self._result = None; return
            rem = remasters[idx]
            print(f"\n  {rem['title']}  v{rem['latest_version']}")
            print("  [1] Add new version\n  [2] Edit metadata\n  [0] Cancel")
            action = input("  Choice: ").strip()
            if action == "1":
                version = input("  New version: ").strip()
                if not version:
                    self._result = None; return
                file_path = input("  File path: ").strip()
                if not file_path or not Path(file_path).exists():
                    print("  File not found."); self._result = None; return
                image_path = input("  Image path (optional): ").strip() or None
                self._result = {
                    "action": "add_version",
                    "remaster_id": rem["id"],
                    "version": version,
                    "file_path": file_path,
                    "image_path": image_path,
                }
            elif action == "2":
                new_title = input(f"  Title [{rem['title']}]: ").strip() or rem["title"]
                new_desc = input(f"  Description [{rem['description'][:40]}]: ").strip() \
                    or rem["description"]
                self._result = {
                    "action": "edit_meta",
                    "remaster_id": rem["id"],
                    "title": new_title,
                    "description": new_desc,
                }
            else:
                self._result = None
        except (EOFError, KeyboardInterrupt, ValueError):
            self._result = None


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _upload_file(bot: commands.Bot, file_path: str) -> tuple:
    """Upload a local file to the bot owner's DM for a private CDN URL."""
    path = Path(file_path)
    app_info = await bot.application_info()
    owner = app_info.owner
    dm = await owner.create_dm()
    msg = await dm.send(
        content="[REMASTERS] internal file upload — do not delete",
        file=discord.File(str(path))
    )
    cdn_url = msg.attachments[0].url
    await msg.delete()
    return cdn_url, path.name

async def _upload_image(bot: commands.Bot, image_path: str) -> Optional[str]:
    if not image_path:
        return None
    cdn_url, _ = await _upload_file(bot, image_path)
    return cdn_url

async def _get_announcements_channel(bot: commands.Bot) -> Optional[discord.TextChannel]:
    name = CONFIG.get("announcements_channel_name", "announcements")
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name == name:
                return ch
    return None

async def _get_offtopic_channel(bot: commands.Bot) -> Optional[discord.TextChannel]:
    name = CONFIG.get("offtopic_channel_name", "off-topic")
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name == name:
                return ch
    return None

async def _get_releases_role(bot: commands.Bot) -> Optional[discord.Role]:
    name = CONFIG.get("releases_role_name", "Emball Releases")
    for guild in bot.guilds:
        for role in guild.roles:
            if role.name == name:
                return role
    return None

async def _get_info_channel(bot: commands.Bot) -> Optional[discord.TextChannel]:
    name = CONFIG.get("info_channel_name", "info")
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name == name:
                return ch
    return None

def _user_has_releases_role(interaction: discord.Interaction) -> bool:
    member = interaction.guild and interaction.guild.get_member(interaction.user.id)
    if not member:
        return False
    role_name = CONFIG.get("releases_role_name", "Emball Releases")
    return any(r.name == role_name for r in member.roles)

def _user_is_cleared(interaction: discord.Interaction) -> bool:
    try:
        from moderation import is_flagged
        if interaction.guild:
            return not is_flagged(str(interaction.guild.id), str(interaction.user.id))
    except ImportError:
        pass
    return True

def _user_can_download(interaction: discord.Interaction) -> bool:
    return _user_has_releases_role(interaction) and _user_is_cleared(interaction)


# ══════════════════════════════════════════════════════════════════════════════
#  DELIVERY — ephemeral inline audio player → DM fallback
# ══════════════════════════════════════════════════════════════════════════════

async def _deliver_remaster(bot: commands.Bot, interaction: discord.Interaction,
                             version_row: dict) -> None:
    """
    Primary: fetch file from CDN, re-upload as ephemeral attachment (inline audio player).
    Fallback: send CDN URL via DM if upload fails.
    """
    remaster = _db_get_remaster(version_row["remaster_id"])
    if not remaster:
        await interaction.followup.send("Release data not found.", ephemeral=True)
        return

    cdn_url = version_row["cdn_url"]
    filename = version_row["filename"]

    # ── Primary: ephemeral file upload ───────────────────────────────────────
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(cdn_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    file_obj = discord.File(
                        fp=__import__("io").BytesIO(data),
                        filename=filename,
                    )
                    embed = discord.Embed(
                        title=f"⬇  {remaster['title']}  ·  {version_row['version']}",
                        description=remaster.get("description", ""),
                        color=discord.Color.from_rgb(80, 220, 140),
                    )
                    embed.add_field(name="Version", value=f"`{version_row['version']}`", inline=True)
                    embed.add_field(name="File", value=f"`{filename}`", inline=True)
                    if version_row.get("image_cdn_url"):
                        embed.set_thumbnail(url=version_row["image_cdn_url"])
                    await interaction.followup.send(embed=embed, file=file_obj, ephemeral=True)
                    bot.logger.log(MODULE_NAME,
                        f"Ephemeral delivery: '{remaster['title']}' to {interaction.user}")
                    return
    except ImportError:
        bot.logger.log(MODULE_NAME, "aiohttp not available, falling back to DM", "WARNING")
    except discord.HTTPException as e:
        bot.logger.log(MODULE_NAME,
            f"Ephemeral upload failed (HTTP {e.status}), falling back to DM", "WARNING")
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Ephemeral delivery error ({e}), falling back to DM", "WARNING")

    # ── Fallback: DM the CDN URL directly ────────────────────────────────────
    bot.logger.log(MODULE_NAME, f"Falling back to DM for '{remaster['title']}'")
    embed = discord.Embed(
        title=f"⬇  {remaster['title']}  ·  {version_row['version']}",
        description=(
            f"Here's your download link for **{remaster['title']}** "
            f"`{version_row['version']}`.\n\n"
            f"[**Click here to download**]({cdn_url})"
        ),
        color=discord.Color.from_rgb(80, 220, 140),
    )
    embed.add_field(name="File", value=f"`{filename}`", inline=True)
    embed.add_field(name="Version", value=f"`{version_row['version']}`", inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    try:
        dm_ch = await interaction.user.create_dm()
        await dm_ch.send(embed=embed)
        await interaction.followup.send("Check your DMs for the download link!", ephemeral=True)
    except discord.Forbidden:
        bot.logger.log(MODULE_NAME, f"Could not DM {interaction.user} — notifying in off-topic",
                       "WARNING")
        ot_ch = await _get_offtopic_channel(bot)
        if ot_ch:
            await ot_ch.send(
                f"{interaction.user.mention} — I couldn't send you a DM! "
                "Please enable DMs from server members and click **Download** again."
            )
        await interaction.followup.send(
            "I couldn't reach your DMs. Please enable DMs and try again — "
            "I've pinged you in off-topic.",
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ANNOUNCEMENT POSTING
# ══════════════════════════════════════════════════════════════════════════════

async def _post_release(bot: commands.Bot, result: dict) -> None:
    logger = bot.logger
    logger.log(MODULE_NAME, f"Processing new release: {result['title']} {result['version']}")
    try:
        logger.log(MODULE_NAME, "Uploading file to CDN...")
        cdn_url, filename = await _upload_file(bot, result["file_path"])
        image_url = await _upload_image(bot, result.get("image_path"))
        rid = _db_create_remaster(result["title"], result["description"], result["version"])
        vid = _db_add_version(rid, result["version"], cdn_url, filename, image_url)
        remaster = _db_get_remaster(rid)
        version_row = _db_get_version(vid)
        ann_ch = await _get_announcements_channel(bot)
        if not ann_ch:
            logger.log(MODULE_NAME, "announcements channel not found", "WARNING"); return
        role = await _get_releases_role(bot)
        role_mention = role.mention if role else "@Emball Releases"
        embed = _release_embed(remaster, version_row, role_mention=role_mention, is_update=False)
        view = _DownloadView(vid)
        msg = await ann_ch.send(content=role_mention, embed=embed, view=view)
        _db_set_announcement(vid, str(msg.id), str(ann_ch.id))
        # Discussion thread
        try:
            thread_name = f"💬 {remaster['title']} {version_row['version']} — Discussion"
            await msg.create_thread(
                name=thread_name[:100],
                auto_archive_duration=10080,
                reason="Remaster release discussion thread",
            )
            logger.log(MODULE_NAME, f"Discussion thread created for '{remaster['title']}'")
        except discord.Forbidden:
            logger.log(MODULE_NAME, "Missing permissions to create thread", "WARNING")
        except Exception as te:
            logger.log(MODULE_NAME, f"Thread creation failed: {te}", "WARNING")
        logger.log(MODULE_NAME,
            f"Release posted: '{result['title']}' {result['version']} (msg {msg.id})")
    except Exception as e:
        logger.error(MODULE_NAME, "Failed to post release", e)

async def _post_new_version(bot: commands.Bot, result: dict) -> None:
    logger = bot.logger
    rid = result["remaster_id"]
    remaster = _db_get_remaster(rid)
    if not remaster:
        logger.log(MODULE_NAME, f"Remaster {rid} not found", "ERROR"); return
    old_versions = _db_versions_for(rid)
    logger.log(MODULE_NAME, f"Adding version {result['version']} to {remaster['title']}")
    try:
        cdn_url, filename = await _upload_file(bot, result["file_path"])
        image_url = await _upload_image(bot, result.get("image_path"))
        vid = _db_add_version(rid, result["version"], cdn_url, filename, image_url)
        remaster = _db_get_remaster(rid)
        version_row = _db_get_version(vid)
        ann_ch = await _get_announcements_channel(bot)
        if not ann_ch:
            logger.log(MODULE_NAME, "announcements channel not found", "WARNING"); return
        embed = _release_embed(remaster, version_row, is_update=True)
        view = _DownloadView(vid)
        msg = await ann_ch.send(embed=embed, view=view)
        _db_set_announcement(vid, str(msg.id), str(ann_ch.id))
        for old_ver in old_versions:
            if old_ver.get("announcement_msg_id") and old_ver.get("announcement_ch_id"):
                try:
                    ch = bot.get_channel(int(old_ver["announcement_ch_id"]))
                    if ch:
                        old_msg = await ch.fetch_message(int(old_ver["announcement_msg_id"]))
                        if old_msg.embeds:
                            await old_msg.edit(
                                embed=_outdated_embed(old_msg.embeds[0], result["version"]))
                except Exception as edit_err:
                    logger.log(MODULE_NAME,
                        f"Couldn't update old embed for version {old_ver['version']}: {edit_err}",
                        "WARNING")
        logger.log(MODULE_NAME, f"Version {result['version']} posted for {remaster['title']}")
    except Exception as e:
        logger.error(MODULE_NAME, "Failed to post new version", e)

async def _apply_meta_edit(bot: commands.Bot, result: dict) -> None:
    _db_update_remaster_meta(result["remaster_id"], result["title"], result["description"])
    bot.logger.log(MODULE_NAME, f"Metadata updated for remaster {result['remaster_id']}")


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD BUTTON  (on announcement embeds)
# ══════════════════════════════════════════════════════════════════════════════

class _DownloadView(discord.ui.View):
    def __init__(self, version_id: str):
        super().__init__(timeout=None)
        self._vid = version_id
        btn = discord.ui.Button(
            label="Download",
            style=discord.ButtonStyle.primary,
            emoji="⬇",
            custom_id=f"remaster_dl:{version_id}",
        )
        btn.callback = self._on_download
        self.add_item(btn)

    async def _on_download(self, interaction: discord.Interaction):
        if not _user_can_download(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        version_row = _db_get_version(self._vid)
        if not version_row:
            await interaction.response.send_message("Release not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _deliver_remaster(interaction.client, interaction, version_row)


# ══════════════════════════════════════════════════════════════════════════════
#  REMASTER NAVIGATOR  (pinned in #info)
# ══════════════════════════════════════════════════════════════════════════════

class RemasterNavigatorView(discord.ui.View):
    """
    Ephemeral per-user navigator. Shows all releases newest-first in a select menu.
    Selecting a release shows its details and a Download button.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self._bot = bot
        self._remasters = _db_all_remasters()  # newest first
        self._selected_rid: Optional[str] = None
        self._page = 0
        self._render_list()

    # ── Renderers ─────────────────────────────────────────────────────────────

    def _render_list(self):
        self.clear_items()
        total = len(self._remasters)
        if total == 0:
            return
        page_size = 25
        start = self._page * page_size
        end = min(start + page_size, total)
        has_prev = self._page > 0
        has_next = end < total
        opts = []
        if has_prev:
            opts.append(discord.SelectOption(label="Previous page", value="__prev__", emoji="◀"))
        for rem in self._remasters[start:end]:
            opts.append(discord.SelectOption(
                label=rem["title"][:100],
                value=rem["id"],
                description=f"v{rem['latest_version']}  ·  {rem['updated_at'][:10]}",
            ))
        if has_next:
            opts.append(discord.SelectOption(label="Next page", value="__next__", emoji="▶"))
        pages = (total + page_size - 1) // page_size
        ph = f"Choose a release… (page {self._page + 1}/{pages})" if pages > 1 \
            else "Choose a release…"
        sel = discord.ui.Select(placeholder=ph[:150], options=opts, custom_id="rmnav_list")
        sel.callback = self._on_select
        self.add_item(sel)

    def _render_detail(self, remaster: dict, version_row: dict):
        self.clear_items()
        # Download button
        dl_btn = discord.ui.Button(
            label="Download",
            style=discord.ButtonStyle.success,
            emoji="⬇",
            custom_id=f"rmnav_dl:{version_row['id']}",
        )
        dl_btn.callback = self._on_download
        self.add_item(dl_btn)
        # Back button
        back = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="rmnav_back",
            emoji="←",
        )
        back.callback = self._on_back
        self.add_item(back)

    def _list_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎛️  Emball Remaster Archive",
            color=discord.Color.from_rgb(255, 215, 80),
        )
        embed.description = (
            f"**{len(self._remasters)} release{'s' if len(self._remasters) != 1 else ''}** available.\n"
            "Select a title from the menu below."
        )
        embed.set_footer(text="Emball Remasters  ·  Files delivered as inline audio")
        return embed

    def _detail_embed(self, remaster: dict, version_row: dict) -> discord.Embed:
        embed = discord.Embed(
            title=f"✦  {remaster['title']}",
            description=remaster.get("description", ""),
            color=discord.Color.from_rgb(255, 215, 80),
        )
        embed.add_field(name="Latest Version", value=f"`{remaster['latest_version']}`", inline=True)
        embed.add_field(name="File", value=f"`{version_row['filename']}`", inline=True)
        embed.add_field(name="Released", value=version_row["created_at"][:10], inline=True)
        if version_row.get("image_cdn_url"):
            embed.set_image(url=version_row["image_cdn_url"])
        embed.set_footer(text="Click Download to listen — delivered privately, only you can see it")
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_select(self, interaction: discord.Interaction):
        if not _user_can_download(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        val = interaction.data["values"][0]
        if val == "__prev__":
            self._page = max(0, self._page - 1)
            self._render_list()
            await interaction.response.edit_message(embed=self._list_embed(), view=self)
            return
        if val == "__next__":
            self._page += 1
            self._render_list()
            await interaction.response.edit_message(embed=self._list_embed(), view=self)
            return
        self._selected_rid = val
        remaster = _db_get_remaster(val)
        if not remaster:
            await interaction.response.send_message("Release not found.", ephemeral=True)
            return
        version_row = _db_latest_version(val)
        if not version_row:
            await interaction.response.send_message("No version available.", ephemeral=True)
            return
        self._render_detail(remaster, version_row)
        await interaction.response.edit_message(
            embed=self._detail_embed(remaster, version_row), view=self)

    async def _on_download(self, interaction: discord.Interaction):
        if not _user_can_download(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        # Extract version_id from custom_id
        cid = interaction.data.get("custom_id", "")
        vid = cid.split(":", 1)[1] if ":" in cid else None
        if not vid:
            await interaction.response.send_message("Release not found.", ephemeral=True)
            return
        version_row = _db_get_version(vid)
        if not version_row:
            await interaction.response.send_message("Release not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _deliver_remaster(self._bot, interaction, version_row)

    async def _on_back(self, interaction: discord.Interaction):
        self._selected_rid = None
        self._render_list()
        await interaction.response.edit_message(embed=self._list_embed(), view=self)

    async def on_timeout(self):
        self.clear_items()


# ══════════════════════════════════════════════════════════════════════════════
#  INFO EMBED  (pinned in #info)
# ══════════════════════════════════════════════════════════════════════════════

def _build_remaster_info_embed() -> discord.Embed:
    remasters = _db_all_remasters()
    count = len(remasters)
    latest = remasters[0] if remasters else None
    embed = discord.Embed(
        title="🎛️  Emball Remaster Archive",
        description=(
            "Browse and download Emball's remaster releases.\n\n"
            "Use the **Browse Remasters** button below to pick a release and listen "
            "privately — only you can see the file.\n\n"
            "New releases are announced in #announcements with a Download button."
        ),
        color=discord.Color.from_rgb(255, 215, 80),
    )
    embed.add_field(name="Total Releases", value=str(count), inline=True)
    if latest:
        embed.add_field(name="Latest", value=latest["title"], inline=True)
        embed.add_field(name="Version", value=f"`{latest['latest_version']}`", inline=True)
    embed.set_footer(text="Emball Remasters  ·  Requires Emball Releases role")
    return embed


class _RemastersInfoView(discord.ui.View):
    """Persistent launcher pinned in #info."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self._bot = bot
        btn = discord.ui.Button(
            label="Browse Remasters",
            style=discord.ButtonStyle.primary,
            emoji="🎛️",
            custom_id="remasters_browse_v1",
        )
        btn.callback = self._on_browse
        self.add_item(btn)

    async def _on_browse(self, interaction: discord.Interaction):
        if not _user_can_download(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        remasters = _db_all_remasters()
        if not remasters:
            await interaction.response.send_message(
                "No releases yet — check back soon!", ephemeral=True)
            return
        nav = RemasterNavigatorView(self._bot)
        await interaction.response.send_message(
            embed=nav._list_embed(), view=nav, ephemeral=True)


async def post_or_refresh_info_embed(bot: commands.Bot, force: bool = False) -> None:
    """Post (or refresh) the remaster info embed in #info."""
    info_ch = await _get_info_channel(bot)
    if not info_ch:
        bot.logger.log(MODULE_NAME, "info channel not found — skipping remaster embed post", "WARNING")
        return
    embed = _build_remaster_info_embed()
    view = _RemastersInfoView(bot)
    existing_id = CONFIG.get("info_embed_msg_id")
    if existing_id and not force:
        try:
            await info_ch.fetch_message(int(existing_id))
            bot.logger.log(MODULE_NAME, "Remaster info embed already present, skipping")
            return
        except (discord.NotFound, discord.Forbidden):
            pass
    try:
        msg = await info_ch.send(embed=embed, view=view)
        _save_config({"info_embed_msg_id": str(msg.id)})
        bot.logger.log(MODULE_NAME, f"Remaster info embed posted (msg {msg.id})")
    except discord.Forbidden:
        bot.logger.log(MODULE_NAME, "Missing permissions to post in #info", "WARNING")


# ══════════════════════════════════════════════════════════════════════════════
#  CONSOLE COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════════════════

def _make_console_handler(bot: commands.Bot):
    async def handle_remaster(args: str):
        args = args.strip()
        if args in ("", "new"):
            loop = asyncio.get_event_loop()
            tui = _RemasterTUI(bot)
            result = await loop.run_in_executor(None, tui.run_new_release)
            if result is None:
                print("  Cancelled.\n"); return
            if result["action"] == "new":
                print(f"  Posting release: {result['title']} {result['version']}...")
                await _post_release(bot, result)
                print("  Done.\n")
        elif args == "manage":
            loop = asyncio.get_event_loop()
            tui = _RemasterTUI(bot)
            result = await loop.run_in_executor(None, tui.run_manage)
            if result is None:
                print("  Cancelled.\n"); return
            if result["action"] == "add_version":
                print(f"  Posting new version {result['version']}...")
                await _post_new_version(bot, result)
                print("  Done.\n")
            elif result["action"] == "edit_meta":
                await _apply_meta_edit(bot, result)
                print("  Metadata updated.\n")
        elif args == "list":
            remasters = _db_all_remasters()
            if not remasters:
                print("  No releases yet.\n"); return
            print(f"\n  {'Title':<40} {'Version':<14} {'Updated'}")
            print("  " + "─" * 72)
            for r in remasters:
                print(f"  {r['title']:<40} {r['latest_version']:<14} {r['updated_at'][:10]}")
            print()
        else:
            print("  Usage:  /remaster          — publish new release")
            print("          /remaster manage    — manage existing releases")
            print("          /remaster list      — list all releases\n")
    return handle_remaster


# ══════════════════════════════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup(bot: commands.Bot):
    global CONFIG
    CONFIG = _load_config()
    _init_db()

    # Register persistent views so buttons survive restarts
    bot.add_view(_RemastersInfoView(bot))

    # Persistent handler for announcement embed Download buttons
    @bot.listen("on_interaction")
    async def _on_interaction(interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id", "")
        if not cid.startswith("remaster_dl:"):
            return
        if not _user_can_download(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        version_id = cid.split(":", 1)[1]
        version_row = _db_get_version(version_id)
        if not version_row:
            await interaction.response.send_message("Release not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _deliver_remaster(bot, interaction, version_row)

    # Auto-post info embed on ready
    @bot.listen("on_ready")
    async def _remasters_on_ready():
        await post_or_refresh_info_embed(bot, force=False)

    # Console: /remaster
    bot.console_commands["remaster"] = {
        "description": "Publish or manage remaster releases  (subcommands: manage, list)",
        "handler": _make_console_handler(bot),
    }

    # Console: postinfo_remasters
    async def handle_postinfo_remasters(_args: str):
        bot.logger.log(MODULE_NAME, "Force-refreshing remaster info embed...")
        await post_or_refresh_info_embed(bot, force=True)
        print("  Remaster info embed refreshed.\n")

    bot.console_commands["postinfo_remasters"] = {
        "description": "Force-refresh the remaster browse embed in #info",
        "handler": handle_postinfo_remasters,
    }

    bot.logger.log(MODULE_NAME, "Remasters module loaded.")
