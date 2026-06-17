import asyncio
import hashlib
import json
import urllib.request
import urllib.parse
import discord
from discord import app_commands
from discord.ext import tasks
from _utils import script_dir, atomic_json_write, NetworkState, is_killswitch_active

MODULE_NAME = "TRACKER"

SPREADSHEET_ID = "1x9tTOOqH5WpKOoptdQzABSN_x8oZbMgzIGlGH9w1IKA"

SHEETS = {
    "Unreleased": "Eminem Unreleased Tracker",
    "Released": "Eminem Released Tracker",
    "Recent": "Eminem Recent Tracker",
    "\U0001f3c6 Grails / \U0001f947 Wanted": "Eminem Grails Tracker",
    "\u2b50 Best Of (Unreleased)": "Eminem Best Of Tracker",
    "\u2728 Special": "Eminem Special Tracker",
    "\U0001f5d1\ufe0f Worst Of (Unreleased)": "Eminem Worst Of Tracker",
    "Misc (WIP)": "Eminem Misc (WIP) Tracker",
    "Tracklists": "Eminem Tracklists Tracker",
    "Stems": "Eminem Stems Tracker",
    "Art": "Eminem Art Tracker",
    "Remixes (WIP)": "Eminem Remixes Tracker",
    "Groupbuys": "Eminem Groupbuys Tracker",
    "\U0001f4bf Samples (WIP)": "Eminem Samples Tracker",
    "Fakes": "Eminem Fakes Tracker",
    "Unreleased (Production Projects) [Archived]": "Eminem Unreleased (Production) Tracker",
}

LONG_FIELDS = {"Notes", "Tracklist", "Info"}
MAX_FIELD_LEN = 300

CONFIG_DEFAULTS = {
    "api_key": "",
    "channel_id": 0,
    "poll_interval_minutes": 5,
}

CONFIG_PATH = script_dir() / "config" / "tracker.json"

# In-memory snapshot — intentionally not persisted to disk.
# On every startup/reload the first poll silently baselines all sheets.
_snapshot: dict = {}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in CONFIG_DEFAULTS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    atomic_json_write(str(CONFIG_PATH), CONFIG_DEFAULTS)
    return dict(CONFIG_DEFAULTS)


def _save_config(cfg: dict):
    atomic_json_write(str(CONFIG_PATH), cfg)


def _fetch_sheet(sheet_name: str, api_key: str) -> list | None:
    encoded = urllib.parse.quote(sheet_name, safe='')
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
        f"/values/{encoded}?key={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("values", [])
    except Exception:
        return None


def _is_section_header(row: list) -> bool:
    if not row:
        return True
    non_empty = sum(1 for c in row if str(c).strip())
    if non_empty <= 1:
        return True
    name_col = str(row[1]).strip() if len(row) > 1 else ""
    if not name_col or name_col.startswith("(") or name_col.startswith("Join") or name_col.startswith("Note:"):
        return True
    return False


def _row_to_dict(row: list, header: list) -> dict:
    return {header[i]: str(row[i]).strip() if i < len(row) else "" for i in range(len(header))}


def _truncate(val: str, field: str) -> str:
    if field in LONG_FIELDS and len(val) > MAX_FIELD_LEN:
        return val[:MAX_FIELD_LEN] + "…"
    return val


def _sheet_to_keyed_rows(raw: list) -> tuple:
    """
    Returns (header, {key: row_dict}).
    Key is Era|||Name — stable across row insertions/deletions.
    Duplicate Era+Name combos get a __#N suffix.
    """
    if not raw:
        return [], {}

    header = [str(c).strip() for c in raw[0]]
    rows = {}
    seen: dict = {}
    fallback_idx = 0

    for raw_row in raw[1:]:
        if _is_section_header(raw_row):
            continue
        row_dict = _row_to_dict(raw_row, header)
        era = row_dict.get("Era", "").strip()
        name = row_dict.get("Name", "").strip()
        base = f"{era}|||{name}" if (era or name) else f"__row_{fallback_idx}"
        fallback_idx += 1
        if base not in seen:
            seen[base] = 0
            key = base
        else:
            seen[base] += 1
            key = f"{base}__#{seen[base]}"
        rows[key] = row_dict

    return header, rows


