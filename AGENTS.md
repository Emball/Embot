# AGENTS.md

**Read this entire file before doing anything else.** If the architecture or module layout changes significantly, update this file to match.

## User

Michael (Emball/Embis). Vibe-coder with beginner Python knowledge. Dual-boots Linux and Windows.

## Agentic Behavior

- NEVER spawn agents or delegate to separate chat processes without explicit confirmation.

## Environment & Testing

**The bot auto-updates.** After every push it polls git every ~30s, detects the version bump, pulls, and restarts on its own. The standard workflow is: push → wait ~30s → verify version via `bridge status` → continue. The `update` and `restart` bridge commands exist only as a manual override if the auto-update loop appears broken — they are not part of the normal post-push flow.

NEVER run Embot.py locally for testing — the live bot runs on the Linux machine 24/7. A duplicate instance registers two bots at once and causes issues. All testing happens against the live server via remote debug.

**EXEC IS READ-ONLY.** Never use `exec` to edit files on the server. Editing files directly creates uncommitted changes that block `git pull --ff-only`. All code edits go through git: edit locally → commit → push → server pulls clean commit. Exec is for reading files, checking logs, and running diagnostics only. Exceptions require explicit approval.

## Remote Debugging

Two modes — both share `config/remote_debug.json`.

### LAN Client (on-machine or Windows)

Run from project root:

| Command | Purpose |
|---|---|
| `uv run python modules/remote_debug.py ping` | Test connectivity |
| `uv run python modules/remote_debug.py status` | Bot vitals |
| `uv run python modules/remote_debug.py guilds` | Guild list |
| `uv run python modules/remote_debug.py modules` | Loaded modules |
| `uv run python modules/remote_debug.py logs` | Last 200 lines of today's log |
| `uv run python modules/remote_debug.py logs --file session_20250101.log` | Specific day file |
| `uv run python modules/remote_debug.py logs --session 2` | Specific session |
| `uv run python modules/remote_debug.py logs --tail 1000` | Last N lines |
| `uv run python modules/remote_debug.py logs --search <pattern>` | Regex search logs |
| `uv run python modules/remote_debug.py logs --search <pattern> --max 50` | Search with result limit |
| `uv run python modules/remote_debug.py logs-list` | All log files |
| `uv run python modules/remote_debug.py db-download <name>` | Download .db to temp/ |
| `uv run python modules/remote_debug.py db-query <name> "<SQL>"` | SELECT/PRAGMA query |
| `uv run python modules/remote_debug.py config <name>` | View config file |
| `uv run python modules/remote_debug.py exec <cmd>` | Shell command (read-only) |
| `echo '<cmd>' \| uv run python modules/remote_debug.py exec` | Same via stdin |
| `uv run python modules/remote_debug.py update` | Git pull + restart |
| `uv run python modules/remote_debug.py restart` | Restart bot |

### Claude Bridge (this session)

GitHub-based command queue via private `Emball/EmbotDebug` repo. The bot polls `cmd.json` every 2s via GitHub API, executes internally, commits results back.

**Config** (`config/remote_debug.json` on Linux):
```json
"claude_bridge": {
    "enabled": true,
    "repo": "Emball/EmbotDebug",
    "token": "ghp_...",
    "poll_interval": 2.0
}
```

**Result routing:**
- Direct output (ping, status, guilds, modules, exec, update, restart) → `result.json`
- File artifacts (logs, logs-list, logs-search, config, db-query) → committed under `logs/`, `config/`, `db/`
- Blocked: db-download, stream

**Startup behaviour:** On bridge start, the bot zeroes out both `cmd.json` and `result.json` (seq → 0). This clears any stale command left over from a previous session or mid-cycle restart, preventing it from being re-executed.

**Claude's workflow per session:**

The GitHub token is in Claude's user preferences as `GitHub Access Token: ghp_...`. This is the same token used for cloning the repo and for `session-init` — there is no separate debug token.

`remote_debug.py` imports `aiohttp` lazily (inside `RemoteDebugServer.__init__`), so no deps need to be installed for the bridge to work.

