import discord
import asyncio
import json
import os
import tempfile
from pathlib import Path
from discord import app_commands
from discord.ext import tasks

from _utils import script_dir

MODULE_NAME = "YOUTUBE"

CONFIG_DEFAULTS = {
    "channel_id": "",
    "announce_channel_id": 0,
    "announce_role_id": 0,
    "last_video_id": "",
    "poll_interval_minutes": 5,
    "max_ogg_size_mb": 25,
    "cookies_txt": "",
}

def _config_path():
    return script_dir() / "config" / "youtube.json"

def load_config():
    from _utils import migrate_config
    return migrate_config(_config_path(), CONFIG_DEFAULTS)

def save_config(cfg):
    try:
        from _utils import atomic_json_write
        atomic_json_write(str(_config_path()), cfg)
    except Exception:
        with open(_config_path(), "w") as f:
            json.dump(cfg, f, indent=2)

async def run_yt_dlp(*args):
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()

async def update_yt_dlp():
    for cmd in (["uv", "pip", "install", "--upgrade", "yt-dlp"],
                ["pip", "install", "--upgrade", "--quiet", "yt-dlp"]):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            break

async def _cookies_args(cfg: dict) -> list:
    p = cfg.get("cookies_txt", "").strip()
    return ["--cookies", p] if p and Path(p).exists() else []

async def extract_ogg(url: str, out_dir: str, cfg: dict = None) -> tuple[str | None, str | None]:
    await update_yt_dlp()
    out_template = os.path.join(out_dir, "%(title)s.%(ext)s")
    cookies = await _cookies_args(cfg or {})
    code, stdout, stderr = await run_yt_dlp(
        "-x",
        "--audio-format", "opus",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--extractor-retries", "3",
        "--no-check-certificates",
        "--extractor-args", "youtube:player_client=android_vr",
        *cookies,
        "-o", out_template,
        "--no-playlist",
        url,
    )
    if code != 0:
        return None, stderr.strip()
    for f in os.listdir(out_dir):
        if f.endswith((".opus", ".ogg", ".webm")):
            path = os.path.join(out_dir, f)
            if not f.endswith(".ogg"):
                new_path = os.path.join(out_dir, Path(f).stem + ".ogg")
                os.rename(path, new_path)
                return new_path, None
            return path, None
    return None, "No output file found after download."

async def get_latest_video(channel_id: str, cfg: dict = None) -> tuple[str | None, str | None, str | None]:
    cookies = await _cookies_args(cfg or {})
    code, stdout, stderr = await run_yt_dlp(
        f"https://www.youtube.com/channel/{channel_id}/videos",
        "--flat-playlist",
        "--playlist-end", "1",
        "--print", "%(id)s|%(title)s",
        "--no-warnings",
        "--no-check-certificates",
        "--extractor-args", "youtube:player_client=android_vr",
        *cookies,
    )
    if code != 0 or not stdout.strip():
        return None, None, None
    parts = stdout.strip().split("|", 1)
    vid_id = parts[0].strip()
    title = parts[1].strip() if len(parts) > 1 else "New Video"
    return vid_id, title, f"https://www.youtube.com/watch?v={vid_id}"

