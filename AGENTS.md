# AGENTS.md

**Read this entire file before doing anything else.** If the architecture or module layout changes significantly, update this file to match before committing anything.

## Code Style

- Brief comments only. Good code explains itself.
- No section headers, block comments, or reasoning inside code.

## User

Michael (Emball/Embis). Vibe-coder with beginner Python knowledge. However, never assume he is clueless or naive. If he raises an error or flags something, he will likely have already gone through the obvious (restarting the code, ensuring it's up to date, etc)

## Embot Codebase Overview

Codebase on GitHub at Emball/Embot. Discord bot for Eminem fan server (discord.py, single guild focus). The bot enforces single-guild operation and is not deployed in more than one server.

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

Module order is enforced at runtime via `_MODULE_ORDER` in `Embot.py`; if you add a new module, add it to that list in the correct position. 

| Module | Description |
|---|---|
| `_utils.py` | `atomic_json_write()`, `migrate_config()`, `script_dir()`, `_now()` — imported by nearly everything |
| `_messages.py` | Message + media cache. No bot dependency; imported directly by mod_core and vms_playback |
| `mod_core.py` | Moderation core. Provides `is_owner()`. Owns media cache TTL loop and `on_vm_transcribed` automod listener |
| `mod_suspicion.py` | Scores members on join using signals to detect suspicious users. Provides `is_flagged()` |
| `mod_actions.py` | ban, kick, mute, warn, purge, lock, slowmode |
| `mod_appeals.py` | Ban appeal flow — modal submission, mod voting, lifecycle management |
| `mod_oversight.py` | Pending action review with approve/revert, daily integrity reports, embed tracking |
| `mod_rules.py` | Syncs and displays server rules in #rules channel |
| `mod_notes.py` | Self-maintaining mod command reference posted to the #mod-notes channel |
| `info.py` | Self-maintaining info docs synced to #info. Polls for config changes every 15s, verifies embed every 5min |
| `mod_logger.py` | 17 Discord event types → join-logs/bot-logs |
| `vms_core.py` | VMS core — transcription queue, commands, dispatches `vm_transcribed`. mod_core listens |
| `vms_transcribe.py` | Whisper-based transcription, waveform generation, bulk batch processing |
| `vms_storage.py` | VM file conforming, archival after 150 days, deletion after 365, backfill |
| `vms_playback.py` | Context-aware VM selection, CDN upload, play counters, ping cooldown |
| `remote_debug.py` | LAN HTTP debug API + Claude bridge |
| `music_archive.py` | SMB-compatible Eminem music archive. FLAC/MP3 scan, SQLite index, in-server CDN cache |
| `music_player.py` | VC playback for archive files and YouTube/SoundCloud |
| `community.py` | Project/artwork submission tracking with emoji voting and Spotlight Friday |
| `starboard.py` | Dyno-style starboard, config-driven |
| `youtube.py` | Polls a YouTube channel for new uploads, extracts .OGG audio, announces to Discord |
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

## Claude Bridge

GitHub-based command queue via private `Emball/EmbotDebug` repo.

**How it works:** The bot side uses the GitHub API (faster) to poll and commit results. The Claude side uses plain git (clone/push) because GitHub API URLs are not whitelisted in Claude's environment.

**Result routing:**
- Direct output (ping, status, guilds, modules, shell, update, restart) → `result.json`
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
| `config-write <name> <json>` | ✓ | — | Write a config file atomically (no shell mangling) |
| `db-query <name> "<SQL>"` | ✓ | ✓ | Read-only SQL query |
| `db-download <name>` | ✓ | ✓ | Download .db to temp/ |
| `shell <cmd>` | ✓ | ✓ | Shell command — use single quotes for inner strings (double quotes get mangled by the bridge shell) |
| `script-exec <python>` | ✓ | — | Run a Python script passed as a string — avoids quote nesting issues in exec |
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

In shell, double quotes inside double quotes get mangled. Always use single quotes: `bridge shell "uv run python -c 'code here'"`

The raw, live bot log is a great source of truth. Check it first every time if something fails. The outputs are generally very verbose.

Before drawing any conclusions about why something broke, fetch a large chunk of the log. Searching for specific strings is useful but can miss context.

Log Workflow:
1. Run `date` in the bash tool to get current UTC time, then cross-reference against log timestamps to identify the current session and ignore stale entries
2. `logs --tail 500` (or `--tail 1000` for harder problems) — read the raw output
3. Only use `--search` once you know what you're looking for
4. If the log doesn't show the error, go wider or pull the entire log file if necessary

If that fails to identify the issue, you can expand to other avenues.

If facing response issues, never use `sleep` to wait for a response. Poll with `bridge ping` instead, and until it responds.

Use `config-write` instead of `shell` for writing configs/data — handles special characters safely. Use `script-exec` instead of `shell` for Python snippets — avoids quote nesting.

For large `config-write` payloads (e.g. full JSON configs), invoke via Python subprocess to avoid shell mangling:
```python
import json, subprocess
result = subprocess.run(
    ["python", "modules/remote_debug.py", "bridge", "config-write", "<name>", json.dumps(data)],
    cwd="/home/claude/Embot", capture_output=True, text=True
)
```

## Components V2 (discord.py LayoutView)

As of 2026, Components V2 is properly implanted in the latest version's of Discord.py

If other API details aren't properly covered here or you encounter errors, use your search tool to search online.

**Key classes** (all under `discord.ui`):
- `LayoutView(timeout=None)` — top-level container, sent via `channel.send(view=layout)` or `message.edit(view=layout)`
- `Container(*children, accent_color=None)` — card-like box; `accent_color` is an int (e.g. `0x1a1a2e`) for a colored left border, or `None` for no border
- `TextDisplay(content)` — renders markdown text inside a Container or directly in the layout
- `Separator(spacing=discord.SeparatorSpacing.small)` — vertical gap between items; `SeparatorSpacing.small` or `SeparatorSpacing.large`

**Markdown in TextDisplay** renders fully: `**bold**`, `## headings`, `` `code` ``, `[links](url)`, `-# small text` (for footers).

**Standard pattern** used in this codebase:
```python
def _build_layout(cfg):
    items = []
    for i, section in enumerate(cfg["sections"]):
        title, content = section["title"], section["content"]
        text = f"## {title}\n{content}" if title.strip() else content
        items.append(discord.ui.Container(discord.ui.TextDisplay(text)))
        if i < len(cfg["sections"]) - 1:
            items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    if cfg.get("footer"):
        items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        items.append(discord.ui.TextDisplay(f"-# {cfg['footer']}"))
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view
```

**Sending/editing:**
```python
await channel.send(view=layout)          # new message
await existing_msg.edit(view=layout)     # update

# Interaction responses work the same — discord.py detects LayoutView and sets the flag automatically:
await interaction.response.send_message(view=layout, ephemeral=True)
# Do NOT pass flags= manually — send_message() does not accept a flags kwarg and will error
```

**Important:** Never use `defer()` + `followup.send(view=layout)` for Components V2 — followup does not set the flag and renders as a plain message. Always use `interaction.response.send_message` directly.

**Additional components** (all under `discord.ui`):
- `Section(text, accessory=...)` — text on the left, optional `Button` or `Thumbnail` accessory on the right
- `Thumbnail(url=...)` — small inline image, used as a `Section` accessory
- `MediaGallery(*items)` — correct way to embed images in a layout; each item is a `MediaGalleryItem(url=...)`. Do NOT use markdown `![]()` in TextDisplay for images.
- `ActionRow(*children)` — horizontal row of up to 5 `Button`s or 1 select menu; buttons/selects must live inside an ActionRow (or Section accessory)

**Component limits:** max 40 total components per message, 4000 chars across all TextDisplays.

**Important constraints:**
- `Container` children are passed as positional args to the constructor, not via `add_item`
- `LayoutView` items ARE added via `view.add_item(item)`
- Do NOT mix `content=` or `embed=` with a LayoutView — Discord rejects it; all text goes in `TextDisplay`
- Do NOT pass `color` in DEFAULTS for modules that don't use `accent_color` — `_build_layout` must not reference `DEFAULTS["color"]` if that key doesn't exist
- DEFAULTS string values must use `\n` escapes, not literal newlines — Python 3.11 rejects unterminated string literals

## Session Start Acknowledgement

After reading this file, respond with: "I've read AGENTs.md! [quick summary of your understanding of the workflows and codebase]. What are we working on today, Michael?"