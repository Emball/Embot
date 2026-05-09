import discord
from discord import app_commands
from discord.ext import tasks
from typing import Optional
import inspect

MODULE_NAME = "HELPDOC"

# Commands to exclude from the public help doc
HIDDEN_COMMANDS = {
    "community_setup", "spotlight_preview", "spotlight_run",
    "rebuild_index", "youtube_setup", "updaterules", "restart",
    "setjoinlogs", "setbotlogs", "logconfig",
    "ban", "multiban", "unban", "kick", "timeout", "untimeout",
    "mute", "unmute", "softban", "warn", "warnings", "clearwarnings",
    "purge", "slowmode", "lock", "unlock", "report",
    "fedcheck", "fedflag", "fedclear", "fedscan", "fedinvites",
    "linkset", "linkremove", "linktoggle",
    "update",
}

# Category groupings: command name -> category label
CATEGORIES = {
    "vmtranscribe":   "Voice Messages",
    "vmstats":        "Voice Messages",
    "archive":        "Archive",
    "play":           "Music Player",
    "stop":           "Music Player",
    "skip":           "Music Player",
    "pause":          "Music Player",
    "resume":         "Music Player",
    "queue":          "Music Player",
    "loop":           "Music Player",
    "leave":          "Music Player",
    "artwork":        "Utilities",
    "magicemball":    "Utilities",
    "extract_audio":  "Utilities",
    "xp":             "Community",
    "leaderboard":    "Community",
    "submission_info":"Community",
    "linklist":       "Links",
    "linkinfo":       "Links",
    "rules":          "Server Info",
}

CATEGORY_ORDER = [
    "Voice Messages",
    "Archive",
    "Music Player",
    "Community",
    "Links",
    "Utilities",
    "Server Info",
    "Other",
]

# Human-readable usage examples per command (optional override)
# If not set, examples are auto-generated from parameters
USAGE_OVERRIDES: dict[str, list[str]] = {
    "archive": [
        "/archive flac antichrist",
        "/archive mp3 antichrist 2005 version",
        "/archive [format] [song] [version, optional]",
    ],
    "play": [
        "/play flac antichrist",
        "/play mp3 antichrist 2005 version",
        "/play [format] [song] [version, optional]",
    ],
    "vmtranscribe": [
        "/vmtranscribe disable",
        "/vmtranscribe enable",
    ],
    "skip": ["/skip"],
    "pause": ["/pause"],
    "resume": ["/resume"],
    "queue": ["/queue"],
    "loop": ["/loop"],
    "stop": ["/stop"],
    "leave": ["/leave"],
    "leaderboard": ["/leaderboard"],
    "rules": ["/rules"],
}

DISCORD_MSG_LIMIT = 2000


def _param_label(param: app_commands.Parameter) -> str:
    if param.required:
        return f"[{param.name}]"
    return f"[{param.name}, optional]"


def _build_usage(cmd: app_commands.Command) -> list[str]:
    if cmd.name in USAGE_OVERRIDES:
        return USAGE_OVERRIDES[cmd.name]
    params = list(cmd.parameters)
    if not params:
        return [f"/{cmd.name}"]
    parts = " ".join(_param_label(p) for p in params)
    return [f"/{cmd.name} {parts}"]


def _build_doc(commands: list[app_commands.Command]) -> list[str]:
    """Return list of message strings (split to fit Discord limit)."""
    by_cat: dict[str, list[app_commands.Command]] = {}
    for cmd in commands:
        cat = CATEGORIES.get(cmd.name, "Other")
        by_cat.setdefault(cat, []).append(cmd)

    sections: list[str] = []
    for cat in CATEGORY_ORDER:
        cmds = by_cat.get(cat)
        if not cmds:
            continue
        for cmd in sorted(cmds, key=lambda c: c.name):
            block = f"## {cmd.description}\n\n"
            for line in _build_usage(cmd):
                block += f"`{line}`\n"
            sections.append(block.strip())

    # Pack sections into messages respecting the 2000 char limit
    messages: list[str] = []
    current = ""
    for section in sections:
        chunk = section + "\n\n"
        if len(current) + len(chunk) > DISCORD_MSG_LIMIT:
            if current:
                messages.append(current.strip())
            current = chunk
        else:
            current += chunk
    if current.strip():
        messages.append(current.strip())

    return messages


async def _get_or_create_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    return None


async def _get_bot_pinned(channel: discord.TextChannel, bot_id: int) -> list[discord.Message]:
    try:
        pins = await channel.pins()
        return [m for m in pins if m.author.id == bot_id]
    except Exception:
        return []


async def sync_helpdoc(bot: discord.Client, channel_id: int):
    guild = bot.guilds[0] if bot.guilds else None
    if not guild:
        bot.logger.log(MODULE_NAME, "No guild found, skipping sync", "WARNING")
        return

    channel = await _get_or_create_channel(guild, channel_id)
    if not channel:
        bot.logger.log(MODULE_NAME, f"Channel {channel_id} not found", "WARNING")
        return

    visible = [
        cmd for cmd in bot.tree.get_commands()
        if isinstance(cmd, app_commands.Command) and cmd.name not in HIDDEN_COMMANDS
    ]

    pages = _build_doc(visible)
    if not pages:
        bot.logger.log(MODULE_NAME, "No visible commands to document", "WARNING")
        return

    existing = await _get_bot_pinned(channel, bot.user.id)

    try:
        # Edit existing pinned messages or post new ones
        for i, content in enumerate(pages):
            if i < len(existing):
                if existing[i].content != content:
                    await existing[i].edit(content=content)
            else:
                msg = await channel.send(content)
                await msg.pin()

        # Remove surplus pinned messages if doc shrank
        for old in existing[len(pages):]:
            await old.unpin()
            await old.delete()

        bot.logger.log(MODULE_NAME, f"Help doc synced ({len(pages)} message(s))")
    except Exception as e:
        bot.logger.error(MODULE_NAME, "Failed to sync help doc", e)


def setup(bot: discord.Client):
    config: dict = getattr(bot, "config", {})
    channel_id: int = config.get("helpdoc_channel_id", 0)

    if not channel_id:
        bot.logger.log(MODULE_NAME, "helpdoc_channel_id not set in config — module inactive", "WARNING")
        return

    @bot.listen("on_ready")
    async def _on_ready():
        await bot.wait_until_ready()
        await sync_helpdoc(bot, channel_id)

    @bot.tree.command(name="helpdoc_sync", description="[Owner only] Force re-sync the help doc in #info")
    @app_commands.guild_only()
    async def helpdoc_sync_cmd(interaction: discord.Interaction):
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await sync_helpdoc(bot, channel_id)
        await interaction.followup.send("Help doc synced.", ephemeral=True)

    bot.logger.log(MODULE_NAME, f"Ready — doc channel: {channel_id}")