First, init with that token (once per session):
```bash
python modules/remote_debug.py session-init ghp_...
```

Then use the one-liner for every command — no token needed:
```bash
python modules/remote_debug.py bridge <command> [args...]

# Examples
uv run python modules/remote_debug.py bridge status
uv run python modules/remote_debug.py bridge logs --tail 500
uv run python modules/remote_debug.py bridge logs --search "ERROR"
uv run python modules/remote_debug.py bridge config starboard
uv run python modules/remote_debug.py bridge db-query mod "SELECT name FROM sqlite_master WHERE type='table'"
uv run python modules/remote_debug.py bridge exec "echo hello"
uv run python modules/remote_debug.py bridge update
uv run python modules/remote_debug.py bridge restart
```

The `bridge` command handles everything via plain git (no GitHub API calls): conflict-retrying push, polling, and fetching artifact content. No manual git or sleep needed.

On boot the bot writes `status.json` to EmbotDebug with `online`, `version`, and `started_at`. The bridge client uses this to wait smartly — for `restart`/`update` it reads the pre-restart `started_at` and polls until a newer timestamp appears, rather than blindly sleeping. Normal commands wait ~1s then poll every 1s.

Use `--timeout N` to extend the wait window (default 45s).

## Debugging

**The raw log is the #1 source of truth — check it first, every time.**

Before drawing any conclusions about why something broke, fetch a large chunk of the log. Searching for specific strings is useful but can miss context; a broad `--tail 500` or `--tail 1000` will almost always show the error directly. Don't rely on bridge result output, git history, or assumptions — the log contains the actual traceback, the actual sequence of events, and the actual error message.

Workflow:
1. `logs --tail 500` (or `--tail 1000` for harder problems) — read the raw output
2. Only use `--search` once you know what you're looking for
3. If the log doesn't show the error, go wider (`--tail 2000`, different session) before trying anything else

## Code Style

- Brief comments only. Good code explains itself.
- No section headers, block comments, or reasoning inside code.
- Post-code overview for design decisions only.

## Versioning & Git

- Version format: `MAJOR.MINOR.PATCH.MICRO`
- Bump thresholds (lines changed):
  - `300+` → MAJOR, `100+` → minor, `20+` → patch, `1+` → micro
- Commit message = version number only.
- Increment version, commit, and push after every edit. No permission needed.
- After pushing, wait ~30s for the bot to auto-update, then verify with `bridge status`. Only use the `update` or `restart` bridge commands if the auto-update loop appears broken.
- Keep `requirements.txt` synced. Keep `.gitignore` clean. Keep AGENTS.md current.
- Temp/test code goes in `/temp` (gitignored).

## Embot Codebase Overview

Discord bot for Eminem fan server (discord.py, single guild, UV environment).

### Top-Level

| Path | Purpose |
|---|---|
| `Embot.py` | Entry point — boots bot, loads modules, syncs commands |
| `_version.py` | `__version__ = "X.Y.Z.W"` |
| `pyproject.toml` | uv project config + deps (source of truth) |
| `requirements.txt` | Dep list (kept synced) |
| `config/` | JSON configs, gitignored |
| `modules/` | Feature modules, auto-loaded via `setup(bot)` |
| `icons/` | Holiday icon PNGs |
| `logs/`, `db/`, `cache/` | Runtime data (gitignored) |
| `temp/` | Scratch space (gitignored) |

### Modules

Private `_*.py` files are skipped by the loader.

