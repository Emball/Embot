import asyncio
import hashlib
import json
import urllib.request
import urllib.parse
import discord
from discord import app_commands
from discord.ext import tasks
from _utils import script_dir, atomic_json_write, NetworkState

MODULE_NAME = "TRACKER"

SPREADSHEET_ID = "1x9tTOOqH5WpKOoptdQzABSN_x8oZbMgzIGlGH9w1IKA"

# Sheets to monitor and their friendly names for embed footers
SHEETS = {
    "Unreleased":                   "Eminem Unreleased Tracker",
    "Released":                     "Eminem Released Tracker",
    "Stems":                        "Eminem Stems Tracker",
    "Fakes":                        "Eminem Fakes Tracker",
    "Misc (WIP)":                   "Eminem Misc (WIP) Tracker",
    "Tracklists":                   "Eminem Tracklists Tracker",
    "Remixes (WIP)":                "Eminem Remixes Tracker",
    "Groupbuys":                    "Eminem Groupbuys Tracker",
    "Unreleased (Production Projects": "Eminem Unreleased (Production) Tracker",
}

# Columns that are too long/noisy to show full diffs for — truncate them
LONG_FIELDS = {"Notes", "Tracklist", "Info"}
MAX_FIELD_LEN = 300

CONFIG_DEFAULTS = {
    "api_key": "",
    "channel_id": 0,
    "poll_interval_minutes": 5,
}

CONFIG_PATH = script_dir() / "config" / "tracker.json"
SNAPSHOT_PATH = script_dir() / "config" / "tracker_snapshot.json"


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


def _load_snapshot() -> dict:
    if SNAPSHOT_PATH.exists():
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_snapshot(snap: dict):
    atomic_json_write(str(SNAPSHOT_PATH), snap)


def _fetch_sheet(sheet_name: str, api_key: str) -> list[list[str]] | None:
    encoded = urllib.parse.quote(sheet_name)
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


def _row_key(row: list[str], header: list[str]) -> str:
    """Stable identifier for a row: Era + Name (first two non-empty columns)."""
    era = row[0].strip() if len(row) > 0 else ""
    name = row[1].strip() if len(row) > 1 else ""
    return f"{era}|||{name}"


def _is_section_header(row: list[str], header: list[str]) -> bool:
    """Section header rows have content only in col 0 or are summary lines."""
    if not row:
        return True
    non_empty = sum(1 for c in row if c.strip())
    if non_empty <= 1:
        return True
    name_col = row[1].strip() if len(row) > 1 else ""
    if not name_col or name_col.startswith("(") or name_col.startswith("Join"):
        return True
    return False


def _row_to_dict(row: list[str], header: list[str]) -> dict:
    return {header[i]: row[i].strip() if i < len(row) else "" for i in range(len(header))}


def _truncate(val: str, field: str) -> str:
    if field in LONG_FIELDS and len(val) > MAX_FIELD_LEN:
        return val[:MAX_FIELD_LEN] + "…"
    return val


def _sheet_to_rows(raw: list[list[str]]) -> tuple[list[str], dict[str, dict]]:
    """Parse raw sheet values into header + keyed row dict. Skips headers/section rows."""
    if not raw:
        return [], {}

    header = [c.strip() for c in raw[0]]
    rows = {}

    for raw_row in raw[1:]:
        if _is_section_header(raw_row, header):
            continue
        key = _row_key(raw_row, header)
        if not key.strip("|||"):
            continue
        rows[key] = _row_to_dict(raw_row, header)

    return header, rows


