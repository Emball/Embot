# Embot

Eminem-themed Discord bot with music archive distribution, voice message transcription, moderation, and auto-updating.

## Setup

```bash
# Clone
git clone https://github.com/Emball/Embot.git
cd Embot

# Run — handles Python 3.11 + deps automatically via uv
./start.sh    # Linux
start.bat     # Windows
```

First run creates all config templates in `config/` automatically.

### Required config

| File | Purpose |
|---|---|
| `config/auth.json` | `{"bot_token": "..."}` — create manually |
| `config/embot.json` | `home_guild_id` — set to your server ID |

### Optional config

| File | Purpose |
|---|---|
| `config/archive_config.json` | `eminem_root` — path to your Eminem music folder |
| `config/dev.json` | Version bump thresholds and toggles |
| `config/starboard_config.json` | `channel_id` and `threshold` for starboard |
| `config/moderation.json` | Strike thresholds, rules, invite labels |

## Features

- **Archive** — Search and download Eminem songs via `/archive`. Delivered ephemerally (no public traces). Supports FLAC and MP3 with metadata matching.
- **Voice Messages** — Automatic transcription and archiving with `/vmtranscribe` and context menus.
- **Moderation** — Strike system with fed flagging, invite scanning, and media hash tracking.
- **Starboard** — Pin messages that reach a reaction threshold.
- **Console** — Interactive CLI with `help`, `status`, `reload`, and module-specific commands.

## Development mode

```bash
uv run python Embot.py -dev
```

On startup, diffs the working tree against the last version-bump commit. If changes are detected, bumps `_version.py` (MAJOR.MINOR.PATCH.MICRO), commits everything, and pushes.

### Console commands (dev mode)

| Command | Description |
|---|---|
| `commit` | Stage all, version bump, commit, push |
| `changelog [N]` | Show last N version bumps |
| `git` | Show repo status |
| `dev_status` | Show version, auto-commit, git state |
| `auto_commit [on/off]` | Toggle auto-commit |
| `auto_version [on/off]` | Toggle auto-versioning |

## Auto-update

Runs a pre-flight check before Discord login. Compares local `_version.py` against remote. If remote is newer, fast-forwards and restarts (exit code 42). Runtime checks repeat every `auto_update_interval_minutes`.

Config in `embot.json`:

```json
"network": {
    "auto_update": true,
    "auto_update_interval_minutes": 5,
    "auto_update_git_remote": ""
}
```

Set `auto_update_git_remote` if running from a bare folder (not a clone). The bot will `git init` and set up the remote automatically.

Only `config/auth.json` contains secrets. All other configs auto-generate with sensible defaults.
