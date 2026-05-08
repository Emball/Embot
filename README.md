# Embot

Eminem-themed Discord bot with music archive, voice message transcription, moderation, audio playback, community features, and auto-updating.

## Setup

```bash
git clone https://github.com/Emball/Embot.git
cd Embot
./start.sh    # Linux (handles Python 3.11 + deps via uv)
start.bat     # Windows
```

First run creates all config templates in `config/` automatically.

### Required

| File | Purpose |
|---|---|
| `config/auth.json` | `{"bot_token": "..."}` — create manually |
| `config/embot.json` | `home_guild_id` — set to your server ID |

### Optional

| File | Purpose |
|---|---|
| `config/archive_config.json` | `eminem_root` — path to Eminem music folder |
| `config/dev.json` | Version bump thresholds, auto-commit/version toggles |
| `config/starboard_config.json` | `channel_id` and `threshold` for starboard |
| `config/moderation.json` | Strike thresholds, rules, invite labels |
| `config/links_config.json` | Quick-link shortcuts (`?name`) |
| `config/logger_config.json` | Log channel assignments |

## Features

### Archive
`/archive` — Search and download Eminem songs. Delivered ephemerally with DM fallback (no public traces). Supports FLAC and MP3, metadata matching, folder-based browsing. Uses a permanent `#songcache` CDN channel.

### Voice Messages
`/vmtranscribe`, `vmstats`, context menus — Saves `.ogg` VMs, transcribes via Whisper, auto-archives (150 days), deletes after 365 days. Reply to @mentions with random VMs. Periodic playback in `#general`.

### Moderation (23 commands)
`/ban /multiban /unban /kick /timeout /untimeout /mute /unmute /softban /warn /warnings /clearwarnings /purge /slowmode /lock /unlock /report /rules /updaterules` — Standard moderation toolkit.

`/fedcheck /fedflag /fedclear /fedscan /fedinvites` — Invite-based suspicion scoring, encrypted media caching, ban appeal voting.

### Audio Player
`/play /stop /skip /pause /resume /queue /loop /leave` — YouTube/SoundCloud playback in voice channels. Queue management, vote-skip, loop toggle.

### Logging
`/setjoinlogs /setbotlogs /logconfig` — Logs 17 Discord event types (messages, members, roles, channels, voice, invites) to designated channels.

### Community
`/community_setup /xp /leaderboard /submission_info /spotlight_preview /spotlight_run` — Submission tracking in `#projects` / `#artwork`, voting/XP, auto-rotating Spotlight Friday winner.

### Starboard
Config-driven — Pins messages to a starboard channel when they reach a reaction threshold.

### Links
`/linkset /linkremove /linktoggle /linklist /linkinfo` — Manage `?name` quick-link shortcuts.

### Icons
Rotates server icon and bot avatar based on holidays (Pride Month, 4th of July, Halloween, Thanksgiving, Christmas).

### Magic Emball
`/magicemball` — 8-ball style responses, per-user cooldown, regex-based smart answers.

### Console
Interactive CLI: `help`, `status`, `version`, `reload`, `modules`, `logs`, and module-specific commands.

## Development mode

```bash
uv run python Embot.py -dev
```

On startup, diffs the working tree against the last version-bump commit. If changes are detected, bumps `_version.py` (MAJOR.MINOR.PATCH.MICRO), commits everything, pulls, and pushes.

### Dev console commands

| Command | Description |
|---|---|
| `commit` | Stage all, version bump, commit, pull, push |
| `changelog [N]` | Show last N version bumps from git history |
| `git` | Show repo branch, status, and remotes |
| `dev_status` | Show version, auto-commit/version state, last bump |
| `auto_commit [on/off]` | Toggle auto-commit (persists to dev.json) |
| `auto_version [on/off]` | Toggle auto-versioning (persists to dev.json) |
| `setup_github <token>` | Configure GitHub auth from config/auth.json |

## Auto-update

Pre-flight check runs before Discord login. Compares local `_version.py` against remote. If remote is newer, fast-forwards via `git merge --ff-only` and restarts (exit code 42). Runtime checks repeat every `auto_update_interval_minutes`.

Config in `embot.json`:

```json
"network": {
    "auto_update": true,
    "auto_update_interval_minutes": 5,
    "auto_update_git_remote": ""
}
```

Set `auto_update_git_remote` if running from a bare folder (not a clone). The bot will `git init` and set up the remote automatically.

## Test mode

```bash
uv run python Embot.py -t
```

Dry-run: connects to Discord, loads modules, syncs commands, reports pass/fail, exits.

## Security

Only `config/auth.json` contains secrets. All other configs auto-generate with defaults and are gitignored.
