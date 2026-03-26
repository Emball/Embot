import os
import re
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from io import BytesIO
import asyncio
import difflib
from discord import app_commands
import discord
from discord.ext import tasks
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

METADATA_EXECUTOR = ThreadPoolExecutor(max_workers=4)
MODULE_NAME = "ARCHIVE"

_cache_lock = asyncio.Lock()

import json as _json

def _load_eminem_root() -> Path:
    env_val = os.environ.get("EMINEM_ROOT")
    if env_val:
        return Path(env_val)
    config_file = Path(__file__).parent.parent / "config" / "archive_config.json"
    if config_file.exists():
        try:
            data = _json.loads(config_file.read_text(encoding="utf-8"))
            if "eminem_root" in data:
                return Path(data["eminem_root"])
        except Exception:
            pass
    raise FileNotFoundError(
        "EMINEM_ROOT is not configured. Set the EMINEM_ROOT environment variable "
        "or add 'eminem_root' to config/archive_config.json."
    )

def _load_archive_config() -> dict:
    config_file = Path(__file__).parent.parent / "config" / "archive_config.json"
    defaults = {
        "info_channel_name": "info",
        "info_embed_msg_id": None,
    }
    if config_file.exists():
        try:
            data = _json.loads(config_file.read_text(encoding="utf-8"))
            defaults.update(data)
        except Exception:
            pass
    return defaults

def _save_archive_config(data: dict) -> None:
    config_file = Path(__file__).parent.parent / "config" / "archive_config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = {}
        if config_file.exists():
            existing = _json.loads(config_file.read_text(encoding="utf-8"))
        existing.update(data)
        tmp = str(config_file) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(config_file))
    except Exception as e:
        print(f"[ARCHIVE] Failed to save config: {e}")

try:
    EMINEM_ROOT = _load_eminem_root()
except FileNotFoundError as _e:
    import sys as _sys
    print(f"[ARCHIVE] WARNING: {_e}", file=_sys.stderr)
    EMINEM_ROOT = Path(".")

FORMATS = ["FLAC", "MP3"]
CACHE_CHANNEL_NAME = "songcache"
INDEX_FILE = str(Path(__file__).parent.parent / "cache" / "archive" / "song_index.json")
CACHE_INDEX = str(Path(__file__).parent.parent / "cache" / "archive" / "cache_index.json")
Path(__file__).parent.parent.joinpath("cache", "archive").mkdir(parents=True, exist_ok=True)
INDEX_REFRESH_HOURS = 24
CACHE_EXPIRE_DAYS = 7
VERSION_KEYWORDS = ['live', 'remix', 'demo', 'acoustic', 'version', 'edit', 'radio']
SPECIAL_FOLDERS = {
    "8 - Features": "Feature",
    "7 - Singles": "Single",
    "10 - Freestyles (MP3 Only)": "Freestyle",
    "11 - Leaks (Mostly MP3)": "Leak"
}
LARGE_FILE_MSG = "Sorry! The song file was too big to upload."
MAX_SEARCH_RESULTS = 5
NAV_PAGE_SIZE = 23  # reserve 2 slots for ◀ ▶ pagination arrows

# ── Folder name cleaner ────────────────────────────────────────────────────────

_FOLDER_CLEAN_RE = re.compile(
    r"^\d+\s*-\s*|\s*\(.*?\)\s*$|\s*\[.*?\]\s*$",
    re.VERBOSE,
)

def _clean_folder_name(raw: str) -> str:
    """'8 - Features' → 'Features', '10 - Freestyles (MP3 Only)' → 'Freestyles'."""
    cleaned = _FOLDER_CLEAN_RE.sub("", raw).strip()
    return cleaned if cleaned else raw


# ══════════════════════════════════════════════════════════════════════════════
#  METADATA / INDEX
# ══════════════════════════════════════════════════════════════════════════════

async def extract_metadata_async(file_path):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(METADATA_EXECUTOR, extract_metadata_sync, file_path)
    except Exception as e:
        print(f"Async metadata error for {file_path}: {e}")
        return None

def extract_metadata_sync(file_path):
    try:
        if file_path.lower().endswith('.flac'):
            audio = FLAC(file_path)
            return {
                'title': audio.get('title', [''])[0],
                'album': audio.get('album', [''])[0],
                'artist': audio.get('artist', [''])[0],
                'year': audio.get('date', [''])[0].split('-')[0] if audio.get('date') else '',
            }
        elif file_path.lower().endswith('.mp3'):
            try:
                audio = MP3(file_path, ID3=ID3)
                tags = audio.tags
                if not tags:
                    return None
                return {
                    'title': tags['TIT2'].text[0] if 'TIT2' in tags else '',
                    'album': tags['TALB'].text[0] if 'TALB' in tags else '',
                    'artist': tags['TPE1'].text[0] if 'TPE1' in tags else '',
                    'year': str(tags['TDRC'].text[0]) if 'TDRC' in tags and tags['TDRC'].text else '',
                }
            except Exception as mp3_error:
                print(f"MP3 metadata error for {file_path}: {mp3_error}")
                return {
                    'title': Path(file_path).stem,
                    'album': 'Unknown',
                    'artist': 'Eminem',
                    'year': ''
                }
    except Exception as e:
        print(f"General metadata error for {file_path}: {e}")
    return None