def setup(bot):
    cfg = load_config()

    @bot.tree.command(name="extract_audio", description="Extract and deliver opus audio from a YouTube link as .ogg")
    @app_commands.describe(url="YouTube video URL")
    async def youtube_cmd(interaction: discord.Interaction, url: str):
        if "youtube.com" not in url and "youtu.be" not in url:
            await interaction.response.send_message("Please provide a valid YouTube URL.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        with tempfile.TemporaryDirectory() as tmp:
            filepath, err = await extract_ogg(url, tmp, cfg)
            if err or not filepath:
                bot.logger.log(MODULE_NAME, f"yt-dlp error: {err}")
                await interaction.followup.send(f"Failed to extract audio: `{err}`")
                return

            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            max_mb = cfg.get("max_ogg_size_mb", 25)
            if size_mb > max_mb:
                await interaction.followup.send(
                    f"The audio file is {size_mb:.1f} MB, which exceeds the {max_mb} MB limit."
                )
                return

            filename = Path(filepath).name
            bot.logger.log(MODULE_NAME, f"{interaction.user} requested: {filename} ({size_mb:.1f} MB)")
            await interaction.followup.send(
                f"Here's your audio, {interaction.user.mention}!",
                file=discord.File(filepath, filename=filename),
            )

    @bot.tree.command(name="youtube_setup", description="[Owner only] Configure YouTube notification settings")
    @app_commands.describe(
        channel_id="Emball YouTube channel ID",
        announce_channel="Discord channel for upload notifications",
        announce_role="Role to ping on new upload",
        poll_interval="How often to check for uploads (minutes)",
        cookies_txt="Absolute path to a Netscape cookies.txt file for YouTube auth",
    )
    async def youtube_setup(
        interaction: discord.Interaction,
        channel_id: str = None,
        announce_channel: discord.TextChannel = None,
        announce_role: discord.Role = None,
        poll_interval: int = None,
        cookies_txt: str = None,
    ):
        if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return

        updated = []
        nonlocal cfg

        if channel_id:
            cfg["channel_id"] = channel_id
            updated.append(f"YouTube channel ID: `{channel_id}`")
        if announce_channel:
            cfg["announce_channel_id"] = announce_channel.id
            updated.append(f"Announce channel: {announce_channel.mention}")
        if announce_role:
            cfg["announce_role_id"] = announce_role.id
            updated.append(f"Ping role: {announce_role.mention}")
        if poll_interval:
            cfg["poll_interval_minutes"] = poll_interval
            updated.append(f"Poll interval: {poll_interval}m")
            notify_task.change_interval(minutes=poll_interval)
        if cookies_txt is not None:
            cfg["cookies_txt"] = cookies_txt
            updated.append(f"Cookies: `{cookies_txt}`")

        save_config(cfg)
        if updated:
            await interaction.response.send_message("Updated:\n" + "\n".join(updated))
        else:
            await interaction.response.send_message(
                f"Current config:\n"
                f"• Channel ID: `{cfg['channel_id']}`\n"
                f"• Announce channel: <#{cfg['announce_channel_id']}>\n"
                f"• Role: <@&{cfg['announce_role_id']}>\n"
                f"• Poll interval: {cfg['poll_interval_minutes']}m\n"
                f"• Cookies: `{cfg.get('cookies_txt', '') or 'not set'}`"
            )

    @tasks.loop(minutes=cfg.get("poll_interval_minutes", 5))
    async def notify_task():
        nonlocal cfg
        yt_channel = cfg.get("channel_id", "").strip()
        announce_id = cfg.get("announce_channel_id", 0)
        role_id = cfg.get("announce_role_id", 0)

        if not yt_channel or not announce_id:
            return

        try:
            vid_id, title, url = await get_latest_video(yt_channel, cfg)
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Failed to fetch latest video", e)
            return

        if not vid_id or vid_id == cfg.get("last_video_id"):
            return

        cfg["last_video_id"] = vid_id
        save_config(cfg)

        channel = bot.get_channel(announce_id)
        if not channel:
            bot.logger.log(MODULE_NAME, f"Announce channel {announce_id} not found")
            return

        role_mention = f"<@&{role_id}>" if role_id else ""
        await channel.send(f"{role_mention} Emball just released a new video. Check it out: {url}")
        bot.logger.log(MODULE_NAME, f"Announced new video: {title} ({vid_id})")

    @notify_task.before_loop
    async def before_notify():
        await bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def _sync_config():
        nonlocal cfg
        try:
            cfg = load_config()
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Config sync error", e)

    @_sync_config.before_loop
    async def _before_sync_config():
        await bot.wait_until_ready()

    _sync_config.start()
    notify_task.start()
    bot.logger.log(MODULE_NAME, "YouTube module loaded")
