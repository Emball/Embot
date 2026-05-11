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
            "title": "Moderation Commands",
            "content": "**Warn a member**\n`/warn [member] [reason]`\n\n**Timeout a member**\n`/timeout [member] [duration] [reason]`\n\n**Mute a member**\n`/mute [member] [duration] [reason]`\n\n**Kick a member**\n`/kick [member] [reason]`\n\n**Ban a member**\n`/ban [member] [reason] [delete_days]`\n\n**Softban** *(ban + immediate unban to purge messages)*\n`/softban [member] [reason]`\n\n**Ban multiple members at once**\n`/multiban [user_ids] [reason]`\n\n**Unban a member**\n`/unban [user_id] [reason]`\n\n**View warnings for a member**\n`/warnings [member]`\n\n**Purge messages**\n`/purge [count]`\n\n**Lock a channel**\n`/lock [channel] [reason]`\n\n**Unlock a channel**\n`/unlock [channel]`\n\n**Set slowmode**\n`/slowmode [seconds]`"
        },
        {
            "title": "Owner Commands",
            "content": "**Clear all warnings for a member**\n`/clearwarnings [member]`\n\n**View or toggle logging settings**\n`/logconfig`\n\n**Set moderation log channel**\n`/setbotlogs [channel]`\n\n**Set join/leave log channel**\n`/setjoinlogs [channel]`\n\n**Update server rules** *(reloads from mod.json)*\n`/updaterules`\n\n**Federation tools**\n`/fedcheck [user]` — check suspicion score\n`/fedflag [user] [reason]` — manually flag\n`/fedclear [user]` — clear flag\n`/fedscan` — scan all members\n`/fedinvites` — audit invite sources"
        },
        {
            "title": "Useful Info",
            "content": "**Report command** *(available to all members)*\n`/report [message link] [reason]` — sends to mod-chat\n\n**Appeal process** — banned members receive a DM with appeal instructions automatically.\n\n**Strike thresholds** — configured in `mod.json`. Warnings trigger auto-actions at defined strike counts."
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


def _build_layout(cfg: dict) -> discord.ui.LayoutView:
    sections = cfg.get("sections", [])
    items: list = []

    for i, section in enumerate(sections):
        title   = section.get("title", "")
        content = section.get("content", "")
        text = f"## {title}\n{content}" if title and title.strip() else content

        items.append(discord.ui.Container(discord.ui.TextDisplay(text)))

        if i < len(sections) - 1:
            items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


async def _sync(bot, guild: discord.Guild, *, force: bool = False) -> bool:
    cfg          = _load_config()
    state        = _load_state()
    current_hash = _hash_config(cfg)

    channel = discord.utils.get(guild.text_channels, name=cfg["channel_name"])
    if not channel:
        bot.logger.log(MODULE_NAME, f"#{cfg['channel_name']} not found in {guild.name}", "ERROR")
        return False

    msg_id      = state.get("message_id")
    posted_hash = state.get("config_hash")

    existing_msg = None
    if msg_id:
        try:
            existing_msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            bot.logger.log(MODULE_NAME, "Mod notes message was deleted — reposting", "WARNING")
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Could not fetch mod notes message: {e}", "WARNING")

    if existing_msg and not force and current_hash == posted_hash:
        return False

    layout = _build_layout(cfg)

    if existing_msg:
        try:
            await existing_msg.edit(view=layout)
            state["config_hash"] = current_hash
            _save_state(state)
            bot.logger.log(MODULE_NAME, "Mod notes message updated")
            return True
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Failed to edit mod notes message: {e} — reposting", "WARNING")

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
        _save_state(state)
        bot.logger.log(MODULE_NAME, f"Mod notes message posted (id {new_msg.id})")
        return True
    except Exception as e:
        bot.logger.log(MODULE_NAME, f"Failed to post mod notes message: {e}", "ERROR")
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
                    bot.logger.log(MODULE_NAME, "Config change detected — syncing")
                for guild in bot.guilds:
                    await _sync(bot, guild, force=(current_hash != last_hash))
                last_hash = current_hash
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Watcher error: {e}", "WARNING")


async def _periodic_verify(bot):
    """Every 5 minutes, confirm the message still exists even if config hasn't changed."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(300)
        try:
            for guild in bot.guilds:
                await _sync(bot, guild)
        except Exception as e:
            bot.logger.log(MODULE_NAME, f"Periodic verify error: {e}", "WARNING")


def setup(bot):
    _load_config()

    if bot.is_ready():
        async def _late_start():
            for guild in bot.guilds:
                await _sync(bot, guild)
            asyncio.create_task(_watch_loop(bot))
            asyncio.create_task(_periodic_verify(bot))
            bot.logger.log(MODULE_NAME, "Mod notes module ready (late start)")
        asyncio.ensure_future(_late_start())
    else:
        @bot.listen("on_ready")
        async def _mod_notes_on_ready():
            for guild in bot.guilds:
                await _sync(bot, guild)
            asyncio.create_task(_watch_loop(bot))
            asyncio.create_task(_periodic_verify(bot))
            bot.logger.log(MODULE_NAME, "Mod notes module ready")

    bot.logger.log(MODULE_NAME, "Mod notes module loaded")
