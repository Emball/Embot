import asyncio
import discord
import hashlib
import json
from _utils import script_dir, atomic_json_write

MODULE_NAME = "INFO"

CONFIG_PATH = script_dir() / "config" / "info.json"
STATE_PATH  = script_dir() / "config" / "info_state.json"

DEFAULTS = {
    "channel_name": "info",
    "color": 0x1a1a2e,
    "sections": [
        {
            "title": "Welcome",
            "content": "It seems you've fallen into the Ǝmball pit. Don't be scared!"
        },
        {
            "title": "Links",
            "content": "[My Edit & Remaster Archive](https://drive.google.com/drive/folders/1RJ9IU9hivytvKnO4qDhlMlnaZr3e7q0W?usp=sharing)\n[Eminem Leak Tracker](https://docs.google.com/spreadsheets/d/1x9tTOOqH5WpKOoptdQzABSN_x8oZbMgzIGlGH9w1IKA/edit?usp=sharing)\n[Emball Community Edits Tracker](https://docs.google.com/spreadsheets/d/1FCJmG1RlT6N0cQio7t4xgup9scFkezsA87GS2dObZAg/edit?gid=207340854#gid=207340854)\n[The Complete Eminem Archive](https://docs.google.com/document/d/179l9aN3Y5gStie83tI-oS9dwE45UoYtU/edit?usp=sharing&ouid=106288690543947942103&rtpof=true&sd=true)\n[PayPal Tip Jar](https://www.paypal.com/donate/?business=FPWACREA4X5Z8&no_recurring=0&item_name=Remastering+content+to+preserve+for+the+future&currency_code=USD)\n\nI also do paid remaster requests for $10. If you want a song remastered, DM me."
        },
        {
            "title": "Commands",
            "content": "**Toggle automatic VM transcription**\nTurn Embot voice message transcriptions on or off.\n`/vmtranscribe disable`\n`/vmtranscribe enable`\n\n**Download a song from the archive**\n`/archive flac antichrist`\n`/archive mp3 antichrist 2005 version`\n`/archive [format] [song] [version, optional]`"
        }
    ],
    "footer": ""
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
    color = cfg.get("color", DEFAULTS["color"])
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
            bot.logger.log(MODULE_NAME, "Info message was deleted — reposting", "WARNING")
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not fetch info message: {e}", "WARNING")

    if existing_msg and not force and current_hash == posted_hash:
        return False  # up to date, nothing to do

    layout = _build_layout(cfg)

    if existing_msg:
        try:
            await existing_msg.edit(view=layout)
            state["config_hash"] = current_hash
            _save_state(guild.id, state)
            bot.logger.log(MODULE_NAME, "Info message updated")
            return True
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Failed to edit info message: {e} — reposting", "WARNING")
            # fall through to repost

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
        bot.logger.log(MODULE_NAME, f"Info message posted (id {new_msg.id})")
        return True
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Failed to post info message: {e}", "ERROR")
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
            bot.logger.log(MODULE_NAME, "Info module ready (late start)")
        asyncio.ensure_future(_late_start())
    else:
        @bot.listen("on_ready")
        async def _info_on_ready():
            for guild in bot.guilds:
                await _sync(bot, guild)
            asyncio.create_task(_watcher(bot))
            bot.logger.log(MODULE_NAME, "Info module ready")

    bot.logger.log(MODULE_NAME, "Info module loaded")
