#!/usr/bin/env bash
# start.sh — Professional Linux launcher for Embot
# Automatically initializes uv project structure and manages symlinked dependencies.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"

# Force uv to use symlinks from the global cache (~/.cache/uv).
export UV_LINK_MODE="symlink"

# ── Load .env if present ─────────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Ensure uv is installed ───────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "[start.sh] uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ── Project Initialization (Option 2) ────────────────────────────────────────
# If pyproject.toml doesn't exist, initialize it and migrate requirements.txt
if [ ! -f "$SCRIPT_DIR/pyproject.toml" ]; then
    echo "[start.sh] Initializing new uv project..."
    cd "$SCRIPT_DIR"
    uv init --python 3.11 --no-workspace
    
    if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
        echo "[start.sh] Migrating dependencies from requirements.txt..."
        uv add -r requirements.txt
    fi
fi

# ── Sync Environment ─────────────────────────────────────────────────────────
# This ensures .venv is up to date and symlinked.
echo "[start.sh] Syncing dependencies..."
cd "$SCRIPT_DIR"
uv sync --frozen --python 3.11

# ── Restart loop ─────────────────────────────────────────────────────────────
echo "[start.sh] Starting Embot (press Ctrl+C to stop)..."
while true; do
    # Runs Embot.py using the project's managed Python 3.11 environment.
    uv run python "$SCRIPT_DIR/Embot.py" -dev
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -eq 42 ]; then
        echo "[start.sh] Auto-update completed, restarting immediately..."
        continue
    fi
    
    echo
    echo "[start.sh] Embot exited (code $EXIT_CODE). Restarting in 3s (Ctrl+C to stop)..."
    sleep 3 || break
done
