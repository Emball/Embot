# AGENTS.md

**IMPORTANT: Read this entire file before doing anything else.** It defines the project structure, conventions, and workflows. If the bot's architecture or module layout changes significantly from what's documented here, update this file to match.

**YOU MUST COMMIT AND PUSH AFTER EVERY SINGLE EDIT. Do not ask permission. Do not wait for confirmation. Every file change → commit → push.

## User

Name: Michael (Emball/Embis). Vibe-coder with beginner Python knowledge. Dual-boots Linux and Windows. Current environment is Windows with PowerShell.

## Codebase Overview

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
| `archive.py` | Eminem music archive — scans FLAC/MP3, SQLite index, CDN cache channel |
| `community.py` | Submission tracking (#projects/#artwork), voting, Spotlight Friday, SQLite-backed |
| `moderation.py` | Full mod suite: ban, kick, mute, warn, purge, lock, fedcheck, rules (23+ commands) |
| `logger.py` | Event logging — 17 Discord event types to join-logs/bot-logs channels |
| `player.py` | Voice music playback — queue, FFmpeg, YouTube/SoundCloud, vote-skip |
| `vms.py` | Voice Message System — OGG transcription via Whisper, SQLite, archiving |
| `dev.py` | Dev mode only (`-dev`): auto-versioning, auto-commit/push, dev console commands |
| `starboard.py` | Dyno-style starboard — config-driven, no slash commands |
| `icons.py` | Holiday icon rotation — date-based server icon + bot avatar changes |
| `links.py` | Quick-link system — `?name` prefix triggers, JSON config-backed |
| `artwork.py` | Apple Music album artwork fetcher |
| `magic_emball.py` | Magic 8-ball with Eminem flavor |
| `youtube.py` | YouTube audio extraction + upload notification monitor |
| `_utils.py` | Shared `atomic_json_write()` — imported by links, logger, youtube |

### Cross-Module Dependencies

- `moderation.py` is the central dependency hub — its `is_owner()`, `is_flagged()` are imported by archive, community, links, logger at call time (lazy imports inside handlers).
- Modules attach themselves to `bot` via attributes (e.g. `bot.ARCHIVE_manager`, `bot._mod_system`, `bot._community_system`).
- `bot.logger` (ConsoleLogger) is available to all modules — set by `Embot.py`.
- `_utils.py` provides `atomic_json_write()` shared by links, logger, youtube.

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
- **Config pattern:** JSON files in `config/` auto-generate with defaults if missing. Only `auth.json` holds secrets.

## Code Style

- Comments must be brief. Good code explains itself.
- No longwinded section headers or block comments.
- No reasoning inside code files — plan beforehand or use a scratch file.
- No verbose explanations in responses. Post-code overviews for design decisions only.

## Token Usage

Quota is limited. Minimize tool calls and response length. Complete tasks fully but concisely.

## Before Pushing

- Test code for errors and sanity-check before every push.
- Test utilities and temporary code go in /temp, which is gitignored.

## Versioning & Git

- Version format: `MAJOR.MINOR.PATCH.MICRO` (defined in `modules/dev.py:_increment_version`).
- GitHub token lives in the auth file inside the project (`config/auth.json`).
- Increment the version file on every change.
- Commit message = version number only.
- Always commit and push after every edit.
- Ensure the .gitignore file is up to date and you do not track files that shouldn't be pushed.
- The commit/push instruction at the top of this file is absolute — do not override it.

## Requirements

- Keep `requirements.txt` synced to actual imports after every edit.

## Agentic Behavior

- Do not spawn sub-agents or delegate to separate processes without explicitly confirming with the user first.
