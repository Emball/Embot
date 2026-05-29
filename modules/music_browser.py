"""
Interactive archive browser — single V2 LayoutView panel in #info-test.
Format select lives in an ActionRow inside the panel. Album/song selects
edit the same ephemeral in place at each step.
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


def _load_panel_id() -> str | None:
    try:
        with open(STATE_PATH) as f:
            return json.load(f).get("panel_msg_id")
    except Exception:
        return None

def _save_panel_id(msg_id: str):
    atomic_json_write(STATE_PATH, {"panel_msg_id": msg_id})

def _is_fed(interaction: discord.Interaction) -> bool:
    try:
        from mod_suspicion import is_flagged
        if interaction.guild:
            return is_flagged(str(interaction.guild.id), str(interaction.user.id))
    except Exception:
        pass
    return False


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_categories(fmt: str) -> list[str]:
    from music_archive import _db_conn
    with _db_conn() as c:
        rows = c.execute(
            "SELECT DISTINCT category FROM song_index WHERE format=? ORDER BY category",
            (fmt,)
        ).fetchall()
    return [r[0] for r in rows]

def _get_songs(fmt: str, category: str) -> list[dict]:
    from music_archive import _db_conn
    with _db_conn() as c:
        rows = c.execute(
            "SELECT file_path, original_title FROM song_index "
            "WHERE format=? AND category=? ORDER BY original_title",
            (fmt, category)
        ).fetchall()
    return [{"path": r[0], "title": r[1]} for r in rows]


# ── Persistent panel — full V2 LayoutView with ActionRow select ───────────────

class BrowserPanelView(ui.LayoutView):
    """Persistent panel message. Format select lives inside an ActionRow in V2."""

    format_row: ui.ActionRow["BrowserPanelView"] = ui.ActionRow()

    @format_row.select(
        placeholder="Select a format…",
        options=[
            discord.SelectOption(label="FLAC", description="Lossless — highest quality"),
            discord.SelectOption(label="MP3",  description="Compressed — smaller files"),
        ],
        custom_id="browser:format",
    )
    async def format_select(self, interaction: discord.Interaction, select: ui.Select):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "Something went wrong loading the archive. Try again later.", ephemeral=True)
            return
        fmt = select.values[0]
        categories = await asyncio.get_event_loop().run_in_executor(None, _get_categories, fmt)
        if not categories:
            await interaction.response.send_message(
                "No songs indexed yet — try again after the archive loads.", ephemeral=True)
            return
        view = AlbumSelectView(fmt, categories)
        await interaction.response.send_message(
            f"**{fmt}** — pick an album:", view=view, ephemeral=True)

    def build(self) -> "BrowserPanelView":
        self.add_item(ui.Container(
            ui.TextDisplay(
                "## 📂 Archive Browser\n"
                "Browse and download songs directly from Eminem's archive.\n\n"
                "**How to use**\n"
                "Pick a format below — then choose an album and song. "
                "Your download link appears just for you."
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            self.format_row,
            ui.Separator(spacing=discord.SeparatorSpacing.small, visible=False),
            ui.TextDisplay("-# Use `/archive [format] [song]` for direct lookup by name."),
            accent_color=0x1a1a2e,
        ))
        return self


# ── Ephemeral album + song selects — edit in place ────────────────────────────

class AlbumSelectView(ui.View):
    def __init__(self, fmt: str, categories: list[str]):
        super().__init__(timeout=120)
        self.fmt = fmt
        chunks = [categories[i:i+25] for i in range(0, len(categories), 25)]
        for chunk_idx, chunk in enumerate(chunks[:5]):
            options = [discord.SelectOption(label=c[:100], value=c) for c in chunk]
            placeholder = f"Pick an album… ({chunk_idx*25+1}–{chunk_idx*25+len(chunk)})" if len(chunks) > 1 else "Pick an album…"
            sel = ui.Select(placeholder=placeholder, options=options, row=chunk_idx)
            sel.callback = self._make_callback(fmt)
            self.add_item(sel)

    def _make_callback(self, fmt: str):
        async def callback(interaction: discord.Interaction):
            category = interaction.data["values"][0]
            songs = await asyncio.get_event_loop().run_in_executor(None, _get_songs, fmt, category)
            if not songs:
                await interaction.response.edit_message(content="No songs found in that album.", view=None)
                return
            view = SongSelectView(fmt, category, songs)
            label = category[:60] + ("…" if len(category) > 60 else "")
            await interaction.response.edit_message(
                content=f"**{fmt} / {label}** — pick a song:", view=view)
        return callback


class SongSelectView(ui.View):
    def __init__(self, fmt: str, category: str, songs: list[dict]):
        super().__init__(timeout=120)
        chunks = [songs[i:i+25] for i in range(0, len(songs), 25)]
        for chunk_idx, chunk in enumerate(chunks[:5]):
            options = [
                discord.SelectOption(label=s["title"][:100], value=s["path"])
                for s in chunk
            ]
            placeholder = f"Pick a song… ({chunk_idx*25+1}–{chunk_idx*25+len(chunk)})" if len(chunks) > 1 else "Pick a song…"
            sel = ui.Select(placeholder=placeholder, options=options, row=chunk_idx)
            sel.callback = self._make_callback(fmt)
            self.add_item(sel)

    def _make_callback(self, fmt: str):
        async def callback(interaction: discord.Interaction):
            file_path = interaction.data["values"][0]
            await interaction.response.edit_message(content="Fetching…", view=None)
            await _deliver(interaction, fmt, file_path)
        return callback


async def _deliver(interaction: discord.Interaction, fmt: str, file_path: str):
    from music_archive import _cache_lookup, _get_or_upload_cache, _log_delivery, LARGE_FILE_MSG
    bot = interaction.client
    p = Path(file_path)

    url = await _get_or_upload_cache(bot, file_path)
    if url == "FILE_TOO_LARGE":
        await interaction.edit_original_response(content=LARGE_FILE_MSG)
        return
    if not url:
        await interaction.edit_original_response(content="Failed to retrieve that song.")
        return

    entry = _cache_lookup(file_path)
    note = "\n-# Served transcoded — source exceeds Discord's upload limit" if entry and entry.get("transcoded") else ""
    await interaction.edit_original_response(content=f"[{p.name}]({url}){note}")

    candidate = {"path": file_path, "original_title": p.stem, "metadata": {"title": p.stem}}
    await _log_delivery(bot, interaction.user, candidate, source="browser")
    bot.logger.log(MODULE_NAME, f"Delivered '{p.name}' via browser to {interaction.user}")


# ── Panel post + setup ────────────────────────────────────────────────────────

async def _post_panel(bot, channel) -> discord.Message:
    panel_id = _load_panel_id()
    if panel_id:
        try:
            old = await channel.fetch_message(int(panel_id))
            await old.delete()
        except Exception:
            pass
    view = BrowserPanelView(timeout=None)
    view.build()
    msg = await channel.send(view=view)
    _save_panel_id(str(msg.id))
    return msg


def setup(bot):
    from discord import app_commands
    from mod_core import is_owner

    bot.logger.log(MODULE_NAME, "Setting up music browser")

    # Register persistent view so the format select callback survives restarts
    persistent = BrowserPanelView(timeout=None)
    persistent.build()
    bot.add_view(persistent)

    async def _init():
        await bot.wait_until_ready()
        mgr = getattr(bot, "ARCHIVE_manager", None)
        if mgr:
            try:
                await asyncio.wait_for(mgr.song_index_ready.wait(), timeout=60)
            except asyncio.TimeoutError:
                bot.logger.log(MODULE_NAME, "Archive index not ready after 60s", "WARNING")

        chan = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
        if not chan:
            bot.logger.log(MODULE_NAME, f"#{CHANNEL_NAME} not found — create it to enable the browser", "WARNING")
            return

        await _post_panel(bot, chan)
        bot.logger.log(MODULE_NAME, f"Browser panel live in #{CHANNEL_NAME}")

    asyncio.create_task(_init())

    @bot.tree.command(name="browse", description="Browse the archive interactively")
    async def browse(interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "Something went wrong loading the archive. Try again later.", ephemeral=True)
            return
        mgr = getattr(bot, "ARCHIVE_manager", None)
        if not mgr or not mgr.song_index_ready.is_set():
            await interaction.response.send_message(
                "Archive is still loading — try again in a moment.", ephemeral=True)
            return
        # Reuse the same flow as the panel — send a fresh ephemeral format select
        view = ui.View(timeout=120)
        sel = ui.Select(
            placeholder="Select a format…",
            options=[
                discord.SelectOption(label="FLAC", description="Lossless — highest quality"),
                discord.SelectOption(label="MP3",  description="Compressed — smaller files"),
            ],
        )
        async def _fmt_callback(inter: discord.Interaction):
            fmt = inter.data["values"][0]
            categories = await asyncio.get_event_loop().run_in_executor(None, _get_categories, fmt)
            if not categories:
                await inter.response.edit_message(content="No songs indexed yet.", view=None)
                return
            await inter.response.edit_message(
                content=f"**{fmt}** — pick an album:",
                view=AlbumSelectView(fmt, categories)
            )
        sel.callback = _fmt_callback
        view.add_item(sel)
        await interaction.response.send_message("Pick a format:", view=view, ephemeral=True)

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
        await interaction.followup.send(f"Browser panel refreshed in #{CHANNEL_NAME}.", ephemeral=True)
        bot.logger.log(MODULE_NAME, f"Panel refreshed by {interaction.user}")

    bot.logger.log(MODULE_NAME, "Music browser setup complete")
