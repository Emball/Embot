# AGENTS.md

**Read this entire file before doing anything else.** If the architecture or module layout changes significantly, update this file to match before committing anything.

## Code Style

- Brief comments only. Good code explains itself.
- No section headers, block comments, or reasoning inside code.

## User

Michael (Emball/Embis). Vibe-coder with beginner Python knowledge. However, never assume he is clueless or naive. If he raises an error or flags something, he will likely have already gone through the obvious (restarting the code, ensuring it's up to date, etc)

## Embot Codebase Overview

Codebase on GitHub at Emball/Embot. Discord bot for Eminem fan server (discord.py, single guild focus). The bot enforces single-guild operation and is not deployed in more than one server.

**Read the actual source files** before making changes — don't rely solely on the descriptions below. The repo is cloned locally at the start of every session; always read code from the local clone, never via the bridge (`shell`, `script-exec`, etc.) — the bridge is slow and should be reserved for interacting with the live bot runtime.

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
- `_version.py` changes on every commit and is explicitly excluded from the restart trigger — it never causes a full restart on its own.
- Keep `requirements.txt` synced. Keep `.gitignore` clean. Keep AGENTS.md current.
- Temp/test code goes in `/temp` (gitignored).

## Claude Bridge

GitHub-based command queue via private `Emball/EmbotDebug` repo.

**How it works:** The bot side uses the GitHub API (faster) to poll and commit results. The Claude side uses plain git (clone/push) because GitHub API URLs are not whitelisted in Claude's environment.

**Result routing:**
- Direct output (ping, status, guilds, modules, shell, update, restart, reload) → `result.json`
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
| `config <name>` | ✓ | ✓ | View config file — `auth` is blocked and will return an error |
| `config-write <name> <json>` | ✓ | — | Write a config file atomically — JSON payload routed via `payload.txt`, no mangling |
| `config-patch <name> <json>` | ✓ | — | Atomic read-modify-write on a config file — JSON payload routed via `payload.txt`, no mangling |
| `db-query <name> "<SQL>"` | ✓ | ✓ | Read-only SQL query |
| `db-download <name>` | ✓ | ✓ | Download .db to temp/ |
| `shell <cmd>` | ✓ | ✓ | Shell command — use single quotes for inner strings (double quotes get mangled by the bridge shell) |
| `script-exec <python>` | ✓ | — | Run a Python script on the bot — payload routed via `payload.txt` in EmbotDebug repo, no shell mangling |
| `reload <module>` | ✓ | — | Hot-reload a single module without restarting |
| `update` | ✓ | ✓ | Smart update: reloads only changed modules if possible, full restart only if core files changed |
| `restart` | ✓ | ✓ | Restart bot |
| `session-init <token>` | ✓ | — | Store GitHub token (once per session) |

Bridge: `python modules/remote_debug.py bridge <command> [args...]`
LAN: `uv run python modules/remote_debug.py <command> [args...]`

`restart` waits smartly for the bot to come back online. `update` only waits if a restart was triggered; if it hot-reloaded modules it returns immediately.

**EmbotDebug history:** The bot force-pushes on every result commit, so EmbotDebug intentionally has a shallow/rewritten history. This is expected — don't try to recover or preserve old commits there. The only files that should ever be in EmbotDebug are: `cmd.json`, `result.json`, `status.json`, `payload.txt`, and transient artifacts under `logs/`, `config/`, `db/` written by bridge commands. Never commit anything else there. `config auth` is blocked at the bridge level.

## Debugging

The bot auto-updates. After every push it polls git every ~1 minute, detects the version bump, and pulls. **If only module files changed, the bot hot-reloads those modules in place — no restart, no disruption.** A full restart only triggers if `Embot.py`, config, or other core files changed. `_version.py` never triggers a restart on its own. Use `bridge update` to trigger this immediately rather than waiting out the interval. Use `bridge restart` only when a full restart is explicitly needed. Auto-update is a good fallback if the server is unreachable.

For single-module changes during active development, prefer `bridge reload <module>` — it reloads immediately without touching any other module or waiting for a git pull cycle.

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

Use `config-write` instead of `shell` for writing configs/data. Use `script-exec` instead of `shell` for Python snippets. Both route their payload through `payload.txt` in the EmbotDebug repo — no shell mangling at any layer, regardless of quotes, newlines, or special characters. Pass payloads directly as the final argument; no subprocess workaround needed.

## Components V2 (discord.py LayoutView)

As of 2026, Components V2 is properly supported in discord.py 2.6+. Signatures below are sourced directly from the installed library via `inspect` — treat them as ground truth.

If other API details aren't covered here or you encounter errors, use your search tool.

**All components are top-level only** — every component can only be used directly on `LayoutView` (or inside `Container`/`Section` where noted). They are NOT nestable arbitrarily.

### Signatures

```
LayoutView(*, timeout=None)
Container(*children, accent_colour=None, accent_color=None, spoiler=False, id=None)
TextDisplay(content, *, id=None)
Separator(*, visible=True, spacing=SeparatorSpacing.small, id=None)
Section(*children, accessory, id=None)
Thumbnail(media, *, description=None, spoiler=False, id=None)
MediaGallery(*items, id=None)
  MediaGalleryItem(media, *, description=None, spoiler=False)   # discord.ui.media_gallery.MediaGalleryItem
File(media, *, spoiler=False, id=None)
ActionRow(*children, id=None)
Button(*, style=ButtonStyle.secondary, label=None, disabled=False, custom_id=None, url=None, emoji=None, ...)
```

`SeparatorSpacing`: `small=1`, `large=2`
`ButtonStyle`: `primary=1`, `secondary=2`, `success=3`, `danger=4`, `link=5`, `premium=6`

### What goes where

- `LayoutView` — add items via `view.add_item(item)`
- `Container(*children)` — children passed as positional args to constructor. Can contain: `ActionRow`, `TextDisplay`, `Section`, `MediaGallery`, `File`, `Separator`
- `Section(*children, accessory)` — children are `TextDisplay` items or strings (up to 3); `accessory` is required, must be `Button` or `Thumbnail`
- `MediaGallery(*items)` — up to 10 `MediaGalleryItem`s; `media` is positional URL or `attachment://filename`
- `File(media)` — `media` is `attachment://filename`; pass actual `discord.File` objects in `files=` on the send call
- `Thumbnail(media)` — `media` is positional URL or `attachment://filename`; Section accessory only
- `ActionRow(*children)` — up to 5 `Button`s or 1 select menu

**Important:** `MediaGalleryItem` is NOT re-exported to `discord.ui` — always use `discord.ui.media_gallery.MediaGalleryItem`.

### Constraints

- Do NOT pass `accent_colour`/`accent_color` to `Container` — silently falls back to plain embed
- Do NOT mix `content=` or `embed=` with a LayoutView — Discord rejects it; all text goes in `TextDisplay`
- Do NOT use markdown `![]()` in TextDisplay for images — use `MediaGallery` or `File`
- Component limit: max 40 total components per message, 4000 chars across all TextDisplays
- DEFAULTS string values must use `\n` escapes, not literal newlines — Python 3.11 rejects unterminated string literals

### Sending/editing

```python
await channel.send(view=layout)             # new message
await channel.send(view=layout, files=[...])  # with file attachments
await existing_msg.edit(view=layout)        # update

# discord.py sets the IS_COMPONENTS_V2 flag automatically — do NOT pass flags= manually
await interaction.response.send_message(view=layout, ephemeral=True)
```

**Never** use `defer()` + `followup.send(view=layout)` — followup doesn't set the flag and renders as a plain message. Always use `interaction.response.send_message` directly.

**Markdown in TextDisplay** renders fully: `**bold**`, `## headings`, `` `code` ``, `[links](url)`, `-# small text` (for footers).

### Standard pattern used in this codebase

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

## Session Start Acknowledgement

After reading this file, respond with: "I've read AGENTs.md! [quick summary of your understanding of the workflows and codebase]. What are we working on today, Michael?"