def extract_artwork(file_path):
    try:
        if file_path.lower().endswith('.flac'):
            audio = FLAC(file_path)
            if audio.pictures:
                return audio.pictures[0].data
        elif file_path.lower().endswith('.mp3'):
            audio = MP3(file_path, ID3=ID3)
            if audio.tags:
                for tag in audio.tags.values():
                    if getattr(tag, "FrameID", None) == 'APIC':
                        return tag.data
    except Exception:
        pass
    return None

def handle_special_folder(file_path, metadata, folder_name):
    if not metadata:
        metadata = {
            'title': Path(file_path).stem,
            'album': folder_name,
            'artist': 'Eminem',
            'year': ''
        }
    for folder_key, folder_type in SPECIAL_FOLDERS.items():
        if folder_key in folder_name:
            metadata['album'] = folder_type
            ym = re.search(r'\((\d{4})\)', folder_name)
            if ym:
                metadata['year'] = ym.group(1)
            if folder_key == "8 - Features":
                feat1 = re.match(r'\((\d{4})\)\s*(.+?)\s*-\s*(.+?)\s*\(feat', metadata.get('title', ''))
                if feat1:
                    metadata['year'], metadata['artist'], metadata['title'] = feat1.groups()
                else:
                    feat2 = re.match(r'(.+?)\s*-\s*(.+?)\s*\(feat', metadata.get('title', ''))
                    if feat2:
                        metadata['artist'], metadata['title'] = feat2.groups()
            elif folder_key in ["7 - Singles", "10 - Freestyles (MP3 Only)"]:
                sm = re.match(r'\((\d{4})\)\s*(.+)', metadata.get('title', ''))
                if sm:
                    metadata['year'], metadata['title'] = sm.groups()
            elif folder_key == "11 - Leaks (Mostly MP3)":
                era = re.search(r'\((\d{4}-\d{4})\)', folder_name)
                if era:
                    metadata['year'] = era.group(1)
                    metadata['album'] = "Leak"
    if not metadata.get('artist') or 'eminem' not in metadata['artist'].lower():
        metadata['artist'] = "Eminem"
    return metadata

