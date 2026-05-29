# AGENTS.md

**Read this entire file before doing anything else.** Update it in the same commit as any change that affects the architecture, workflow, or anything documented here.

## AGENTS.md Editing Etiquette

- **Purpose: this file is a guide for future agents so they know the basic workflows and code layout.**
- **One place per fact.** Duplication guarantees drift.
- **State only, no backstory.** Document what the current state is, not why it got there or what happened in a past session.
- **Don't document the obvious.** If it's readable from the code, it doesn't belong here.
- **No unverified constraints.** Don't add "do NOT do X" unless it's been confirmed to actually fail.
- **Cut before adding.** If something new makes something else redundant, remove the old one.
- **No session discoveries as permanent rules.** A one-off observation isn't a policy.
- **Be brief in your explanations.** This file takes up vital context in every agents session.
- **This is a living document.** Feel free to iterate on it any way you see fit during coding, post-confirmation.

## Syncing AGENTS.md

When Michael says **"sync AGENTS.md"**, follow this protocol exactly — do not improvise:

1. **Gather all available context** — the current session, any pasted prior session transcripts, and the current state of the codebase. If multiple sessions are available, treat them all as input.
2. **Read the current AGENTS.md in full** before touching it.
3. **For each section**, ask: does anything from the sessions contradict, extend, or obsolete what's here? If yes, update it. If no, leave it alone.
4. **What to add:** architecture changes, new modules, new DB tables, new config keys, new known quirks, new commands, confirmed workflow changes. Only add things that are now permanently true of the codebase.
5. **What NOT to add:** session-specific events, debugging blow-by-blow, transient bugs that were fixed, anything that's already implied by the code itself, anything unconfirmed.
6. **Apply the editing etiquette rules** (below) to every change — one place per fact, state only, no backstory, cut before adding.
7. **Bump the version and commit** in the same commit as the AGENTS.md update.

The goal: a future agent reading only AGENTS.md should have an accurate picture of the codebase as it stands right now, informed by everything that has happened across all known sessions.

## Code Style

- Brief comments only. Good code explains itself.
- No section headers, block comments, or reasoning inside code.
- All modules must log their processes to the bot console. No errors silently swallowed — but check `NetworkState.is_online()` before logging network-related errors (see Known Quirks).

## User

Michael (Emball/Embis). Vibe-coder with beginner Python knowledge — don't assume he's clueless. If he flags an error he's already tried the obvious.

## Codebase Overview

GitHub: `Emball/Embot`. Discord bot for an Eminem fan server (discord.py, single guild).

**Always read the actual source files before making changes.**

The repo should be cloned locally at session start, always edit and read code from here.

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

Private `_*.py` files are skipped by the loader. Module load order is enforced via `_MODULE_ORDER` in `Embot.py` — add new modules there.

Modules sharing a prefix form a family. The `_core` file owns the DB, config, and shared state — subordinates import from it, never the reverse. New features that grow large should follow this same split pattern (`prefix_core.py`, `prefix_descriptor.py`).

**Shared utilities** (no `setup(bot)`, imported directly)

| Module | Description |
|---|---|
| `_utils.py` | `atomic_json_write()`, `migrate_config()`, `script_dir()`, `_now()`, `NetworkState` — imported by nearly everything |
| `_messages.py` | Message + media cache. No bot dependency; imported directly by mod_core and vms_playback |

**Moderation** (`mod_`)

| Module | Description |
|---|---|
| `mod_core.py` | Root — DB, shared state, `is_owner()`, media cache TTL loop, `on_vm_transcribed` automod, `on_member_ban` appeal DM. Wraps `bot.logger.error` on setup to DM the owner (from `mod.json` `owner_id`) on every error, with 5-min dedup per unique message. |
| `mod_suspicion.py` | Join scoring to detect suspicious users. Provides `is_flagged()` |
| `mod_actions.py` | ban, kick, mute, warn, purge, lock, slowmode |
| `mod_appeals.py` | Ban appeal flow — modal submission, mod voting, lifecycle management |
| `mod_oversight.py` | Pending action review with approve/revert, daily integrity reports, deletion log tracking |
| `mod_rules.py` | Syncs and displays server rules in #rules. Polls config every 30s, verifies message every 2.5min |
| `mod_notes.py` | Self-maintaining mod command reference posted to #mod-notes |
| `mod_logger.py` | 17 Discord event types → join-logs/bot-logs |

**Voice Message System** (`vms_`)