| Module | Description |
|---|---|
| `messages.py` | Shared message + media cache — text cache, encrypted attachment cache (Fernet), eviction, `cache_message()`, `get_context_messages()`, `get_recent_messages()` |
| `music_archive.py` | Eminem music archive — FLAC/MP3 scan, SQLite index, CDN cache, batch backfill, lazy CDN refresh, SMB-compatible |
| `community.py` | Submission tracking (#projects/#artwork), voting, Spotlight Friday, SQLite |
| `mod_core.py` | Moderation core: DB, config, auth helpers, ModContext, ModerationSystem. Owns media cache TTL loop and `on_vm_transcribed` automod listener |
| `mod_actions.py` | ban, kick, mute, warn, purge, lock, slowmode |
| `mod_appeals.py` | Ban appeal views, modal, voting, lifecycle |
| `mod_oversight.py` | Action review, bot-log monitoring, daily integrity reports, embed tracking |
| `mod_rules.py` | RulesManager — sync/display server rules |
| `mod_suspicion.py` | Suspicion engine: /fedcheck, /fedflag, /fedclear, /fedscan, /fedinvites |
| `mod_logger.py` | 17 Discord event types → join-logs/bot-logs |
| `music_player.py` | Voice playback — queue, FFmpeg, YouTube/SoundCloud, vote-skip |
| `vms_core.py` | VMS core: transcription queue, commands/listeners, dispatches `vm_transcribed` event |
| `vms_transcribe.py` | OGG transcription via Whisper, waveform gen, bulk processing |
| `vms_storage.py` | VM scan/conform, archival, backfill, purge |
| `vms_playback.py` | VM selection, CDN upload, counters, ping cooldown |
| `remote_debug.py` | HTTP debug API + Claude bridge (GitHub-based command queue) |
| `starboard.py` | Dyno-style starboard, config-driven |
| `icons.py` | Holiday icon rotation — server icon + bot avatar |
| `links.py` | `?name` quick-link triggers, JSON-backed |
| `artwork.py` | Apple Music artwork fetcher |
| `magic_emball.py` | Magic 8-ball with Eminem flavor |
| `youtube.py` | YouTube audio extraction + upload monitor |
| `_utils.py` | `atomic_json_write()`, `migrate_config()`, `script_dir()`, `_now()` |

### Config Files (`config/`)

| File | Owner | Key Fields |
|---|---|---|
| `mod.json` | mod_core | roles, channel IDs, log toggles, strike thresholds, rules, invite labels |
| `vms.json` | vms_core | `cache_dir` (changing triggers auto-migration) |
| `music.json` | music_archive | `eminem_root` (SMB path) |
| `links.json` | links | name→value map |
| `starboard.json` | starboard | channel_id, threshold, emoji, self_star, ignore_before |
| `youtube.json` | youtube | channel_id, announce_channel_id, poll_interval, cookies_txt |
| `remote_debug.json` | remote_debug | server, host, port, token, allowed_ips, claude_bridge |

### Databases (`db/`)

| File | Owner |
|---|---|
| `mod.db` | mod_core |
| `vms.db` | vms_core / vms_storage |
| `community.db` | community |
| `starboard.db` | starboard |
| `musicarchive.db` | music_archive |
| `archive.db` | mod_oversight |

### Cross-Module Dependencies

- `messages.py` — shared state, no bot dependency, imported directly by mod_core and vms_playback
- `mod_core.py` — provides `is_owner()` (lazy-imported by music_archive, community, links, mod_logger)
- `mod_suspicion.py` — provides `is_flagged()` (lazy-imported by music_archive)
- `mod_core.setup()` — central hub, creates ModerationSystem, wires all mod modules
- `vms_core.py` dispatches `vm_transcribed`; mod_core listens — VMS has no moderation knowledge
- Modules attach to `bot` via attributes: `bot.ARCHIVE_manager`, `bot._mod_system`, `bot._community_system`, `bot.remote_debug_server`, `bot.vms_manager`
- `bot.logger` (ConsoleLogger) available to all modules
- `_utils.py` used broadly across modules

### Startup Flow

1. Parse CLI args
2. Load `config/embot.json` (auto-create defaults if missing)
3. Init `discord.ext.commands.Bot` with `!` and `?` prefixes
4. Create ConsoleLogger (session-scoped log in `logs/`)
5. `on_ready`: load `_version.py`, start console + heartbeat + auto-update loop, call `load_modules()`, sync slash commands
6. Auto-update: `git fetch`, compare version, fast-forward merge, restart on exit code 42
