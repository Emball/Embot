#!/usr/bin/env bash
# start.sh — Linux launcher for Embot
# Uses uv for fast, cache-efficient dependency management.
# Requires: uv (https://github.com/astral-sh/uv)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ── Load .env if present ─────────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Ensure ~/.local/bin is on PATH (needed if uv was just installed) ────────
export PATH="$HOME/.local/bin:$PATH"

# ── Install uv if missing ───────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "[start.sh] uv not found — installing via official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ── Create venv pinned to Python 3.11 ───────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "[start.sh] Creating virtual environment with Python 3.11..."
    uv venv "$VENV_DIR" --python 3.11
fi

# ── Install / sync dependencies from requirements.txt ───────────────────────
echo "[start.sh] Syncing dependencies..."
uv pip install \
    --python "$VENV_DIR/bin/python" \
    -r "$SCRIPT_DIR/requirements.txt"

# ── Restart loop ─────────────────────────────────────────────────────────────
echo "[start.sh] Starting Embot (press Ctrl+C twice to quit the loop)..."
while true; do
    "$VENV_DIR/bin/python" "$SCRIPT_DIR/Embot.py" -dev || true
    echo
    echo "[start.sh] Embot exited. Press Enter to restart, or Ctrl+C to stop."
    read -r || break
done
