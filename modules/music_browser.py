"""
Interactive archive browser — posts a persistent V2 panel to #info-test.
Users pick Format → Album → Song via select menus; song link delivered ephemerally.
"""

import asyncio
import discord
from discord import ui
from pathlib import Path
from _utils import script_dir, atomic_json_write
import json

MODULE_NAME = "MUSIC BROWSER"
CHANNEL_NAME = "info-test"
STATE_PATH = script_dir() / "config" / "music_browser_state.json"

# ── State persistence ─────────────────────────────────────────────────────────

def _load_panel_id() -> str | None:
    try:
        with open(STATE_PATH) as f:
            return json.load(f).get("panel_msg_id")
    except Exception:
        return None

def _save_panel_id(msg_id: str):
    atomic_json_write(STATE_PATH, {"panel_msg_id": msg_id})

# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_categories(fmt: str) -> list[str]:
    """Distinct top-level categories for a format, sorted."""
    from music_archive import _db_conn
    with _db_conn() as c:
        rows = c.execute(
            "SELECT DISTINCT category FROM song_index WHERE format=? ORDER BY category",
            (fmt,)
        ).fetchall()
    return [r[0] for r in rows]

def _get_songs(fmt: str, category: str) -> list[dict]:
    """All songs in a format+category, sorted by original_title."""
    from music_archive import _db_conn
    with _db_conn() as c:
        rows = c.execute(
            "SELECT file_path, original_title, metadata_json FROM song_index "
            "WHERE format=? AND category=? ORDER BY original_title",
            (fmt, category)
        ).fetchall()
    return [{"path": r[0], "title": r[1]} for r in rows]

# ── Panel V2 layout ───────────────────────────────────────────────────────────

def _build_panel_view() -> ui.LayoutView:
    """The static V2 display panel — instructions only, no interactive components."""
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(
            "## 📂 Archive Browser\n"
            "Browse and download songs directly from Eminem's archive.\n\n"
            "**How to use**\n"
            "Use the `/browse` command to open the interactive browser.\n"
            "Pick your format, then album, then song — the download link appears just for you."
        ),
        ui.Separator(spacing=discord.SeparatorSpacing.small),
        ui.TextDisplay("-# Links are ephemeral and expire after a short time. Use `/archive` for direct lookup by name."),
        accent_color=0x1a1a2e,
    ))
    return view

# ── Select menus (V1 — needed for callbacks) ──────────────────────────────────

class FormatSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="① Pick a format…",
            options=[
                discord.SelectOption(label="FLAC", description="Lossless — highest quality", emoji="🎵"),
                discord.SelectOption(label="MP3",  description="Compressed — smaller files",  emoji="🎧"),
            ],
            custom_id="browser:format",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        fmt = self.values[0]
        categories = await asyncio.get_event_loop().run_in_executor(None, _get_categories, fmt)
        if not categories:
            await interaction.response.send_message(
                "No songs indexed yet — try again after the archive loads.", ephemeral=True)
            return
        view = AlbumSelectView(fmt, categories)
        await interaction.response.send_message(
            f"**{fmt}** selected — now pick an album:",
            view=view, ephemeral=True
        )


class AlbumSelectView(ui.View):
    """Ephemeral view with album select, paginated across multiple selects if needed."""

    def __init__(self, fmt: str, categories: list[str]):
        super().__init__(timeout=120)
        self.fmt = fmt
        # Discord allows max 25 options per select; split into pages of 25
        chunks = [categories[i:i+25] for i in range(0, len(categories), 25)]
        for chunk_idx, chunk in enumerate(chunks[:5]):  # max 5 rows
            options = [discord.SelectOption(label=c[:100], value=c) for c in chunk]
            placeholder = f"Pick an album… ({chunk_idx*25+1}–{chunk_idx*25+len(chunk)})" if len(chunks) > 1 else "Pick an album…"
            sel = ui.Select(
                placeholder=placeholder,
                options=options,
                row=chunk_idx,
            )
            sel.callback = self._make_callback(fmt)
            self.add_item(sel)

    def _make_callback(self, fmt: str):
        async def callback(interaction: discord.Interaction):
            category = interaction.data["values"][0]
            songs = await asyncio.get_event_loop().run_in_executor(None, _get_songs, fmt, category)
            if not songs:
                await interaction.response.send_message("No songs found in that album.", ephemeral=True)
                return
            view = SongSelectView(fmt, category, songs)
            label = category[:50] + ("…" if len(category) > 50 else "")
            await interaction.response.send_message(
                f"**{fmt} / {label}** — pick a song:",
                view=view, ephemeral=True
            )
        return callback


class SongSelectView(ui.View):
    """Ephemeral view with song select, paginated if needed."""

    def __init__(self, fmt: str, category: str, songs: list[dict]):
        super().__init__(timeout=120)
        self.fmt = fmt
        self.category = category
        chunks = [songs[i:i+25] for i in range(0, len(songs), 25)]
        for chunk_idx, chunk in enumerate(chunks[:5]):
            options = [
                discord.SelectOption(label=s["title"][:100], value=s["path"])
                for s in chunk
            ]
            placeholder = f"Pick a song… ({chunk_idx*25+1}–{chunk_idx*25+len(chunk)})" if len(chunks) > 1 else "Pick a song…"
            sel = ui.Select(
                placeholder=placeholder,
                options=options,
                row=chunk_idx,
            )
            sel.callback = self._make_callback(fmt)
            self.add_item(sel)

    def _make_callback(self, fmt: str):
        async def callback(interaction: discord.Interaction):
            file_path = interaction.data["values"][0]
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _deliver(interaction, fmt, file_path)
        return callback


