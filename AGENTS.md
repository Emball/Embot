# AGENTS.md

**IMPORTANT: Read this entire file before doing anything else.** It defines the project structure, conventions, and workflows. If the bot's architecture or module layout changes significantly from what's documented here, update this file to match.

## User

Name: Michael (Emball/Embis). Vibe-coder with beginner Python knowledge. Dual-boots Linux and Windows. Current environment is Windows with PowerShell.

## Agentic Behavior

- NEVER spawn agents or delegate to separate chat processes without explicitly confirming with the user first.

## Remote Debugging & Testing Workflow

**CRITICAL: NEVER load the bot on Windows for testing.** The live bot runs on the Linux laptop 24/7. Running a duplicate instance on Windows would register two bots at once and corrupt the workflow. All testing MUST go through the remote debug API.

### Architecture

| Machine | Role |
|---|---|
| Windows (current) | Development — edit code, commit, push |
| Linux laptop | Deployment — runs the bot 24/7, holds live DBs, configs, and console output |

`modules/remote_debug.py` runs a lightweight HTTP API inside the bot on Linux. `temp/remote_client.py` on Windows connects to it over the local network.

### Default Testing Path

For every code change, follow this order:

1. **Syntax-check individual modules locally:**
   `uv run python -c "import ast; ast.parse(open('modules/<name>.py').read()); print('OK')"`
2. **Optional: import check** for modules that have no startup side-effects:
   `uv run python -c "from modules.<name> import setup; print('OK')"`
3. **Commit and push** (per Versioning & Git rules)
4. **Test against the live bot via remote client** — this is the final word on whether code works, because it exercises against the real server-level configs, DBs, and runtime state.

### Remote Client Setup (Windows)

Create `temp/remote.json`:
```json
{"url": "http://<linux-lan-ip>:8765", "token": "<token-from-linux-console>"}
```

The Linux laptop logs its LAN IP and token on startup:
```
[REMOTE_DEBUG] Remote debug API online at http://192.168.x.x:8765
[REMOTE_DEBUG] Auth token: <64-char-hex>
```

If `temp/remote.json` is missing, the client falls back to env vars `REMOTE_URL` and `REMOTE_TOKEN`.

### Remote Client Commands

All run from the project root:

| Command | Purpose |
|---|---|
| `uv run temp/remote_client.py ping` | Test connectivity |
| `uv run temp/remote_client.py status` | Bot vitals (version, latency, uptime log file) |
| `uv run temp/remote_client.py logs` | Fetch last 200 lines of console log |
| `uv run temp/remote_client.py logs --lines 1000` | Fetch last N lines |
| `uv run temp/remote_client.py stream` | Live tail the console log (Ctrl+C to stop) |
| `uv run temp/remote_client.py db-download <name>` | Download a .db file to temp/ |
| `uv run temp/remote_client.py db-query <name> "<SQL>"` | Run a SELECT/PRAGMA query |
| `uv run temp/remote_client.py config <name>` | View a config file (auth blocked) |
| `uv run temp/remote_client.py update` | Git pull + restart if new commits |
| `uv run temp/remote_client.py restart` | Restart the bot remotely |

### Post-Commit Testing Checklist

After pushing changes:

1. `uv run temp/remote_client.py update` — trigger immediate pull + restart
2. `uv run temp/remote_client.py status` — verify version matches, no crash
3. `uv run temp/remote_client.py logs --lines 300` — scan for ERROR lines
4. If error found: `uv run temp/remote_client.py logs --lines 1000` and grep for traceback
5. If data issue suspected: `uv run temp/remote_client.py db-query <name> "<relevant query>"`
6. Fix, repeat from step 1

**The remote test against Linux is mandatory before considering any change complete.** Local syntax checks alone are insufficient — the real server environment is the only true validator.

## Code Style

- Comments must be brief. Good code explains itself.
- No longwinded section headers or block comments.
- No reasoning inside code files — plan beforehand or use a scratch file.
- No verbose explanations in responses. Post-code overviews for design decisions only.

## Token Usage

Quota is limited. Minimize tool calls and response length. Complete tasks fully but concisely.

## Versioning & Git

- GitHub token lives in the auth file inside the project (`config/auth.json`).
- Version format: `MAJOR.MINOR.PATCH.MICRO` (defined in `modules/dev.py:_increment_version`).
- Increment the version file on every change.
- Commit message = version number only.
- Ensure the .gitignore file is up to date and you do not track files that shouldn't be pushed.
- Keep `requirements.txt` synced to actual imports after every edit.
- Ensure AGENTS.md structure is up to date with codebase.
- Test code for errors and sanity-check before every push (see Remote Debugging & Testing Workflow above).
- Test utilities and temporary code go in /temp, which is gitignored.
- Always commit and push after every edit, if there's no errors. Don't ask permission, just do it.

## Embot Codebase Overview

Discord bot for Eminem fan server made with discord.py. Designed for a single guild. Codebase has a UV environment initialized.

### Top-Level