def normalize_title(title):
    t = re.sub(r'^(\d+\s*-\s*)?\d+\s+', '', title)
    t = re.sub(r'[({\[].*?[)}\]](?=\s*$)', '', t)
    t = re.sub(r'\b(?:feat\.?|ft\.?|with)\s+.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.casefold()

async def check_file_modifications(bot):
    if not Path(INDEX_FILE).exists():
        return True
    try:
        last_index_time = datetime.fromtimestamp(Path(INDEX_FILE).stat().st_mtime, timezone.utc)
        if datetime.now(timezone.utc) - last_index_time > timedelta(hours=INDEX_REFRESH_HOURS):
            bot.logger.log(MODULE_NAME, f"Index older than {INDEX_REFRESH_HOURS} hours, needs refresh")
            return True
        try:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                index_data = json.load(f)
                songs = index_data.get('songs', {})
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not read index file: {e}", "WARNING")
            return True
        current_file_count = 0
        for fmt in FORMATS:
            fmt_path = EMINEM_ROOT / fmt
            if not fmt_path.exists():
                continue
            for root, _, files in os.walk(fmt_path):
                for file in files:
                    if file.lower().endswith(('.flac', '.mp3')):
                        current_file_count += 1
        index_file_count = 0
        for fmt in FORMATS:
            if fmt in songs:
                for entries in songs[fmt].values():
                    index_file_count += len(entries)
        if current_file_count != index_file_count:
            bot.logger.log(MODULE_NAME,
                f"File count changed: {index_file_count} -> {current_file_count}, rebuilding index")
            return True
        bot.logger.log(MODULE_NAME,
            f"Index up-to-date: {current_file_count} files, "
            f"age: {(datetime.now(timezone.utc) - last_index_time).total_seconds()/3600:.1f}h")
        return False
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Error checking file modifications", e)
        return False

async def build_song_index(bot):
    bot.logger.log(MODULE_NAME, "Building song index...")
    song_index = {fmt: defaultdict(list) for fmt in FORMATS}
    total = 0
    for fmt in FORMATS:
        fmt_path = EMINEM_ROOT / fmt
        if not fmt_path.exists():
            bot.logger.log(MODULE_NAME, f"Format directory missing: {fmt_path}", "WARNING")
            continue
        bot.logger.log(MODULE_NAME, f"Scanning {fmt} directory...")
        all_files = []
        for root, _, files in os.walk(fmt_path):
            folder = Path(root).name
            for fn in files:
                if not fn.lower().endswith(('.flac', '.mp3')):
                    continue
                full_path = Path(root) / fn
                all_files.append((full_path, folder))
        batch_size = 50
        for i in range(0, len(all_files), batch_size):
            batch = all_files[i:i + batch_size]
            batch_tasks = [
                process_single_file(bot, full_path, folder, song_index, fmt)
                for full_path, folder in batch
            ]
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            total += len([r for r in results if r is not None])
            if i + batch_size < len(all_files):
                await asyncio.sleep(0.1)
            bot.logger.log(MODULE_NAME,
                f"Processed {min(i + batch_size, len(all_files))}/{len(all_files)} files...")
    tmp = INDEX_FILE + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({
                'version': 5,
                'created_at': datetime.utcnow().isoformat(),
                'songs': {k: dict(v) for k, v in song_index.items()}
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, INDEX_FILE)
        bot.logger.log(MODULE_NAME, f"Indexed {total} songs total")
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to save index file", e)
        if os.path.exists(tmp):
            os.remove(tmp)
    return song_index

async def process_single_file(bot, full_path, folder, song_index, fmt):
    try:
        md = await extract_metadata_async(str(full_path))
        if any(k in folder for k in SPECIAL_FOLDERS):
            md = handle_special_folder(str(full_path), md, folder)
        if not md:
            md = {'title': full_path.stem, 'album': folder, 'artist': 'Eminem', 'year': ''}
        key = normalize_title(md['title'])
        song_index[fmt][key].append({
            'path': str(full_path),
            'original_title': full_path.stem,
            'folder': folder,
            'metadata': md
        })
        return True
    except Exception as e:
        bot.logger.error(MODULE_NAME, f"Error processing {full_path}", e)
        return None

def load_song_index(bot):
    if not Path(INDEX_FILE).exists():
        bot.logger.log(MODULE_NAME, "Index file not found", "WARNING")
        return None
    try:
        with open(INDEX_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version', 0) < 5:
            bot.logger.log(MODULE_NAME, "Outdated index version", "WARNING")
            return None
        created = datetime.fromisoformat(data['created_at'])
        if datetime.utcnow() - created < timedelta(hours=INDEX_REFRESH_HOURS):
            bot.logger.log(MODULE_NAME, "Loaded index from cache")
            return data['songs']
        bot.logger.log(MODULE_NAME, "Index is outdated, needs refresh")
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Error loading index", e)
    return None

def find_best_match(idx, fmt, query):
    key = normalize_title(query)
    matches = difflib.get_close_matches(key, idx.get(fmt, {}).keys(), n=MAX_SEARCH_RESULTS, cutoff=0.5)
    return matches[0] if matches else None

def select_best_candidate(cands, version=None):
    if version:
        vl = version.lower()
        filtered = [c for c in cands if vl in c['original_title'].lower() or vl in c['folder'].lower()]
        if not filtered:
            return None
        cands = filtered
    scored = []
    for c in cands:
        p = sum(1 for kw in VERSION_KEYWORDS
                if kw in c['original_title'].lower() or kw in c['folder'].lower())
        y = 9999
        try:
            yv = c['metadata'].get('year', '')
            y = int(yv[:4]) if yv and yv[:4].isdigit() else 9999
        except Exception:
            pass
        scored.append((p, y, c))
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2] if scored else None


# ══════════════════════════════════════════════════════════════════════════════
#  CACHE (DM fallback CDN URLs)
# ══════════════════════════════════════════════════════════════════════════════

async def get_cached_url(bot, file_path):
    """Upload to #songcache and return a CDN URL. Used only as DM fallback."""
    p = Path(file_path)
    key = str(p.resolve())
    async with _cache_lock:
        try:
            with open(CACHE_INDEX, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except FileNotFoundError:
            cache = {}
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Error loading cache index", e)
            cache = {}
        now = datetime.utcnow()
        if key in cache:
            cache_time = datetime.fromisoformat(cache[key]['timestamp'])
            if now - cache_time < timedelta(days=CACHE_EXPIRE_DAYS):
                bot.logger.log(MODULE_NAME, f"Using cached URL for {p.name}")
                return cache[key]['url']
        chan = discord.utils.get(bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
        if not chan:
            bot.logger.log(MODULE_NAME, f"Missing channel {CACHE_CHANNEL_NAME}", "WARNING")
            return None
        if not p.exists():
            bot.logger.error(MODULE_NAME, f"File not found: {file_path}")
            return None
        try:
            bot.logger.log(MODULE_NAME, f"Uploading {p.name} to cache channel")
            mf = discord.File(p, filename=p.name)
            msg = await chan.send(file=mf)
            url = msg.attachments[0].url
            cache[key] = {'url': url, 'timestamp': now.isoformat(), 'message_id': msg.id}
            try:
                tmp = CACHE_INDEX + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CACHE_INDEX)
                bot.logger.log(MODULE_NAME, f"Cached new URL for {p.name}")
            except Exception as e:
                bot.logger.error(MODULE_NAME, "Failed to save cache index", e)
            return url
        except discord.HTTPException as e:
            if e.status == 413:
                bot.logger.log(MODULE_NAME, f"File too large: {p.name}", "WARNING")
                return "FILE_TOO_LARGE"
            bot.logger.error(MODULE_NAME, "Upload failed", e)
            return None
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Unexpected upload error", e)
            return None


# ══════════════════════════════════════════════════════════════════════════════
#  DELIVERY  — ephemeral direct upload → DM cache fallback
# ══════════════════════════════════════════════════════════════════════════════

async def _deliver_song(bot, interaction: discord.Interaction, candidate: dict) -> None:
    """
    Primary: upload file as ephemeral reply with Components v2 layout.
    Fallback: CDN-cached URL sent via DM.
    """
    p = Path(candidate['path'])
    md = candidate['metadata']

    # ── Primary: ephemeral direct file upload ────────────────────────────────
    try:
        if p.exists():
            bot.logger.log(MODULE_NAME, f"Delivering '{p.name}' via ephemeral upload")
            art_bytes = extract_artwork(str(p))

            title = md.get('title', p.stem)
            artist = md.get('artist', 'Eminem')
            album = md.get('album', '')
            year = md.get('year', '')

            # Build metadata line
            meta_parts = [f"**{artist}**"]
            if album:
                meta_parts.append(album)
            if year:
                meta_parts.append(year)
            meta_line = "  ·  ".join(meta_parts)

            if art_bytes:
                view = discord.ui.LayoutView(timeout=None)
                section = discord.ui.Section(
                    discord.ui.TextDisplay(f"## {title}"),
                    discord.ui.TextDisplay(meta_line),
                    accessory=discord.ui.Thumbnail(
                        "attachment://cover.jpg",
                        description=f"Cover art for {title}",
                    ),
                )
                view.add_item(section)
                await interaction.followup.send(
                    view=view,
                    file=discord.File(BytesIO(art_bytes), filename="cover.jpg"),
                    ephemeral=True,
                )
            else:
                view = discord.ui.LayoutView(timeout=None)
                view.add_item(discord.ui.TextDisplay(f"## {title}"))
                view.add_item(discord.ui.TextDisplay(meta_line))
                await interaction.followup.send(view=view, ephemeral=True)

            # Send the audio file as a plain ephemeral so it actually appears
            await interaction.followup.send(
                file=discord.File(str(p), filename=p.name),
                ephemeral=True,
            )

            bot.logger.log(MODULE_NAME, f"Ephemeral delivery succeeded for '{p.name}'")
            return
    except discord.HTTPException as e:
        bot.logger.log(MODULE_NAME,
            f"Ephemeral upload failed (HTTP {e.status}), falling back to DM cache", "WARNING")
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Ephemeral delivery error ({e}), falling back to DM", "WARNING")

    # ── Fallback: DM via cache ────────────────────────────────────────────────
    bot.logger.log(MODULE_NAME, f"Falling back to DM cache delivery for '{p.name}'")
    url = await get_cached_url(bot, str(p))
    if url == "FILE_TOO_LARGE":
        await interaction.followup.send(LARGE_FILE_MSG, ephemeral=True)
        return
    if not url:
        await interaction.followup.send("❌ Failed to retrieve song.", ephemeral=True)
        return

    art_bytes = extract_artwork(str(p))
    title = md.get('title', 'Unknown Track')
    artist = md.get('artist', 'Eminem')
    album = md.get('album', '')
    year = md.get('year', '')

    meta_parts = [f"**{artist}**"]
    if album:
        meta_parts.append(album)
    if year:
        meta_parts.append(year)
    meta_line = "  ·  ".join(meta_parts)

    try:
        user = interaction.user
        if art_bytes:
            try:
                fobj = discord.File(BytesIO(art_bytes), filename="cover.jpg")
                dm_view = discord.ui.LayoutView(timeout=None)
                section = discord.ui.Section(
                    discord.ui.TextDisplay(f"## {title}"),
                    discord.ui.TextDisplay(meta_line),
                    discord.ui.TextDisplay(f"[Download]({url})"),
                    accessory=discord.ui.Thumbnail(
                        "attachment://cover.jpg",
                        description=f"Cover art for {title}",
                    ),
                )
                dm_view.add_item(section)
                dm_view.add_item(discord.ui.TextDisplay("-# Link expires when the cache purges"))
                await user.send(file=fobj, view=dm_view)
            except Exception:
                # Fallback DM without artwork
                dm_view = discord.ui.LayoutView(timeout=None)
                dm_view.add_item(discord.ui.TextDisplay(f"## {title}"))
                dm_view.add_item(discord.ui.TextDisplay(meta_line))
                dm_view.add_item(discord.ui.TextDisplay(f"[Download]({url})"))
                dm_view.add_item(discord.ui.TextDisplay("-# Link expires when the cache purges"))
                await user.send(view=dm_view)
        else:
            dm_view = discord.ui.LayoutView(timeout=None)
            dm_view.add_item(discord.ui.TextDisplay(f"## {title}"))
            dm_view.add_item(discord.ui.TextDisplay(meta_line))
            dm_view.add_item(discord.ui.TextDisplay(f"[Download]({url})"))
            dm_view.add_item(discord.ui.TextDisplay("-# Link expires when the cache purges"))
            await user.send(view=dm_view)
        await interaction.followup.send("Check your DMs for the song link!", ephemeral=True)
    except discord.Forbidden:
        offtopic = discord.utils.get(bot.get_all_channels(), name="off-topic")
        if offtopic:
            await offtopic.send(
                f"{interaction.user.mention} — I couldn't send you a DM! "
                "Please enable DMs from server members and try again."
            )
        await interaction.followup.send(
            "I couldn't reach your DMs. Please enable DMs and try again — "
            "I've pinged you in off-topic.",
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  FED CHECK
# ══════════════════════════════════════════════════════════════════════════════

def _is_fed(interaction: discord.Interaction) -> bool:
    try:
        from moderation import is_flagged as _check
        if interaction.guild:
            return _check(str(interaction.guild.id), str(interaction.user.id))
    except Exception:
        pass
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  NAVIGATOR — per-user ephemeral multi-step LayoutView
# ══════════════════════════════════════════════════════════════════════════════

def _get_folders_for_format(song_index: dict, fmt: str) -> list:
    folders = set()
    for entries in song_index.get(fmt, {}).values():
        for e in entries:
            folders.add(e['folder'])
    return sorted(folders)

def _get_songs_in_folder(song_index: dict, fmt: str, folder: str) -> list:
    songs = []
    for entries in song_index.get(fmt, {}).values():
        for e in entries:
            if e['folder'] == folder:
                songs.append(e)
    songs.sort(key=lambda s: s['metadata'].get('title', s['original_title']).lower())
    return songs

def _folder_options_for_page(folders: list, page: int):
    total = len(folders)
    if total <= 25:
        return (
            [
                discord.SelectOption(label=_clean_folder_name(f)[:100], value=str(i))
                for i, f in enumerate(folders)
            ],
            False, False,
        )
    start = page * NAV_PAGE_SIZE
    end = min(start + NAV_PAGE_SIZE, total)
    has_prev = page > 0
    has_next = end < total
    opts = []
    if has_prev:
        opts.append(discord.SelectOption(label="◀  Previous page", value="__prev__"))
    for i, f in enumerate(folders[start:end], start=start):
        opts.append(discord.SelectOption(label=_clean_folder_name(f)[:100], value=str(i)))
    if has_next:
        opts.append(discord.SelectOption(label="Next page  ▶", value="__next__"))
    return opts, has_prev, has_next

def _song_options_for_page(songs: list, page: int):
    total = len(songs)
    if total <= 25:
        return (
            [
                discord.SelectOption(
                    label=s['metadata'].get('title', s['original_title'])[:100],
                    value=str(i),
                    description=(s['metadata'].get('year', '') or '')[:50] or None,
                )
                for i, s in enumerate(songs)
            ],
            False, False,
        )
    start = page * NAV_PAGE_SIZE
    end = min(start + NAV_PAGE_SIZE, total)
    has_prev = page > 0
    has_next = end < total
    opts = []
    if has_prev:
        opts.append(discord.SelectOption(label="◀  Previous page", value="__prev__"))
    for i, s in enumerate(songs[start:end], start=start):
        opts.append(discord.SelectOption(
            label=s['metadata'].get('title', s['original_title'])[:100],
            value=str(i),
            description=(s['metadata'].get('year', '') or '')[:50] or None,
        ))
    if has_next:
        opts.append(discord.SelectOption(label="Next page  ▶", value="__next__"))
    return opts, has_prev, has_next


class ArchiveNavigatorView(discord.ui.LayoutView):
    """
    Ephemeral multi-step navigator using Components v2.
    format_select → folder_select (paginated only if >25) → song_select → deliver
    """

    def __init__(self, bot, song_index: dict):
        super().__init__(timeout=300)
        self._bot = bot
        self._index = song_index
        self._fmt: Optional[str] = None
        self._folder: Optional[str] = None
        self._folders: list = []
        self._songs: list = []
        self._folder_page = 0
        self._song_page = 0
        self._render_format_step()

    # ── Step renderers ────────────────────────────────────────────────────────

    def _render_format_step(self):
        self.clear_items()
        self.add_item(discord.ui.TextDisplay("## Eminem Archive\nChoose a format to browse:"))
        row = discord.ui.ActionRow()
        sel = discord.ui.Select(
            placeholder="Choose a format…",
            options=[discord.SelectOption(label=fmt, value=fmt) for fmt in FORMATS],
            custom_id="nav_fmt",
        )
        sel.callback = self._on_format
        row.add_item(sel)
        self.add_item(row)

    def _render_folder_step(self):
        self.clear_items()
        opts, has_prev, has_next = _folder_options_for_page(self._folders, self._folder_page)
        pages = (len(self._folders) + NAV_PAGE_SIZE - 1) // NAV_PAGE_SIZE
        page_hint = f"  ·  page {self._folder_page + 1}/{pages}" if pages > 1 else ""
        self.add_item(discord.ui.TextDisplay(
            f"## Eminem Archive  ·  {self._fmt}\n"
            f"{len(self._folders)} folders available{page_hint}"
        ))
        row = discord.ui.ActionRow()
        ph = f"Choose a folder…{page_hint}"
        sel = discord.ui.Select(placeholder=ph[:150], options=opts, custom_id="nav_folder")
        sel.callback = self._on_folder
        row.add_item(sel)
        self.add_item(row)
        back_row = discord.ui.ActionRow()
        back = discord.ui.Button(label="← Back to Format", style=discord.ButtonStyle.secondary,
                                 custom_id="nav_b_fmt")
        back.callback = self._on_back_to_format
        back_row.add_item(back)
        self.add_item(back_row)

    def _render_song_step(self):
        self.clear_items()
        opts, has_prev, has_next = _song_options_for_page(self._songs, self._song_page)
        pages = (len(self._songs) + NAV_PAGE_SIZE - 1) // NAV_PAGE_SIZE
        page_hint = f"  ·  page {self._song_page + 1}/{pages}" if pages > 1 else ""
        self.add_item(discord.ui.TextDisplay(
            f"## {_clean_folder_name(self._folder)}\n"
            f"{len(self._songs)} songs{page_hint}"
        ))
        row = discord.ui.ActionRow()
        ph = f"Choose a song…{page_hint}"
        sel = discord.ui.Select(placeholder=ph[:150], options=opts, custom_id="nav_song")
        sel.callback = self._on_song
        row.add_item(sel)
        self.add_item(row)
        back_row = discord.ui.ActionRow()
        back = discord.ui.Button(label="← Back to Folders", style=discord.ButtonStyle.secondary,
                                 custom_id="nav_b_folder")
        back.callback = self._on_back_to_folder
        back_row.add_item(back)
        self.add_item(back_row)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_format(self, interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True); return
        self._fmt = interaction.data["values"][0]
        self._folders = _get_folders_for_format(self._index, self._fmt)
        self._folder_page = 0
        self._render_folder_step()
        await interaction.response.edit_message(view=self)

    async def _on_folder(self, interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True); return
        val = interaction.data["values"][0]
        if val == "__prev__":
            self._folder_page = max(0, self._folder_page - 1)
            self._render_folder_step()
            await interaction.response.edit_message(view=self)
            return
        if val == "__next__":
            self._folder_page += 1
            self._render_folder_step()
            await interaction.response.edit_message(view=self)
            return
        self._folder = self._folders[int(val)]
        self._songs = _get_songs_in_folder(self._index, self._fmt, self._folder)
        self._song_page = 0
        self._render_song_step()
        await interaction.response.edit_message(view=self)

    async def _on_song(self, interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True); return
        val = interaction.data["values"][0]
        if val == "__prev__":
            self._song_page = max(0, self._song_page - 1)
            self._render_song_step()
            await interaction.response.edit_message(view=self)
            return
        if val == "__next__":
            self._song_page += 1
            self._render_song_step()
            await interaction.response.edit_message(view=self)
            return
        candidate = self._songs[int(val)] if val.isdigit() else None
        if not candidate:
            await interaction.response.send_message("Song not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _deliver_song(self._bot, interaction, candidate)
        await _log_delivery(self._bot, interaction.user, candidate, source="navigator")

    async def _on_back_to_format(self, interaction: discord.Interaction):
        self._fmt = None
        self._folder = None
        self._folders = []
        self._folder_page = 0
        self._render_format_step()
        await interaction.response.edit_message(view=self)

    async def _on_back_to_folder(self, interaction: discord.Interaction):
        self._folder = None
        self._songs = []
        self._song_page = 0
        self._render_folder_step()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        self.clear_items()


# ══════════════════════════════════════════════════════════════════════════════
#  INFO MESSAGE  (pinned in #info) — Components v2
# ══════════════════════════════════════════════════════════════════════════════

class _ArchiveInfoView(discord.ui.LayoutView):
    """
    Persistent Components v2 view pinned in #info.
    All navigation (format → folder → song) happens by editing THIS message in-place.
    Only the final file delivery is ephemeral.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self._bot = bot
        # Navigator state (reset to home after each delivery)
        self._fmt: Optional[str] = None
        self._folder: Optional[str] = None
        self._folders: list = []
        self._songs: list = []
        self._folder_page = 0
        self._song_page = 0
        self._render_home()

    # ── Step renderers ────────────────────────────────────────────────────────

    def _render_home(self):
        self.clear_items()
        self._fmt = None
        self._folder = None
        self._folders = []
        self._songs = []
        self._folder_page = 0
        self._song_page = 0
        self.add_item(discord.ui.TextDisplay(
            "## Eminem Archive\n"
            "Download songs from the full Eminem archive in FLAC or MP3.\n\n"
            "`/archive [flac/mp3] [song title] [version (optional)]`\n"
            "Example: `/archive flac antichrist 2005 version`\n\n"
            "Or use the menu below to browse and select a file:"
        ))
        self.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        sel = discord.ui.Select(
            placeholder="Choose a format to start browsing…",
            options=[discord.SelectOption(label=fmt, value=fmt) for fmt in FORMATS],
            custom_id="archive_info_fmt",
        )
        sel.callback = self._on_format
        row.add_item(sel)
        self.add_item(row)

    def _render_folder_step(self):
        self.clear_items()
        opts, has_prev, has_next = _folder_options_for_page(self._folders, self._folder_page)
        pages = (len(self._folders) + NAV_PAGE_SIZE - 1) // NAV_PAGE_SIZE
        page_hint = f"  ·  page {self._folder_page + 1}/{pages}" if pages > 1 else ""
        self.add_item(discord.ui.TextDisplay(
            f"## Eminem Archive  ·  {self._fmt}\n"
            f"{len(self._folders)} folders available{page_hint}"
        ))
        row = discord.ui.ActionRow()
        ph = f"Choose a folder…{page_hint}"
        sel = discord.ui.Select(placeholder=ph[:150], options=opts, custom_id="archive_info_folder")
        sel.callback = self._on_folder
        row.add_item(sel)
        self.add_item(row)
        back_row = discord.ui.ActionRow()
        back = discord.ui.Button(label="← Back to Format", style=discord.ButtonStyle.secondary,
                                 custom_id="archive_info_b_fmt")
        back.callback = self._on_back_to_home
        back_row.add_item(back)
        self.add_item(back_row)

    def _render_song_step(self):
        self.clear_items()
        opts, has_prev, has_next = _song_options_for_page(self._songs, self._song_page)
        pages = (len(self._songs) + NAV_PAGE_SIZE - 1) // NAV_PAGE_SIZE
        page_hint = f"  ·  page {self._song_page + 1}/{pages}" if pages > 1 else ""
        self.add_item(discord.ui.TextDisplay(
            f"## {_clean_folder_name(self._folder)}\n"
            f"{len(self._songs)} songs{page_hint}"
        ))
        row = discord.ui.ActionRow()
        ph = f"Choose a song…{page_hint}"
        sel = discord.ui.Select(placeholder=ph[:150], options=opts, custom_id="archive_info_song")
        sel.callback = self._on_song
        row.add_item(sel)
        self.add_item(row)
        back_row = discord.ui.ActionRow()
        back = discord.ui.Button(label="← Back to Folders", style=discord.ButtonStyle.secondary,
                                 custom_id="archive_info_b_folder")
        back.callback = self._on_back_to_folder
        back_row.add_item(back)
        self.add_item(back_row)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_format(self, interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        manager = getattr(self._bot, "ARCHIVE_manager", None)
        if not manager or not manager.song_index_ready.is_set():
            await interaction.response.send_message(
                "Archive is still initialising — please try again in a moment.", ephemeral=True)
            return
        self._fmt = interaction.data["values"][0]
        self._folders = _get_folders_for_format(manager.song_index, self._fmt)
        self._folder_page = 0
        self._render_folder_step()
        await interaction.response.edit_message(view=self)

    async def _on_folder(self, interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        manager = getattr(self._bot, "ARCHIVE_manager", None)
        val = interaction.data["values"][0]
        if val == "__prev__":
            self._folder_page = max(0, self._folder_page - 1)
            self._render_folder_step()
            await interaction.response.edit_message(view=self)
            return
        if val == "__next__":
            self._folder_page += 1
            self._render_folder_step()
            await interaction.response.edit_message(view=self)
            return
        self._folder = self._folders[int(val)]
        self._songs = _get_songs_in_folder(manager.song_index, self._fmt, self._folder)
        self._song_page = 0
        self._render_song_step()
        await interaction.response.edit_message(view=self)

    async def _on_song(self, interaction: discord.Interaction):
        if _is_fed(interaction):
            await interaction.response.send_message(
                "This didn't work. Please try again later.", ephemeral=True)
            return
        val = interaction.data["values"][0]
        if val == "__prev__":
            self._song_page = max(0, self._song_page - 1)
            self._render_song_step()
            await interaction.response.edit_message(view=self)
            return
        if val == "__next__":
            self._song_page += 1
            self._render_song_step()
            await interaction.response.edit_message(view=self)
            return
        candidate = self._songs[int(val)] if val.isdigit() else None
        if not candidate:
            await interaction.response.send_message("Song not found.", ephemeral=True)
            return
        # Defer the ephemeral delivery, then restore the info embed to home
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _deliver_song(self._bot, interaction, candidate)
        await _log_delivery(self._bot, interaction.user, candidate, source="navigator")
        # Reset navigator back to home state via followup edit
        self._render_home()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def _on_back_to_home(self, interaction: discord.Interaction):
        self._render_home()
        await interaction.response.edit_message(view=self)

    async def _on_back_to_folder(self, interaction: discord.Interaction):
        self._folder = None
        self._songs = []
        self._song_page = 0
        self._render_folder_step()
        await interaction.response.edit_message(view=self)


async def _get_info_channel(bot) -> Optional[discord.TextChannel]:
    cfg = _load_archive_config()
    name = cfg.get("info_channel_name", "info")
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name == name:
                return ch
    return None


async def post_or_refresh_info_embed(bot, force: bool = False) -> None:
    """Post the archive info message to #info. Skips if it already exists unless force=True."""
    cfg = _load_archive_config()
    info_ch = await _get_info_channel(bot)
    if not info_ch:
        bot.logger.log(MODULE_NAME, "info channel not found — skipping archive embed post", "WARNING")
        return
    view = _ArchiveInfoView(bot)
    existing_id = cfg.get("info_embed_msg_id")
    if existing_id and not force:
        try:
            await info_ch.fetch_message(int(existing_id))
            bot.logger.log(MODULE_NAME, "Archive info embed already present, skipping")
            return
        except (discord.NotFound, discord.Forbidden):
            pass
    try:
        msg = await info_ch.send(view=view)
        _save_archive_config({"info_embed_msg_id": str(msg.id)})
        bot.logger.log(MODULE_NAME, f"Archive info embed posted (msg {msg.id})")
    except discord.Forbidden:
        bot.logger.log(MODULE_NAME, "Missing permissions to post in #info", "WARNING")


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

async def _log_delivery(bot, user, candidate: dict, source: str = "command"):
    try:
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            return
        md = candidate['metadata']
        embed = discord.Embed(title="Archive Delivery", color=0x3498db, timestamp=datetime.utcnow())
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Source", value=source, inline=True)
        embed.add_field(name="Title", value=md.get('title', 'Unknown'), inline=True)
        embed.add_field(name="Format", value=Path(candidate['path']).suffix.upper(), inline=True)
        embed.add_field(name="File", value=f"`{Path(candidate['path']).name}`", inline=False)
        await channel.send(embed=embed)
    except Exception:
        pass

async def send_bot_log(bot, log_data):
    try:
        channel = discord.utils.get(bot.get_all_channels(), name="bot-logs")
        if not channel:
            return
        embed = discord.Embed(
            title="Command Execution Log",
            color=0x3498db if log_data.get('success') else 0xe74c3c,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="User", value=f"{log_data['user']} ({log_data['user_id']})", inline=False)
        if 'action' in log_data:
            embed.add_field(name="Command", value=log_data['action'], inline=False)
        if 'params' in log_data:
            params = "\n".join([f"- {k}: {v}" for k, v in log_data['params'].items()])
            embed.add_field(name="Parameters", value=params, inline=False)
        embed.add_field(name="Status", value="SUCCESS" if log_data['success'] else "FAILURE", inline=True)
        if not log_data['success'] and log_data.get('error'):
            embed.add_field(name="Error", value=f"```{log_data['error']}```", inline=False)
        await channel.send(embed=embed)
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to send log", e)


# ══════════════════════════════════════════════════════════════════════════════
#  ARCHIVEManager
# ══════════════════════════════════════════════════════════════════════════════

class ARCHIVEManager:
    def __init__(self, bot):
        self.bot = bot
        self.song_index = None
        self.song_index_ready = asyncio.Event()
        self.initialization_task = None

    async def initialize(self):
        self.bot.logger.log(MODULE_NAME, "Starting song index initialization...")
        self.initialization_task = asyncio.create_task(self._initialize_background())

    async def _initialize_background(self):
        try:
            self.song_index = load_song_index(self.bot)
            if not self.song_index:
                self.bot.logger.log(MODULE_NAME, "Building new song index in background...")
                self.song_index = await build_song_index(self.bot)
            self.song_index_ready.set()
            self.bot.logger.log(MODULE_NAME, "Song index ready")
            self.cache_purge_loop.start()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Background initialization failed", e)

    async def ensure_ready(self):
        if not self.song_index_ready.is_set() and self.initialization_task:
            await self.song_index_ready.wait()

    @tasks.loop(hours=CACHE_EXPIRE_DAYS * 24)
    async def cache_purge_loop(self):
        self.bot.logger.log(MODULE_NAME, "Running cache purge task")
        try:
            chan = discord.utils.get(self.bot.get_all_channels(), name=CACHE_CHANNEL_NAME)
            if chan:
                cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_EXPIRE_DAYS)
                await chan.purge(before=cutoff, limit=None)
                self.bot.logger.log(MODULE_NAME, "Cache purged")
            if await check_file_modifications(self.bot):
                self.bot.logger.log(MODULE_NAME, "File modifications detected, rebuilding index")
                self.song_index_ready.clear()
                self.song_index = await build_song_index(self.bot)
                self.song_index_ready.set()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Purge task error", e)

    @cache_purge_loop.before_loop
    async def before_cache_purge(self):
        await self.bot.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup(bot):
    bot.logger.log(MODULE_NAME, "Setting up ARCHIVE module")

    ARCHIVE_manager = ARCHIVEManager(bot)
    bot.ARCHIVE_manager = ARCHIVE_manager

    # Register persistent info view so Browse button survives restarts
    bot.add_view(_ArchiveInfoView(bot))

    asyncio.create_task(ARCHIVE_manager.initialize())

    @bot.listen("on_ready")
    async def _archive_on_ready():
        await ARCHIVE_manager.ensure_ready()
        await post_or_refresh_info_embed(bot, force=False)

    # ── /archive ───────────────────────────────────────────────────────────
    @bot.tree.command(name="archive", description="Get a song from Eminem's archive")
    @app_commands.describe(
        format="File format",
        song_name="Name of the song",
        version="Specific version (optional)",
    )
    @app_commands.choices(format=[app_commands.Choice(name=fmt, value=fmt) for fmt in FORMATS])
    async def ARCHIVE(interaction: discord.Interaction, format: str, song_name: str,
                      version: Optional[str] = None):
        command_data = {'format': format, 'song_name': song_name, 'version': version or 'N/A'}
        await ARCHIVE_manager.ensure_ready()
        if not ARCHIVE_manager.song_index_ready.is_set():
            await interaction.response.send_message(
                "Initializing — please try again shortly.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if _is_fed(interaction):
            await interaction.followup.send("Failed to retrieve song.", ephemeral=True)
            return
        key = find_best_match(ARCHIVE_manager.song_index, format, song_name)
        if not key:
            bot.logger.log(MODULE_NAME, f"Song not found: '{song_name}' in {format}", "WARNING")
            await interaction.followup.send(f"'{song_name}' not found in {format}.", ephemeral=True)
            await send_bot_log(bot, {
                'user': str(interaction.user), 'user_id': interaction.user.id,
                'success': False, 'error': 'Song not found', 'action': 'ARCHIVE',
                'params': command_data,
            })
            return
        candidates = ARCHIVE_manager.song_index[format][key]
        best = select_best_candidate(candidates, version)
        if not best:
            msg = f"'{song_name}'"
            if version:
                msg += f" (version '{version}')"
            await interaction.followup.send(f"{msg} not found.", ephemeral=True)
            return
        bot.logger.log(MODULE_NAME, f"Delivering: {best['original_title']}")
        await _deliver_song(bot, interaction, best)
        await _log_delivery(bot, interaction.user, best, source="slash command")

    # ── /rebuild_index ─────────────────────────────────────────────────────
    @bot.tree.command(name="rebuild_index", description="[Admin] Rebuild the song index cache")
    async def rebuild_index(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "You need administrator permissions to use this command.", ephemeral=True)
            return
        await interaction.response.send_message("Rebuilding song index...", ephemeral=True)
        try:
            ARCHIVE_manager.song_index_ready.clear()
            ARCHIVE_manager.song_index = await build_song_index(bot)
            ARCHIVE_manager.song_index_ready.set()
            await interaction.followup.send("Song index rebuilt successfully!", ephemeral=True)
            bot.logger.log(MODULE_NAME, "Index rebuilt successfully")
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Index rebuild failed", e)
            await interaction.followup.send("Failed to rebuild index.", ephemeral=True)

    # ── Console: postinfo_archive ──────────────────────────────────────────
    async def handle_postinfo_archive(_args: str):
        bot.logger.log(MODULE_NAME, "Force-refreshing archive info embed...")
        await post_or_refresh_info_embed(bot, force=True)
        print("  Archive info embed refreshed.\n")

    bot.console_commands["postinfo_archive"] = {
        "description": "Force-refresh the archive browse embed in #info",
        "handler": handle_postinfo_archive,
    }

    bot.logger.log(MODULE_NAME, "ARCHIVE module setup complete")