async def _deliver(interaction: discord.Interaction, fmt: str, file_path: str):
    from music_archive import (
        _cache_lookup, _cache_refresh_url, _get_or_upload_cache,
        _log_delivery, LARGE_FILE_MSG, _is_fed
    )
    bot = interaction.client

    if _is_fed(interaction):
        await interaction.followup.send("Failed to retrieve song.", ephemeral=True)
        return

    p = Path(file_path)
    cached = _cache_lookup(file_path)
    candidate = {
        "path": file_path,
        "original_title": p.stem,
        "metadata": {"title": p.stem},
    }

    if not cached:
        await interaction.followup.send("Uploading to cache for the first time — this may take a moment…", ephemeral=True)

    url = await _get_or_upload_cache(bot, file_path)
    if url == "FILE_TOO_LARGE":
        await interaction.followup.send(LARGE_FILE_MSG, ephemeral=True)
        return
    if not url:
        await interaction.followup.send("Failed to retrieve that song.", ephemeral=True)
        return

    entry = _cache_lookup(file_path)
    note = "\n-# Served transcoded — source exceeds Discord's upload limit" if entry and entry.get("transcoded") else ""
    await interaction.followup.send(f"[{p.name}]({url}){note}", ephemeral=True)
    await _log_delivery(bot, interaction.user, candidate, source="browser")
    bot.logger.log(MODULE_NAME, f"Delivered '{p.name}' via browser to {interaction.user}")


# ── Persistent panel + /browse command ───────────────────────────────────────

class BrowserPanelView(ui.View):
    """Persistent V1 view attached to the panel message so FormatSelect callbacks survive restarts."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(FormatSelect())


async def _post_panel(bot, channel) -> discord.Message:
    """Delete any existing panel and repost at the bottom."""
    panel_id = _load_panel_id()
    if panel_id:
        try:
            old = await channel.fetch_message(int(panel_id))
            await old.delete()
        except Exception:
            pass

    layout = _build_panel_view()
    msg = await channel.send(view=layout)
    _save_panel_id(str(msg.id))
    return msg


async def _ensure_panel(bot, channel):
    """Make sure the format-select message exists in the channel (separate from V2 panel)."""
    panel_key = "browser_select_msg_id"
    from _utils import script_dir
    select_state_path = script_dir() / "config" / "music_browser_select.json"
    select_msg_id = None
    try:
        with open(select_state_path) as f:
            select_msg_id = json.load(f).get("msg_id")
    except Exception:
        pass

    if select_msg_id:
        try:
            await channel.fetch_message(int(select_msg_id))
            return  # already there
        except Exception:
            pass

    view = BrowserPanelView()
    msg = await channel.send(
        "**Use the menu below to browse the archive:**",
        view=view
    )
    atomic_json_write(select_state_path, {"msg_id": str(msg.id)})


def setup(bot):
    from discord import app_commands
    from mod_core import is_owner

    bot.logger.log(MODULE_NAME, "Setting up music browser")

    # Register persistent view so callbacks survive restart
    bot.add_view(BrowserPanelView())

    async def _init():
        await bot.wait_until_ready()
        # Wait for archive index to be ready before doing anything
        mgr = getattr(bot, "ARCHIVE_manager", None)
        if mgr:
            try:
                await asyncio.wait_for(mgr.song_index_ready.wait(), timeout=60)
            except asyncio.TimeoutError:
                bot.logger.log(MODULE_NAME, "Archive index not ready after 60s — browser will work once it loads", "WARNING")

        chan = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
        if not chan:
            bot.logger.log(MODULE_NAME, f"#{CHANNEL_NAME} not found — create it to enable the browser", "WARNING")
            return

        await _post_panel(bot, chan)
        await _ensure_panel(bot, chan)
        bot.logger.log(MODULE_NAME, f"Browser panel live in #{CHANNEL_NAME}")

    asyncio.create_task(_init())

    @bot.tree.command(name="browse", description="Browse the archive interactively")
    async def browse(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        mgr = getattr(bot, "ARCHIVE_manager", None)
        if not mgr or not mgr.song_index_ready.is_set():
            await interaction.followup.send("Archive is still loading — try again in a moment.", ephemeral=True)
            return
        if _is_fed(interaction):
            await interaction.followup.send("Unable to process request.", ephemeral=True)
            return
        view = ui.View(timeout=120)
        view.add_item(FormatSelect())
        await interaction.followup.send("Pick a format to start browsing:", view=view, ephemeral=True)

    @bot.tree.command(name="refresh_browser", description="[Owner only] Repost the archive browser panel")
    async def refresh_browser(interaction: discord.Interaction):
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        chan = discord.utils.get(interaction.guild.text_channels, name=CHANNEL_NAME)
        if not chan:
            await interaction.response.send_message(f"#{CHANNEL_NAME} not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await _post_panel(bot, chan)
        await _ensure_panel(bot, chan)
        await interaction.followup.send(f"Browser panel refreshed in #{CHANNEL_NAME}.", ephemeral=True)
        bot.logger.log(MODULE_NAME, f"Panel refreshed by {interaction.user}")

    bot.logger.log(MODULE_NAME, "Music browser setup complete")
