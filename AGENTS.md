# AGENTS.md

**Read this entire file before doing anything else.** If the architecture or module layout changes significantly, update this file to match before committing anything.

## Code Style

- Brief comments only. Good code explains itself.
- No section headers, block comments, or reasoning inside code.

## User

Michael (Emball/Embis). Vibe-coder with beginner Python knowledge. Assumes the bare minimum due diligence has been done — never tell him the bot "hasn't updated yet" or similar. Claude cannot perceive the passage of time and Michael will have already checked.

## Embot Codebase Overview

Discord bot for Eminem fan server (discord.py, single guild, uv environment).

**Read the actual source files** before making changes — don't rely solely on the descriptions below.

### Top-Level

| Path | Purpose |
|---|---|
| `Embot.py` | Entry point — boots bot, loads modules, syncs commands |
| `_version.py` | `__version__ = "X.Y.Z.W"` |
| `pyproject.toml` | uv project config + deps |
| `requirements.txt` | Dep list |
| `config/` | JSON configs, gitignored |
| `modules/` | Feature modules, auto-loaded via `setup(bot)` |
| `icons/` | Holiday icon PNGs |
| `logs/`, `db/`, `cache/` | Runtime data (gitignored) |
| `temp/` | Scratch space (gitignored) |

### Modules

Private `_*.py` files are skipped by the loader.

Modules are listed in **dependency order** — this is also the required read order for audits (see Full Audit Protocol). This order is enforced at runtime via `_MODULE_ORDER` in `Embot.py`; if you add a new module, add it to that list in the correct position. Any module not in the list is appended alphabetically at the end.