| Module | Description |
|---|---|
| `vms_core.py` | Root — transcription queue, commands, dispatches `vm_transcribed` |
| `vms_transcribe.py` | Whisper transcription, waveform generation, bulk batch processing |
| `vms_storage.py` | File conforming, archival after 150 days, deletion after 365, backfill |
| `vms_playback.py` | Context-aware VM selection, CDN upload, play counters, ping cooldown |

**Music** (`music_`)

| Module | Description |
|---|---|
| `music_archive.py` | SMB-compatible Eminem music archive. FLAC/MP3 scan, SQLite index, in-server CDN cache. Backfill uploads files to a songcache Discord channel in batches up to 95MB. Files exceeding the per-file threshold are transcoded before upload (`transcoded=1` flag set in DB). Status embed is always delete-and-repost — never edit-in-place — so it stays pinned at the bottom of the channel. Backfill is manual-start only (`/backfill_start`) and never auto-starts on restart unless `backfill_enabled` is set in `cache_meta`. `/backfill_stop` clears the flag gracefully. `/clear_cache` wipes DB and recreates the channel with a confirmation prompt. Recovery scan at backfill start walks the channel and registers any uploads that landed but weren't stored — infers `transcoded` flag by comparing attachment size vs disk size. `song_cache_fails` logs failed uploads but does NOT block retries — all pending files are always retried on next backfill. |
| `music_player.py` | VC playback for archive files and YouTube/SoundCloud |

**Standalone features**

| Module | Description |
|---|---|
| `info.py` | Self-maintaining info docs synced to #info. Polls config every 15s, verifies embed every 5min |
| `community.py` | Project/artwork submission tracking with emoji voting and Spotlight Friday |
| `starboard.py` | Dyno-style starboard, config-driven, Components V2 |
| `youtube.py` | Polls a YouTube channel for new uploads, extracts .OGG audio, announces to Discord |
| `remote_debug.py` | LAN HTTP debug API + Claude bridge |
| `links.py` | `?name` quick-link triggers, JSON-backed |
| `icons.py` | Holiday icon rotation — server icon + bot avatar |
| `artwork.py` | Apple Music artwork fetcher |
| `magic_emball.py` | Magic 8-ball with Eminem flavor |

### Config Files (`config/`)

All configs are gitignored.

| File | Owner | Keys |
|---|---|---|
| `embot.json` | Embot.py | Core bot config, auto-created with defaults if missing |
| `auth.json` | Embot.py | Bot token |
| `mod.json` | mod_core | roles, channel IDs, log toggles, strike thresholds, rules, invite labels |
| `vms.json` | vms_core | `cache_dir` |
| `music.json` | music_archive | `eminem_root` (SMB path) |
| `links.json` | links | name→value map (read per-call, always live) |
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

- `mod_core.py` — provides `is_owner()` (lazy-imported by music_archive, community, links, mod_logger)
- `mod_suspicion.py` — provides `is_flagged()` (lazy-imported by music_archive)
- `mod_actions`, `mod_appeals`, `mod_oversight` — top-level imports from `mod_core`; reloading `mod_core` alone leaves these stale. Non-issue in practice since mod_core changes always come with submodule changes and auto-update reloads all changed files together.
- `vms_core.py` dispatches `vm_transcribed`; mod_core listens
- `community.py` stores config in SQLite — always live
- Modules attach to `bot` via: `bot.ARCHIVE_manager`, `bot._mod_system`, `bot._community_system`, `bot.remote_debug_server`, `bot.vms_manager`
- `bot.logger` (ConsoleLogger) available to all modules

## Versioning & Git

- Format: `MAJOR.MINOR.PATCH.MICRO`
- Bump thresholds (lines changed): `300+` → MAJOR, `100+` → MINOR, `20+` → PATCH, `1+` → MICRO
- Commit message format: `VERSION — short description` (e.g. `38.1.2.2 — starboard: fix duplicate post on V2 edit`). Description ≤10 words, covers the primary change. No vague words like "improvements", "fixes", "tweaks", or "updates".
- Always stage `_version.py` in the same commit as the code change.
- `_version.py` never triggers a bot restart.
- Keep `requirements.txt` synced. Temp/test code goes in `/temp` (gitignored).

## Claude Bridge

GitHub-based command queue via private `Emball/EmbotDebug` repo. Bot polls and commits results via GitHub API; Claude side uses plain git.

**Session start:** `python modules/remote_debug.py session-init <token>` → `bridge status`

