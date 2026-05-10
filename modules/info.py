import asyncio
import discord
import hashlib
import json
from pathlib import Path
from _utils import script_dir, atomic_json_write, _now

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
            "content": "**My Edit & Remaster Archive**\n**Eminem Leak Tracker**\n**Emball Community Edits Tracker**\n**The Complete Eminem Archive**\n**PayPal Tip Jar**"
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


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    atomic_json_write(STATE_PATH, state)


def _hash_config(cfg: dict) -> str:
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _build_embed(cfg: dict) -> discord.Embed:
    embed = discord.Embed(color=cfg.get("color", DEFAULTS["color"]), timestamp=_now())
    for section in cfg.get("sections", []):
        embed.add_field(name=section["title"], value=section["content"], inline=False)
    footer = cfg.get("footer", "")
    if footer:
        embed.set_footer(text=footer)
    return embed


async def _sync(bot, guild: discord.Guild, *, force: bool = False) -> bool:
    cfg   = _load_config()
    state = _load_state()
    current_hash = _hash_config(cfg)

    channel = discord.utils.get(guild.text_channels, name=cfg["channel_name"])
    if not channel:
        bot.logger.log(MODULE_NAME, f"#{cfg['channel_name']} not found in {guild.name}", "ERROR")
        return False

    msg_id      = state.get("message_id")
    posted_hash = state.get("config_hash")

    # check if existing message is still alive
    existing_msg = None
    if msg_id:
        try:
            existing_msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            bot.logger.log(MODULE_NAME, "Info embed was deleted — reposting", "WARNING")
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not fetch info message: {e}", "WARNING")

    if existing_msg and not force and current_hash == posted_hash:
        return False  # up to date, nothing to do

    embed = _build_embed(cfg)

    if existing_msg:
        try:
            await existing_msg.edit(embed=embed)
            state["config_hash"] = current_hash
            _save_state(state)
            bot.logger.log(MODULE_NAME, "Info embed updated")
            return True
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Failed to edit info embed: {e}", "WARNING")
            # fall through to repost

    # clear any stale bot embeds then post fresh
    try:
        async for msg in channel.history(limit=50):
            if msg.author == guild.me and msg.embeds:
                await msg.delete()
    except Exception:
        pass

    try:
        new_msg = await channel.send(embed=embed)
        state["message_id"]   = new_msg.id
        state["config_hash"]  = current_hash
        _save_state(state)
        bot.logger.log(MODULE_NAME, f"Info embed posted (message {new_msg.id})")
        return True
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Failed to post info embed: {e}", "ERROR")
        return False


async def _watch_loop(bot):
    await bot.wait_until_ready()
    last_hash = _load_state().get("config_hash")
    while not bot.is_closed():
        await asyncio.sleep(15)
        try:
            cfg          = _load_config()
            current_hash = _hash_config(cfg)
            state        = _load_state()
            msg_id       = state.get("message_id")

            if current_hash != last_hash or not msg_id:
                if current_hash != last_hash:
                    bot.logger.log(MODULE_NAME, "Config change detected — syncing embed")
                for guild in bot.guilds:
                    await _sync(bot, guild, force=(current_hash != last_hash))
                last_hash = current_hash
            else:
                # periodically verify the message still exists (every ~5 min)
                # done lazily: _sync fetches the message and reposts if missing
                pass
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Watcher error: {e}", "WARNING")


async def _periodic_verify(bot):
    """Every 5 minutes, confirm the embed still exists even if config hasn't changed."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(300)
        try:
            for guild in bot.guilds:
                await _sync(bot, guild)
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Periodic verify error: {e}", "WARNING")


def setup(bot):
    _load_config()  # ensure config exists on disk

    @bot.listen("on_ready")
    async def _info_on_ready():
        for guild in bot.guilds:
            await _sync(bot, guild)
        asyncio.create_task(_watch_loop(bot))
        asyncio.create_task(_periodic_verify(bot))
        bot.logger.log(MODULE_NAME, "Info module ready")

    # If the bot is already ready (e.g. module loaded after on_ready fired), kick off directly
    if bot.is_ready():
        async def _late_start():
            for guild in bot.guilds:
                await _sync(bot, guild)
            asyncio.create_task(_watch_loop(bot))
            asyncio.create_task(_periodic_verify(bot))
            bot.logger.log(MODULE_NAME, "Info module ready (late start)")
        asyncio.ensure_future(_late_start())

    bot.logger.log(MODULE_NAME, "Info module loaded")
