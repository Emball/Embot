# Embot
### AI slop Discord bot for The Emball Pit Discord Server

A feature-bloated, over-engineered Discord bot built with discord.py

---

## Modules

### 🎵 Archive
Stream FLAC and MP3 files from a local Eminem music library directly to Discord via DM. Maintains a persistent song index with fuzzy search, Discord CDN caching, and metadata extraction via Mutagen.

### 🎤 VMS (Voice Message System)
Automatically captures, transcribes, and archives every Discord voice message using OpenAI Whisper. Periodically replays old VMs in `#general` — either randomly or contextually matched against recent chat — and responds to @mentions with a random VM.

### 🎶 Player
Queue-based music player with voice channel support. Supports FLAC and MP3, vote-skipping, loop mode, and auto-disconnect on inactivity. Pulls from the same archive as the Archive module.

### 🛡️ Moderation
Full moderation suite with slash and prefix commands. Includes ban, kick, timeout, mute, softban, warn, purge, slowmode, lock/unlock, and a rule-violation ban flow. Features an oversight system that tracks pending actions, monitors embed deletions for tampering, generates daily integrity reports, and routes everything through a bot-logs channel. Media attachments are encrypted at rest and scanned for CSAM before being re-hosted.

### 🌍 Community
XP system, submission channels, leaderboard, and Spotlight Friday — a weekly automated feature that highlights a top community submission in `#announcements`. Tracks thread engagement and awards XP for replies to submissions.

### 🔗 Links
Dynamic custom slash commands backed by a JSON config. Any link can be registered, toggled, or removed at runtime without restarting the bot. Includes collision detection against existing commands.

### ⭐ Starboard
Reacts to ⭐ emoji and reposts qualifying messages to a dedicated starboard channel. Star count is tracked in SQLite and the starboard embed updates live as reactions change.

### 🎱 Magic Emball
`/magicemball` — ask a question, get a response. Includes special-case pattern matching for certain phrases. Per-user cooldown to prevent spam.

### 🖼️ Icons
Automatically rotates the server icon and bot avatar based on holidays and special dates (Halloween, Christmas, Thanksgiving, 4th of July, 9/11, Pride Month). Respects Discord's avatar rate limits.

### 📋 Logger
Structured event logging to both console and rotating session log files. All modules share a single `ConsoleLogger` instance via `bot.logger`.

### 🔧 Dev *(development mode only)*
Hot-reload, git integration, and version tracking. Only loaded when the bot is started with the `-dev` flag.

---

## Setup

### Requirements
- Python 3.11+
- FFmpeg on `PATH`
- A Discord bot token with Message Content, Guild Members, and Voice intents enabled

### Install dependencies
```bash
pip install -r requirements.txt
```

### Environment variables
| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Your Discord bot token |
| `EMINEM_ROOT` | Archive module | Path to the root of the local music library |
| `FERNET_KEY` | Moderation module | Fixed secret for encrypting cached media across restarts |

Alternatively, `EMINEM_ROOT` can be set in `config/archive_config.json`.

### Config files
All config lives in `config/`. Key files:

| File | Module | Description |
|---|---|---|
| `moderation.json` | Moderation | Word lists, elevated roles, channel IDs, rules content |
| `starboard_config.json` | Starboard | Channel ID, threshold, emoji |
| `archive_config.json` | Archive | Path to music library |
| `links_config.json` | Links | Registered link commands |

### Run
```bash
# Production
python Embot.py

# Development mode (hot-reload, git integration)
python Embot.py -dev
```

---

## Console
The bot exposes an interactive console while running:

| Command | Description |
|---|---|
| `status` | Bot status, latency, loaded modules |
| `reload <module>` | Hot-reload a specific module |
| `modules` | List all loaded modules |
| `logs` | Show current log file info |
| `version` | Python, discord.py, and bot version |
| `exit` | Graceful shutdown |

---

## Project Structure
```
Embot/
├── Embot.py              # Bot entrypoint, module loader, console
├── _version.py           # Version string
├── modules/              # Feature modules (auto-loaded on startup)
│   ├── archive.py
│   ├── community.py
│   ├── dev.py
│   ├── icons.py
│   ├── links.py
│   ├── logger.py
│   ├── magic_emball.py
│   ├── moderation.py
│   ├── player.py
│   ├── starboard.py
│   └── vms.py
├── config/               # JSON config files
├── icons/                # Holiday icon PNGs
├── cache/                # Runtime cache (VMs, archive URLs, Whisper models)
├── db/                   # SQLite databases
└── logs/                 # Session log files
```

---

## License
Private. Do whatever, I don't care.