The GitHub token is in Claude's user preferences (`GitHub Access Token: ghp_...`) and is used for both session-init and git operations.

**Result routing:**
- Direct output (ping, status, guilds, modules, shell, update, restart, reload) → `result.json`
- Artifact output (logs, config, db-download) → `logs/`, `config/`, `db/` subdirectories

| Command | Server | Bridge | Notes |
|---|---|---|---|
| `ping` | ✓ | ✓ | Test connectivity |
| `status` | ✓ | ✓ | Bot vitals |
| `modules` | ✓ | ✓ | Loaded modules |
| `guilds` | — | ✓ | Guild list |
| `logs [--tail N] [--file F] [--session N] [--search P]` | ✓ | ✓ | Fetch logs |
| `logs-list` | — | ✓ | All log files |
| `config <name>` | ✓ | ✓ | View config file |
| `config-write <name> <json>` | ✓ | — | Write config atomically |
| `config-patch <name> <json>` | ✓ | — | Atomic read-modify-write on config |
| `db-query <name> "<SQL>"` | ✓ | ✓ | Read-only SQL query |
| `db-download <name>` | ✓ | ✓ | Download .db to temp/ |
| `shell <cmd>` | ✓ | ✓ | Shell command — inner strings use single quotes |
| `script-exec <python>` | ✓ | — | Run Python on the bot |
| `reload <module>` | ✓ | — | Hot-reload a single module |
| `update` | ✓ | ✓ | Smart update — reloads changed modules or full restart if core files changed |
| `restart` | ✓ | ✓ | Full restart |
| `session-init <token>` | ✓ | — | Store GitHub token (also works as `bridge session-init <token>`) |

Bridge: `python modules/remote_debug.py bridge <command> [args...]`
LAN: `uv run python modules/remote_debug.py <command> [args...]`

EmbotDebug has intentionally rewritten history (bot force-pushes on every result). Only valid files: `cmd.json`, `result.json`, `status.json`, `payload.txt`, and artifacts under `logs/`, `config/`, `db/`.

## Debugging

**Update hierarchy — use the lowest tier that fits:**

1. **Auto-update (default)** — bot polls git every ~1 min. Module-only changes hot-reload in place; full restart only if `Embot.py` or other non-module tracked files changed. `_version.py` never triggers a restart.
2. **`bridge update`** — triggers the same logic immediately instead of waiting the poll interval.
3. **`bridge reload <module>`** — hot-reloads a single module right now, bypassing git. Use when iterating fast on one module without a commit/push/pull cycle.

`bridge restart` — only when a full restart is explicitly needed.

**Don't run Embot.py locally.** The live bot is the source of truth.

**Logs first.** Before drawing conclusions about a failure, pull a large chunk of the log.

Log workflow:
1. `date` in bash → cross-reference against log timestamps to isolate the current session
2. `bridge logs --tail 500` (or `--tail 1000`) — read raw output
3. `--search` only once you know what you're looking for

Don't `sleep` waiting for bridge responses — poll with `bridge ping`.

Prefer `config-write`/`config-patch` over `shell` for config changes. Prefer `script-exec` over `shell` for Python snippets. Both avoid shell quote mangling.

## Known Quirks

