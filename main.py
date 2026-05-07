"""Launcher for Embot. Handles token loading and starts the bot."""
import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(SCRIPT_DIR))

TOKEN_FILE = SCRIPT_DIR / "config" / "token"
if not TOKEN_FILE.exists():
    sys.exit(f"Token file not found: {TOKEN_FILE}")
with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
    data = json.load(f)
TOKEN = data.get("bot_token", "")
if not TOKEN:
    sys.exit(f"No bot_token in: {TOKEN_FILE}")

from Embot import run_bot
run_bot(TOKEN)
