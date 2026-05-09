# AGENTS.md

## User

Vibe-coder with beginner Python knowledge. Dual-boots Linux and Windows.

## Code Style

- Comments must be brief. Good code explains itself.
- No longwinded section headers or block comments.
- No reasoning inside code files — plan beforehand or use a scratch file.
- No verbose explanations in responses. Post-code overviews for design decisions only.

## Token Usage

quota is limited. Minimize tool calls and response length. Complete tasks fully but concisely.

## Project Setup (new repos)

- Initialize git if not already done.
- Set up `uv` venv properly.
- Create symlink installers for both Linux (`install.sh`) and Windows (`install.bat`).
- Create `start.sh` and `start.bat` pointing to the entry point.
- Create a `_version.py` if no version file exists.

## Versioning & Git

- GitHub token lives in the auth file inside the project (`config/auth.json`).
- Increment the version file on every change.
- Commit message = version number only.
- Always commit and push after every edit.

## Requirements

- Keep `requirements.txt` synced to actual imports after every edit.

## Before Pushing

- Test code for errors and sanity-check before every push.