- **`mod_logger.py` footers** — `-#` footer line must be the last line in the text string passed to `_section_with_avatar()`. The helper splits on the first `\n-#` and pins everything after it as a footer outside the Section.
- **`AGENTS.md` does not trigger restart** — it's in the `ignored` set in `_smart_update()` alongside `_version.py`.
- **`mod_oversight.log_bot_register()`** — stores `text` string + `color` int instead of Embed fields. `handle_bot_log_deletion()` reconstructs LayoutView from stored text.
- **Components V2 messages have no `.embeds`** — use recursive `getattr(c, 'content', None)` + `getattr(c, 'children', [])` to inspect text content in components.
- **`script-exec` via bridge runs as a subprocess** — not inside the bot process. `bot`, `discord`, and the event loop are not available. Use `asyncio.ensure_future()` inside a module's `setup()` for in-process async work. For one-shot bot-internal tasks, write a temporary module (e.g. `sb_migrate.py`), push it via git, auto-update pulls it, then `bridge reload <module>` triggers `setup()` which fires the async task. Module should `Path(__file__).unlink(missing_ok=True)` when done and be removed from the repo in the next commit.
- **Starboard V2 edits** — mixing `content=` with a LayoutView edit raises `400 IS_COMPONENTS_V2`. Treat as `NotFound` — delete and repost. Always pass `allowed_mentions=discord.AllowedMentions.none()` on send/edit; also neutralise mentions inline with a zero-width space after `<@`.
- **`CommandRegistrationError: ban already registered`** — appears in logs during `mod_core` reloads. Pre-existing quirk, not a regression. Bot recovers cleanly.
- **Network error suppression** — `NetworkState` in `_utils.py` tracks connectivity. Check `NetworkState.is_online()` before logging network errors; call `NetworkState.suppress()` instead when offline. `Embot.py` flips state via `on_disconnect`/`on_resumed`.
- **`music_archive` — multiple files per message** — `song_cache` uses `file_path` as sole PRIMARY KEY. Multiple files in one batch share the same `message_id` — this is intentional and correct. Never add a UNIQUE constraint on `message_id`.
- **`music_archive` — filename matching** — Discord CDN attachment filenames mangle spaces and special chars to underscores. Always use `normalize_title(Path(filename).stem)` on both sides when matching attachment filenames against song index keys. Plain `replace(' ', '_')` is not sufficient.
- **`music_archive` — orphan/reconcile system removed** — the bot never deletes bot messages with attachments from the songcache channel. Only non-bot messages and the status embed are ever cleaned up. Do not reintroduce automatic deletion of audio messages.
- **`music_archive` — `cdn_url` column** — stored but never read. `_cache_refresh_url` always does a live `fetch_message` to get a fresh URL since Discord CDN URLs expire.

## Components V2 (discord.py LayoutView)

Supported in discord.py 2.6+. If API details aren't covered here, use web search.

Components are not arbitrarily nestable — each has a fixed valid parent.

### Signatures

```
LayoutView(*, timeout=None)
Container(*children, accent_color=None, spoiler=False, id=None)
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

### Layout rules

- `LayoutView` — `view.add_item(item)`
- `Container(*children)` — accepts `ActionRow`, `TextDisplay`, `Section`, `MediaGallery`, `File`, `Separator`
- `Section(*children, accessory)` — up to 3 `TextDisplay` children; accessory must be `Button` or `Thumbnail`
- `MediaGallery(*items)` — up to 10 `MediaGalleryItem`s
- `MediaGalleryItem` is not re-exported to `discord.ui` — import from `discord.ui.media_gallery`
- `ActionRow` — up to 5 Buttons or 1 select menu

### Constraints

- Do not mix `content=` or `embed=` with a LayoutView
- Do not use `![]()` markdown in TextDisplay for images — use `MediaGallery` or `File`
- Max 40 components per message, 4000 chars across all TextDisplays
- String values in defaults must use `\n` escapes, not literal newlines

### Sending

```python
await channel.send(view=layout)
await channel.send(view=layout, files=[...])
await existing_msg.edit(view=layout)
await interaction.response.send_message(view=layout, ephemeral=True)
```

Do not use `defer()` + `followup.send(view=layout)` — followup doesn't set the V2 flag. Instead, send the layout directly in `response.send_message()` and use `original_response()` if you need to edit it later.

Markdown renders fully in TextDisplay: `**bold**`, `## headings`, `` `code` ``, `[links](url)`, `-# small text`.

### `ModContext.reply()` / `followup()`

Both accept `view=` alongside `embed=` and `content=`. The `view` kwarg is passed through to the underlying discord.py send method.

### `mod_oversight.send_bot_log()`

Signature changed to keyword-only: `send_bot_log(ms, guild, *, text, title=None, color=0, footer=None, files_data=None, log_id=None)`. Builds a LayoutView internally. No longer accepts an `Embed` object.

### Exceptions — still uses V1 `ui.View` with `content=`

Three message types still use V1 Views for interactive buttons and cannot use pure LayoutView (V2 `ActionRow` buttons don't support callbacks):
- **`mod_actions.py` ban DM** — `BanAppealView` with submit button
- **`mod_appeals.py` appeal messages** — `AppealVoteView` with Yes/No buttons
- **`mod_oversight.py` action review** — `ActionReviewView` with Approve/Revert/View Chat buttons

These send buttons as V1 `ui.View` and put the text in `content=` (not an embed). Everything else in the bot uses pure V2 LayoutView.

## Session Start Acknowledgement

After reading this file, respond with: "I've read AGENTS.md! [Duick summary of your understanding of the workflows and codebase] [friendly question]"