| Module | Description |
|---|---|
| `_utils.py` | `atomic_json_write()`, `migrate_config()`, `script_dir()`, `_now()` — imported by nearly everything |
| `_messages.py` | Message + media cache — text cache, encrypted attachment cache (Fernet), eviction. No bot dependency; imported directly by mod_core and vms_playback |
| `mod_core.py` | Moderation core: DB, config, auth helpers, ModContext, ModerationSystem. Provides `is_owner()`. Owns media cache TTL loop and `on_vm_transcribed` automod listener |
| `mod_suspicion.py` | Suspicion engine: /fedcheck, /fedflag, /fedclear, /fedscan, /fedinvites. Provides `is_flagged()` |
| `mod_actions.py` | ban, kick, mute, warn, purge, lock, slowmode |
| `mod_appeals.py` | Ban appeal views, modal, voting, lifecycle |
| `mod_oversight.py` | Action review, bot-log monitoring, daily integrity reports, embed tracking |
| `mod_rules.py` | RulesManager — sync/display server rules |
| `info.py` | Self-maintaining info embed — syncs to `config/info.json`, auto-reposts if deleted, polls for config changes every 15s and verifies embed exists every 5min |
| `mod_logger.py` | 17 Discord event types → join-logs/bot-logs |
| `vms_core.py` | VMS core: transcription queue, commands/listeners, dispatches `vm_transcribed`. mod_core listens — VMS has no moderation knowledge |
| `vms_transcribe.py` | OGG transcription via Whisper, waveform gen, bulk processing |
| `vms_storage.py` | VM scan/conform, archival, backfill, purge |
| `vms_playback.py` | VM selection, CDN upload, counters, ping cooldown |
| `remote_debug.py` | HTTP debug API + Claude bridge. Bridge timeout 45s. Artifacts committed before `result.json`. Calls shared logic in `Embot.py` via `import __main__` |
| `music_archive.py` | Eminem music archive — FLAC/MP3 scan, SQLite index, CDN cache, SMB-compatible. `_cache_store` uses `INSERT ... ON CONFLICT DO UPDATE` — do NOT revert to `INSERT OR REPLACE` (wipes `file_checksum`). `_scan_pending` is DB-lookup only |
| `music_player.py` | Voice playback — queue, FFmpeg, YouTube/SoundCloud, vote-skip |
| `community.py` | Submission tracking (#projects/#artwork), voting, Spotlight Friday, SQLite |
| `starboard.py` | Dyno-style starboard, config-driven |
| `youtube.py` | YouTube audio extraction + upload monitor |
| `links.py` | `?name` quick-link triggers, JSON-backed |
| `icons.py` | Holiday icon rotation — server icon + bot avatar |
| `artwork.py` | Apple Music artwork fetcher |
| `magic_emball.py` | Magic 8-ball with Eminem flavor |

### Config Files (`config/`)

| File | Owner | Notes |
|---|---|---|
| `embot.json` | Embot.py | Core bot config, auto-created with defaults if missing |
| `auth.json` | Embot.py | Bot token |
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

- `_messages.py` — shared state, no bot dependency, imported directly by mod_core and vms_playback
- `mod_core.py` — provides `is_owner()` (lazy-imported by music_archive, community, links, mod_logger)
- `mod_suspicion.py` — provides `is_flagged()` (lazy-imported by music_archive)
- `mod_core.setup()` — central hub, creates ModerationSystem, wires all mod modules
- `vms_core.py` dispatches `vm_transcribed`; mod_core listens — VMS has no moderation knowledge
- Modules attach to `bot` via attributes: `bot.ARCHIVE_manager`, `bot._mod_system`, `bot._community_system`, `bot.remote_debug_server`, `bot.vms_manager`
- `bot.logger` (ConsoleLogger) available to all modules
- `_utils.py` used broadly across modules

## Embot Coding Rules

Ensure new modules added to the bot properly log their processes in the bot console.

Ensure there's no avenues in a module where an error could be silently swallowed or not passed to the console.

## Versioning & Git

- Version format: `MAJOR.MINOR.PATCH.MICRO`
- Bump thresholds (lines changed):
  - `300+` → MAJOR, `100+` → MINOR, `20+` → PATCH, `1+` → MICRO
- Commit message = version number only.
- Increment version, commit, and push after every edit. No permission needed. Always stage `_version.py` in the same commit as the code change — never commit code without it.
- Keep `requirements.txt` synced. Keep `.gitignore` clean. Keep AGENTS.md current.
- Temp/test code goes in `/temp` (gitignored).

## Discord Console Commands

Server-owner-only slash commands registered in `Embot.py` as top-level commands. All responses are ephemeral. Auth uses `interaction.user.id == interaction.guild.owner_id` (actual Discord guild owner, not bot owner). Output is embedded inline as a code block; falls back to file attachment only if content exceeds 2000 characters.

| Command | Description |
|---|---|
| `/status` | Version, latency, uptime, guilds, log file |
| `/modules` | Loaded and failed module list |
| `/logs [tail] [search]` | Recent log lines or regex search — inline, file if large |
| `/config <name>` | View a config file — inline, file if large |
| `/dbquery <name> <query>` | Read-only SQL query — inline, file if large |
| `/restart` | Restart the bot |

## Claude Bridge

GitHub-based command queue via private `Emball/EmbotDebug` repo.

**How it works:** The bot side uses the GitHub API (faster) to poll and commit results. The Claude side uses plain git (clone/push) because GitHub API URLs are not whitelisted in Claude's environment.

**Result routing:**
- Direct output (ping, status, guilds, modules, exec, update, restart) → `result.json`
- File artifacts (logs, logs-list, logs-search, config, db-query, db-download) → committed under `logs/`, `config/`, `db/`

**Session checklist:** `session-init` → `bridge status` → work.

The GitHub token is in Claude's user preferences as `GitHub Access Token: ghp_...`. Same token is used to authenticate Git processes on Claude.

Once per session: `python modules/remote_debug.py session-init ghp_...`

| Command | Bridge (Claude) | LAN (Michael) | Purpose |
|---|---|---|---|
| `ping` | ✓ | ✓ | Test connectivity |
| `status` | ✓ | ✓ | Bot vitals |
| `modules` | ✓ | ✓ | Loaded modules |
| `guilds` | — | ✓ | Guild list |
| `logs [--tail N] [--file F] [--session N] [--search P] [--max N]` | ✓ | ✓ | Fetch logs |
| `logs-list` | — | ✓ | All log files |
| `config <name>` | ✓ | ✓ | View config file |
| `db-query <name> "<SQL>"` | ✓ | ✓ | Read-only SQL query |
| `db-download <name>` | ✓ | ✓ | Download .db to temp/ |
| `exec <cmd>` | ✓ | ✓ | Shell command (read-only) |
| `update` | ✓ | ✓ | Git pull + restart |
| `restart` | ✓ | ✓ | Restart bot |
| `session-init <token>` | ✓ | — | Store GitHub token (once per session) |

Bridge: `python modules/remote_debug.py bridge <command> [args...]`
LAN: `uv run python modules/remote_debug.py <command> [args...]`

`restart`/`update` wait smartly for the bot to come back online.

## Debugging

The bot auto-updates. After every push it polls git every ~1 minute, detects the version bump, pulls, and restarts. Use `bridge update` or `bridge restart` to trigger this immediately rather than waiting out the interval. Auto-update is a good fallback if the server is unreachable.

Testing individual files is recommended, but do not try to run a Embot.py session locally. How the live bot responds to the latest code is the ideal source of truth on whether or not it's truly clean.

Exec is useful for debugging when you need to do something in the live bot root that remote_debug doesn't satisfy. Try to avoid modifying the live bot files though, as it can create uncommitted changes that block `git pull --ff-only`. Code edits go through git: edit locally → commit → push → server pulls.

The raw, live bot log is a great source of truth. Check it first every time if something fails. The outputs are generally very verbose.

Before drawing any conclusions about why something broke, fetch a large chunk of the log. Searching for specific strings is useful but can miss context.

Log Workflow:
1. Run `date` in the bash tool to get current UTC time, then cross-reference against log timestamps to identify the current session and ignore stale entries
2. `logs --tail 500` (or `--tail 1000` for harder problems) — read the raw output
3. Only use `--search` once you know what you're looking for
4. If the log doesn't show the error, go wider or pull the entire log file if necessary

If that fails to identify the issue, you can expand to other avenues.

## Full Audit Protocol

Triggered by Michael saying **"full audit"** or **"audit: \<scope\>"**.

A full audit is not a quick scan. It requires loading every file in scope into context and reasoning across them together. Do not shortcut this — targeted reads miss architectural bugs that only appear when files are compared.

### Read Order

Read `Embot.py` first, then the Modules table top-to-bottom — it is already ordered by dependency. For a scoped audit (`audit: mod`, `audit: vms`, etc.), read only the relevant cluster plus `Embot.py`, `_utils.py`, and `mod_core.py`.

### Reasoning Passes (run both, in order)

**Pass 1 — Within-file:** For each file, check:
- Silent error swallowing (`bare except`, `except: pass`, errors not logged)
- Dead code (assigned but never read, tasks never started, unreachable branches)
- Auth checks — correct pattern used? (`is_owner()` vs guild owner ID vs no check)
- Resource leaks (connections, sessions, executors not closed)

**Pass 2 — Cross-file:** After all files are loaded, check:
- Shared logic usage — does every call site use the canonical function, or does anything reimplement it inline? (`run_exec`, `_check_for_update`, `get_logs_data`, etc.)
- Auth consistency — same operation uses same auth pattern across all three surfaces (console, guild slash, remote_debug)
- Module dependencies — does anything import or reference a symbol that was removed or renamed?
- Duplicate restart/cleanup logic across classes or modules

### Output Format

Group findings by severity:

**Bugs** — incorrect behaviour, wrong logic, silent failures
**Dead Code** — never runs, never read, can be deleted
**Inconsistencies** — same operation done differently in different places
**Suggestions** — not broken, but worth improving

For each finding: file + line range, one-line description, and whether it's safe to fix immediately or needs discussion first.

## Session Start Acknowledgement

After reading this file, respond with: "I've read AGENTs.md! [quick summary of your understanding of the workflows and codebase]. What are we working on today, Michael?"