def _hash_rows(rows: dict) -> str:
    return hashlib.md5(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _display_name(row: dict) -> str:
    return row.get("Name", "???").strip() or "???"


def _build_embeds(header, old_rows, new_rows, friendly_name) -> list:
    embeds = []
    old_keys = set(old_rows)
    new_keys = set(new_rows)

    for key in sorted(new_keys - old_keys):
        row = new_rows[key]
        name = _display_name(row)
        notes = row.get("Notes", "").strip()
        embed = discord.Embed(title=f"New Entry: {name}", description=notes or None, color=0x57F287)
        embed.set_author(name="Info", icon_url="https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png")
        for field in header:
            if field in ("Name", "Notes"):
                continue
            val = row.get(field, "").strip()
            if val:
                embed.add_field(name=field, value=_truncate(val, field), inline=True)
        embed.set_footer(text=friendly_name)
        embeds.append(embed)

    for key in sorted(old_keys - new_keys):
        row = old_rows[key]
        name = _display_name(row)
        notes = row.get("Notes", "").strip()
        embed = discord.Embed(title=f"Removed Entry: {name}", description=notes or None, color=0xED4245)
        embed.set_author(name="Info", icon_url="https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png")
        embed.set_footer(text=friendly_name)
        embeds.append(embed)

    for key in sorted(old_keys & new_keys):
        old_row = old_rows[key]
        new_row = new_rows[key]
        changed = [f for f in header if new_row.get(f, "").strip() != old_row.get(f, "").strip()]
        if not changed:
            continue
        name = _display_name(new_row)
        title = f"Updated {changed[0]}: {name}" if len(changed) == 1 else f"Updated Entry: {name}"
        notes = new_row.get("Notes", "").strip()
        embed = discord.Embed(
            title=title,
            description=notes if "Notes" not in changed else None,
            color=0x5865F2,
        )
        embed.set_author(name="Info", icon_url="https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png")
        for field in changed:
            old_val = _truncate(old_row.get(field, "").strip() or "None", field)
            new_val = _truncate(new_row.get(field, "").strip() or "None", field)
            embed.add_field(name=f"Old {field}", value=old_val, inline=False)
            embed.add_field(name=f"New {field}", value=new_val, inline=True)
        embed.set_footer(text=friendly_name)
        embeds.append(embed)

    return embeds


async def _fetch_sheet_async(sheet_name: str, api_key: str) -> list | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_sheet, sheet_name, api_key)


def setup(bot):
    global _snapshot
    _snapshot = {}  # always start fresh — first poll baselines silently
    cfg = _load_config()

    @bot.tree.command(name="tracker_setup", description="[Owner only] Configure tracker update posting")
    @app_commands.describe(
        channel="Channel to post tracker updates in",
        api_key="Google Sheets API key",
        poll_interval="How often to check for changes (minutes)",
    )
    async def tracker_setup(
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        api_key: str = None,
        poll_interval: int = None,
    ):
        from mod_core import is_owner
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return

        nonlocal cfg
        updated = []
        if channel:
            cfg["channel_id"] = channel.id
            updated.append(f"Channel: {channel.mention}")
        if api_key:
            cfg["api_key"] = api_key
            updated.append("API key updated")
        if poll_interval:
            cfg["poll_interval_minutes"] = poll_interval
            poll_task.change_interval(minutes=poll_interval)
            updated.append(f"Poll interval: {poll_interval}m")

        _save_config(cfg)

        if updated:
            await interaction.response.send_message("Updated:\n" + "\n".join(f"• {u}" for u in updated), ephemeral=True)
        else:
            ch = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set"
            await interaction.response.send_message(
                f"Tracker config:\n• Channel: {ch}\n• API key: {'set' if cfg['api_key'] else 'not set'}\n• Poll interval: {cfg['poll_interval_minutes']}m",
                ephemeral=True,
            )

    @bot.tree.command(name="tracker_snapshot", description="[Owner only] Force re-baseline the tracker (clears in-memory snapshot)")
    async def tracker_snapshot_cmd(interaction: discord.Interaction):
        from mod_core import is_owner
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        global _snapshot
        _snapshot = {}
        bot.logger.log(MODULE_NAME, "Snapshot cleared manually — will re-baseline on next poll")
        await interaction.response.send_message("Snapshot cleared. Will re-baseline silently on next poll.", ephemeral=True)

    @tasks.loop(minutes=cfg.get("poll_interval_minutes", 5))
    async def poll_task():
        global _snapshot
        nonlocal cfg
        if is_killswitch_active(bot):
            return
        cfg = _load_config()
        api_key = cfg.get("api_key", "")
        channel_id = cfg.get("channel_id", 0)

        if not api_key or not channel_id:
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            return

        for sheet_name, friendly_name in SHEETS.items():
            try:
                raw = await _fetch_sheet_async(sheet_name, api_key)
                if raw is None:
                    if NetworkState.is_online():
                        bot.logger.log(MODULE_NAME, f"Failed to fetch sheet: {sheet_name}", "WARNING")
                    else:
                        NetworkState.suppress()
                    continue

                header, new_rows = _sheet_to_keyed_rows(raw)

                if sheet_name not in _snapshot:
                    _snapshot[sheet_name] = new_rows
                    bot.logger.log(MODULE_NAME, f"Baselined {sheet_name} ({len(new_rows)} rows)")
                    continue

                old_rows = _snapshot[sheet_name]
                if _hash_rows(new_rows) == _hash_rows(old_rows):
                    continue

                embeds = _build_embeds(header, old_rows, new_rows, friendly_name)
                for embed in embeds:
                    await channel.send(embed=embed)
                    await asyncio.sleep(0.5)

                _snapshot[sheet_name] = new_rows

                if embeds:
                    bot.logger.log(MODULE_NAME, f"{sheet_name}: posted {len(embeds)} update(s)")

            except Exception as e:
                bot.logger.error(MODULE_NAME, f"Error processing sheet {sheet_name}", e)

    @poll_task.before_loop
    async def before_poll():
        await bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def _sync_config():
        nonlocal cfg
        try:
            cfg = _load_config()
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Config sync error", e)

    @_sync_config.before_loop
    async def before_sync():
        await bot.wait_until_ready()

    _sync_config.start()
    poll_task.start()
    bot.logger.log(MODULE_NAME, "Tracker module loaded")
