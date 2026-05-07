# Embot

Discord bot for The Emball Pit. Built with discord.py.

## Modules

**Archive** -- Stream FLAC/MP3 from a local music library to Discord DM. Persistent song index with fuzzy search, Discord CDN caching, metadata via Mutagen.

**VMS** -- Captures, transcribes, and archives Discord voice messages using OpenAI Whisper. Periodic replay in #general, contextual matching, @mention responses.

**Player** -- Queue-based music player with voice channel support. FLAC/MP3, vote-skipping, loop mode, auto-disconnect.

**Moderation** -- Full suite: ban, kick, timeout, mute, softban, warn, purge, slowmode, lock/unlock. Oversight system tracks pending actions, monitors embed deletions, generates daily integrity reports. Media encrypted at rest and scanned before re-hosting.

**Community** -- XP system, submission tracking, leaderboard, weekly Spotlight Friday feature.

**Links** -- Dynamic custom slash commands from JSON config. Add/remove at runtime.

**Starboard** -- Star-reaction tracking with live-updating embeds, SQLite-backed.

**Magic Emball** -- `/magicemball` yes/no responses with regex pattern matching.

**Icons** -- Holiday-based server icon and avatar rotation with Discord rate-limit awareness.

**Logger** -- Structured event logging to console and rotating session files.

**Dev** *(development mode)* -- Hot-reload, git auto-commit/push, version tracking.

**Network** -- TCP server for remote console control, file sync, and session dominance between instances.

## Setup

Requirements:
- Python 3.11+
- FFmpeg on PATH
- Discord bot token with Message Content, Guild Members, and Voice intents

Install:
```bash
pip install -r requirements.txt
# or
uv sync
```

Token goes in `config/token` (or `token.json` in root) as JSON:
```json
{
    "bot_token": "YOUR_DISCORD_TOKEN",
    "github_token": "ghp_xxxxxxxxxxxx",
    "github_email": "you@example.com",
    "github_name": "YourUsername"
}
```

Config files live in `config/`:
- `embot.json` -- command prefix, home guild, network settings
- `moderation.json` -- moderation rules, roles, channels
- `starboard_config.json` -- channel, threshold, emoji
- `archive_config.json` -- music library path
- `links_config.json` -- registered link commands

## Running

```bash
python Embot.py           # Production
python Embot.py -dev      # Development (hot-reload, git auto-commit)
python Embot.py -c 192.168.1.50   # Remote console
python Embot.py -t 192.168.1.50   # Test mode (remote data, pauses primary)
```

On Linux, use `start.sh`. On Windows, use `start.bat`. Both auto-restart on crash.

## Console Commands

| Command | Description |
|---|---|
| `status` | Bot status, latency, loaded modules |
| `reload <module>` | Hot-reload a module |
| `modules` | List loaded modules |
| `logs` | Current log file info |
| `commit` | Commit and push changes (dev mode) |
| `version` | Bot and Python version |
| `exit` | Graceful shutdown |

## Network

For multi-instance setups (e.g., Linux server + Windows dev machine). Configure `embot.json`:

```json
"network": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 9876,
    "remote_host": "192.168.1.50",
    "auto_update": true,
    "auto_update_interval_minutes": 5
}
```

- `--console <host>` -- Remote terminal relay. Identical to typing on the server.
- `--test <host>` -- Syncs config/db/cache from remote, pauses the primary instance, runs locally.
- The primary instance auto-updates via git pull when remote commits are detected.

## Structure

```
Embot/
├── Embot.py
├── main.py
├── _version.py
├── start.sh / start.bat
├── modules/
│   ├── archive.py
│   ├── community.py
│   ├── dev.py
│   ├── icons.py
│   ├── links.py
│   ├── logger.py
│   ├── magic_emball.py
│   ├── moderation.py
│   ├── network.py
│   ├── player.py
│   ├── remasters.py
│   ├── starboard.py
│   └── vms.py
├── config/
├── icons/
├── cache/
├── db/
└── logs/
```