def _hash_snapshot(rows: dict) -> str:
    return hashlib.md5(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _build_embeds(
    sheet_name: str,
    header: list[str],
    old_rows: dict,
    new_rows: dict,
    friendly_name: str,
) -> list[discord.Embed]:
    embeds = []

    # New entries
    for key, new_row in new_rows.items():
        if key in old_rows:
            continue
        name = new_row.get("Name", key.split("|||")[1])
        embed = discord.Embed(
            title=f"New Entry: {name}",
            description=new_row.get("Notes", ""),
            color=0x57F287,
        )
        embed.set_author(name="Info", icon_url="https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png")
        for field in header:
            if field in ("Name", "Notes"):
                continue
            val = new_row.get(field, "").strip()
            if val:
                embed.add_field(name=field, value=_truncate(val, field), inline=True)
        embed.set_footer(text=friendly_name)
        embeds.append(embed)

    # Removed entries
    for key, old_row in old_rows.items():
        if key in new_rows:
            continue
        name = old_row.get("Name", key.split("|||")[1])
        embed = discord.Embed(
            title=f"Removed Entry: {name}",
            description=old_row.get("Notes", ""),
            color=0xED4245,
        )
        embed.set_author(name="Info", icon_url="https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png")
        embed.set_footer(text=friendly_name)
        embeds.append(embed)

    # Updated entries
    for key, new_row in new_rows.items():
        if key not in old_rows:
            continue
        old_row = old_rows[key]
        changed_fields = [
            f for f in header
            if new_row.get(f, "").strip() != old_row.get(f, "").strip()
        ]
        if not changed_fields:
            continue

        name = new_row.get("Name", key.split("|||")[1])

        # Group by what changed for a nicer title
        if len(changed_fields) == 1:
            title = f"Updated {changed_fields[0]}: {name}"
        else:
            title = f"Updated Entry: {name}"

        notes = new_row.get("Notes", "")
        embed = discord.Embed(title=title, description=notes if "Notes" not in changed_fields else None, color=0x5865F2)
        embed.set_author(name="Info", icon_url="https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png")

        for field in changed_fields:
            old_val = _truncate(old_row.get(field, "").strip() or "None", field)
            new_val = _truncate(new_row.get(field, "").strip() or "None", field)
            embed.add_field(name=f"Old {field}", value=old_val, inline=False)
            embed.add_field(name=f"New {field}", value=new_val, inline=True)

        embed.set_footer(text=friendly_name)
        embeds.append(embed)

    return embeds


async def _fetch_sheet_async(sheet_name: str, api_key: str) -> list[list[str]] | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_sheet, sheet_name, api_key)


def setup(bot):
    cfg = _load_config()
    snapshot = _load_snapshot()

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
        if not await is_owner(interaction):
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

    @bot.tree.command(name="tracker_snapshot", description="[Owner only] Reset the tracker snapshot (next poll will re-baseline, no diff posted)")
    async def tracker_snapshot_cmd(interaction: discord.Interaction):
        from mod_core import is_owner
        if not await is_owner(interaction):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        nonlocal snapshot, cfg
        new_snap = {}
        key = cfg.get("api_key", "")
        if not key:
            await interaction.followup.send("No API key configured.", ephemeral=True)
            return

        for sheet_name in SHEETS:
            raw = await _fetch_sheet_async(sheet_name, key)
            if raw is None:
                continue
            header, rows = _sheet_to_rows(raw)
            new_snap[sheet_name] = rows

        snapshot = new_snap
        _save_snapshot(snapshot)
        bot.logger.log(MODULE_NAME, "Snapshot reset manually")
        await interaction.followup.send(f"Snapshot reset across {len(new_snap)} sheets. Next poll will baseline from current state.", ephemeral=True)

    @tasks.loop(minutes=cfg.get("poll_interval_minutes", 5))
    async def poll_task():
        nonlocal snapshot, cfg
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

                header, new_rows = _sheet_to_rows(raw)
                old_rows = snapshot.get(sheet_name, {})

                # First run — just baseline, don't post
                if sheet_name not in snapshot:
                    snapshot[sheet_name] = new_rows
                    bot.logger.log(MODULE_NAME, f"Baselined {sheet_name} ({len(new_rows)} rows)")
                    continue

                if _hash_snapshot(new_rows) == _hash_snapshot(old_rows):
                    continue

                embeds = _build_embeds(sheet_name, header, old_rows, new_rows, friendly_name)

                for embed in embeds:
                    await channel.send(embed=embed)
                    await asyncio.sleep(0.5)

                snapshot[sheet_name] = new_rows

                if embeds:
                    bot.logger.log(MODULE_NAME, f"{sheet_name}: posted {len(embeds)} update(s)")

            except Exception as e:
                bot.logger.error(MODULE_NAME, f"Error processing sheet {sheet_name}", e)

        _save_snapshot(snapshot)

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