| Path | Purpose |
|---|---|
| `Embot.py` | Sole entry point — boots bot, loads modules, syncs commands |
| `_version.py` | Single line: `__version__ = "X.Y.Z.W"` (MAJOR.MINOR.PATCH.MICRO) |
| `pyproject.toml` | uv project config + deps (source of truth) |
| `requirements.txt` | Human-readable dep list (kept synced) |
| `config/` | JSON configs for modules, gitignored. Code-level config templates should be edited, not these.|
| `modules/` | All feature modules, auto-loaded by `Embot.py` via `setup(bot)` |
| `icons/` | PNG icon variants for holiday rotation |
| `logs/`, `db/`, `cache/` | Runtime data (auto-created, gitignored) |
| `temp/` | Scratch space for tests/utilities (gitignored) |

### Modules

Each module exposes `setup(bot)` — called during boot. Private `_*.py` files are skipped.

| Module | Description |
|---|---|
| `music_archive.py` | Eminem music archive — scans FLAC/MP3, SQLite index, CDN cache channel |
| `community.py` | Submission tracking (#projects/#artwork), voting, Spotlight Friday, SQLite-backed |
| `mod_core.py` | Moderation core: DB, config, auth helpers, ModContext, ModerationSystem, setup() |
| `mod_actions.py` | Mod action functions: ban, kick, mute, warn, purge, lock, slowmode, etc. |
| `mod_appeals.py` | Ban appeal views, modal, voting, appeal lifecycle |
| `mod_oversight.py` | Action review, bot-log monitoring, daily integrity reports, embed tracking |
| `mod_rules.py` | RulesManager — sync/display server rules |
| `mod_suspicion.py` | Suspicion engine: /fedcheck, /fedflag, /fedclear, /fedscan, /fedinvites |
| `mod_logger.py` | Event logging — 17 Discord event types to join-logs/bot-logs channels |
| `music_player.py` | Voice music playback — queue, FFmpeg, YouTube/SoundCloud, vote-skip |
| `vms_core.py` | VMS core: shared defs, VMSManager, setup() with commands/listeners, stats embed, external queue |
| `vms_transcribe.py` | OGG transcription via Whisper, waveform gen, bulk processing |
| `vms_storage.py` | VM scan/conform, archival, backfill, purge |
| `vms_playback.py` | VM selection (contextual/random), Discord CDN upload, counters, ping cooldown |
| `dev.py` | Dev mode only (`-dev`): auto-versioning, auto-commit/push, dev console commands |
| `starboard.py` | Dyno-style starboard — config-driven, no slash commands |
| `icons.py` | Holiday icon rotation — date-based server icon + bot avatar changes |
| `links.py` | Quick-link system — `?name` prefix triggers, JSON config-backed |
| `artwork.py` | Apple Music album artwork fetcher |
| `magic_emball.py` | Magic 8-ball with Eminem flavor |
| `youtube.py` | YouTube audio extraction + upload notification monitor |
| `remote_debug.py` | HTTP API server — log streaming, DB access, config viewing for remote debugging |
| `_utils.py` | Shared utilities: `atomic_json_write()`, `migrate_config()`, `script_dir()`, `_now()` — imported by multiple modules |

### Cross-Module Dependencies

- `mod_core.py` provides `is_owner()` used by music_archive, community, links, mod_logger (lazy imports inside handlers).
- `mod_suspicion.py` provides `is_flagged()` used by music_archive (lazy import inside handler).
- `mod_core.setup()` is the central hub — creates `ModerationSystem`, imports and wires all other mod modules, registers commands and listeners.
- Modules attach themselves to `bot` via attributes (e.g. `bot.ARCHIVE_manager`, `bot._mod_system`, `bot._community_system`, `bot.remote_debug_server`).
- `bot.logger` (ConsoleLogger) is available to all modules — set by `Embot.py`.
- `_utils.py` provides `atomic_json_write()` shared by links, mod_logger, youtube; `migrate_config()` used by mod_core, youtube, dev, starboard, mod_logger, music_archive; `script_dir()` used by every module; `_now()` used by mod_core, mod_logger, starboard, vms_core, music_archive, dev, community.

### Startup Flow

1. Parse CLI args (`-dev`, `-t`)
2. Load `config/embot.json` (auto-create defaults if missing)
3. Init `discord.ext.commands.Bot` with `!` and `?` prefixes
4. Create ConsoleLogger (session-scoped log in `logs/`)
5. `on_ready`: load `_version.py`, start console + heartbeat + auto-update loop, call `load_modules()`, sync slash commands
6. Auto-update (production): pre-flight `git fetch`, merge remote if newer, restart on exit code 42

### Infrastructure

- **Package manager:** `uv`. `pyproject.toml` is source of truth.
- **Python:** 3.11 (pinned in `.python-version`).
- **GitHub:** repo `Emball/Embot`, token in `config/auth.json`.
- **Gitignore:** `pyproject.toml`, `uv.lock`, `config/*.json`, start scripts, `logs/`, `db/`, `cache/`, `temp/`.
- **Config pattern:** JSON files in `config/` auto-generate with defaults if missing. On every load, `migrate_config()` merges them against the module's defaults dict — new keys get defaults, retired keys are pruned. Only `auth.json` holds secrets.