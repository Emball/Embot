import asyncio
import discord
import hashlib
import json
from _utils import script_dir, atomic_json_write

MODULE_NAME = "MOD_NOTES"

CONFIG_PATH = script_dir() / "config" / "mod_notes.json"
STATE_PATH  = script_dir() / "config" / "mod_notes_state.json"

DEFAULTS = {
    "channel_name": "mod-notes",
    "sections": [
        {
            "title": "Owner Commands",
            "content": "**Logging**\n`/logconfig` — view or toggle log settings\n`/setbotlogs [channel]` — set mod log channel\n`/setjoinlogs [channel]` — set join/leave log channel\n\n**Server**\n`/updaterules` — reload rules from mod.json\n\n**Federation tools**\n`/fedcheck [user]` — check suspicion score\n`/fedflag [user] [reason]` — manually flag\n`/fedclear [user]` — clear flag\n`/fedscan` — scan all members\n`/fedinvites` — audit invite sources"
        },
        {
            "title": "Mod Commands",
            "content": "All commands work with both `/` and `?` prefixes.\n\n**Warn a member**\n`/warn [member] [reason]`\n\n**Timeout a member**\n`/timeout [member] [duration] [reason]`\n\n**Mute a member**\n`/mute [member] [duration] [reason]`\n\n**Kick a member**\n`/kick [member] [reason]`\n\n**Ban a member**\n`/ban [member] [reason] [delete_days]`\n\n**Softban** *(ban + immediate unban to purge messages)*\n`/softban [member] [reason]`\n\n**Ban multiple members at once**\n`/multiban [user_ids] [reason]`\n\n**Unban a member**\n`/unban [user_id] [reason]`\n\n**View or clear warnings**\n`/warnings [member]`\n`/clearwarnings [member]`\n\n**Purge messages**\n`/purge [count]`\n\n**Lock / unlock a channel**\n`/lock [channel] [reason]`\n`/unlock [channel]`\n\n**Set slowmode**\n`/slowmode [seconds]`"
        },
        {
            "title": "Enforcement Guide",
            "content": "Offense-by-offense action reference for all mods.\n[Emball Moderator Enforcement Guide](https://docs.google.com/spreadsheets/d/1Gz65bq6f4AtdWwmUiJAMP_NEahM9bvDIlJLR51kBAuU/edit?usp=sharing)"
        }
    ]
}

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULTS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    atomic_json_write(CONFIG_PATH, DEFAULTS)
    return dict(DEFAULTS)


def _load_state(guild_id: int) -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(str(guild_id), {})
        except Exception:
            pass
    return {}


def _save_state(guild_id: int, state: dict):
    all_states = {}
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                all_states = json.load(f)
        except Exception:
            pass
    all_states[str(guild_id)] = state
    atomic_json_write(STATE_PATH, all_states)


def _hash_config(cfg: dict) -> str:
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _build_layout(cfg: dict) -> discord.ui.LayoutView:
    """Build a Components V2 LayoutView from config sections.

    Each section becomes:
      - A Container with accent_color (first section only gets the color border)
      - A ## heading (section title) via TextDisplay
      - The section content via TextDisplay
      - A Separator between sections
    """
    sections = cfg.get("sections", [])
    footer = cfg.get("footer", "")

    items: list[discord.ui.Item] = []

    for i, section in enumerate(sections):
        title   = section.get("title", "")
        content = section.get("content", "")

        # Build the text for this section:
        # - zero-width-space titles are spacer sections (no heading, just content)
        # - normal titles get a ## heading
        if title and title.strip() and title.strip() != "\u200b":
            text = f"## {title}\n{content}"
        else:
            text = content

        container = discord.ui.Container(
            discord.ui.TextDisplay(text),
        )
        items.append(container)

        # Separator between sections (not after the last one)
        if i < len(sections) - 1:
            items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

    if footer:
        items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        items.append(discord.ui.TextDisplay(f"-# {footer}"))

    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


async def _sync(bot, guild: discord.Guild, *, force: bool = False) -> bool:
    cfg          = _load_config()
    state        = _load_state(guild.id)
    current_hash = _hash_config(cfg)

    channel = discord.utils.get(guild.text_channels, name=cfg["channel_name"])
    if not channel:
        bot.logger.log(MODULE_NAME, f"#{cfg['channel_name']} not found in {guild.name}", "ERROR")
        return False

    msg_id      = state.get("message_id")
    posted_hash = state.get("config_hash")

    # Check if the existing message is still alive
    existing_msg = None
    if msg_id:
        try:
            existing_msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            bot.logger.log(MODULE_NAME, "Mod notes message was deleted — reposting", "WARNING")
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not fetch mod notes message: {e}", "WARNING")

    message_missing = bool(msg_id and existing_msg is None)
    if not force and current_hash == posted_hash and not message_missing:
        return False  # up to date, nothing to do

    layout = _build_layout(cfg)

    # Always delete and resend — avoids Discord "(edited)" flag on Components V2 messages
    # Clear any stale bot messages in the channel then post fresh
    try:
        async for msg in channel.history(limit=50):
            if msg.author == guild.me:
                await msg.delete()
    except Exception:
        pass

    try:
        new_msg = await channel.send(view=layout)
        state["message_id"]  = new_msg.id
        state["config_hash"] = current_hash
        _save_state(guild.id, state)
        bot.logger.log(MODULE_NAME, f"Mod notes message posted (id {new_msg.id})")
        return True
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Failed to post mod notes message: {e}", "ERROR")
        return False


async def _watcher(bot):
    """Single loop: checks config hash every 15s, verifies message exists every 5min."""
    await bot.wait_until_ready()
    # Seed last_hash from whichever guild has a saved state
    last_hash = None
    for guild in bot.guilds:
        h = _load_state(guild.id).get("config_hash")
        if h:
            last_hash = h
            break
    tick = 0
    while not bot.is_closed():
        await asyncio.sleep(15)
        tick += 1
        try:
            cfg          = _load_config()
            current_hash = _hash_config(cfg)
            config_changed = current_hash != last_hash

            if config_changed:
                bot.logger.log(MODULE_NAME, "Config change detected — syncing")

            # Every 20 ticks (~5 min) do a full verify even if hash unchanged
            full_verify = (tick % 20 == 0)

            if config_changed or full_verify:
                for guild in bot.guilds:
                    msg_id = _load_state(guild.id).get("message_id")
                    if config_changed or full_verify or not msg_id:
                        await _sync(bot, guild, force=config_changed)
                if config_changed:
                    last_hash = current_hash
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Watcher error: {e}", "WARNING")


def setup(bot):
    _load_config()  # ensure config exists on disk

    if bot.is_ready():
        # Module loaded after on_ready already fired — run directly, skip listener
        async def _late_start():
            for guild in bot.guilds:
                await _sync(bot, guild)
            asyncio.create_task(_watcher(bot))
            bot.logger.log(MODULE_NAME, "Mod notes module ready (late start)")
        asyncio.ensure_future(_late_start())
    else:
        @bot.listen("on_ready")
        async def _mod_notes_on_ready():
            for guild in bot.guilds:
                await _sync(bot, guild)
            asyncio.create_task(_watcher(bot))
            bot.logger.log(MODULE_NAME, "Mod notes module ready")

    bot.logger.log(MODULE_NAME, "Mod notes module loaded")
