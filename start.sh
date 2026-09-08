#!/usr/bin/env bash
# start.sh — Linux/Mac launcher for Embot

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# Ensure uv is installed
if ! command -v uv &>/dev/null; then
    echo "[start.sh] uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Create venv if missing
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "[start.sh] Creating virtual environment..."
    uv venv --python 3.11 "$SCRIPT_DIR/.venv"
fi

# Install dependencies into venv
echo "[start.sh] Installing dependencies..."
uv pip install --python "$SCRIPT_DIR/.venv/bin/python3" -r "$SCRIPT_DIR/requirements.txt"

# Restart loop
echo "[start.sh] Starting Embot (press Ctrl+C to stop)..."
while true; do
    "$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/Embot.py" || EXIT_CODE=$?

    if [ "${EXIT_CODE:-0}" -eq 42 ]; then
        echo "[start.sh] Auto-update completed, restarting immediately..."
        continue
    fi

    echo
    echo "[start.sh] Embot exited (code ${EXIT_CODE:-0}). Restarting in 3s (Ctrl+C to stop)..."
    sleep 3 || break
done
