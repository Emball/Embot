# config.py
import logging
from bot_token import *

# Basic configuration
VERSION = "2.5"
GUILD_ID = 1229868495307669605
MAX_CACHE = 5000

# Channel names
PROJECTS_CHANNEL_NAME = "projects"
ARTWORK_CHANNEL_NAME = "artwork"
SUGGESTIONS_CHANNEL_NAME = "suggestions"
PROJECT_DISCUSSION_CHANNEL_NAME = "project-discussion"
GENERAL_CHANNEL_NAME = "general"
ANNOUNCEMENTS_CHANNEL_NAME = "announcements"
TRACKER_CHANNEL_NAME = "tracker-updates"
BOT_LOGS_CHANNEL_NAME = "bot-logs"

# Reaction configuration
REACTION_EMOJIS = ["🔥", "😐", "🗑️"]  # For projects/artwork
SUGGESTION_EMOJIS = ["👍", "👎"]  # For suggestions channel

# XP weights for reactions
REACTION_WEIGHTS = {
    # Projects/Artwork reactions
    "🔥": 5,    # Fire = +5 XP
    "😐": 0,    # Neutral = 0 XP  
    "🗑️": -5,   # Trash = -5 XP
    
    # Suggestions reactions
    "👍": 1,    # Upvote = +1 XP
    "👎": -1    # Downvote = -1 XP
}

# Logging configuration
LOG_FORMAT = "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def setup_logging():
    """Configure logging for the entire bot"""
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT
    )
    logging.getLogger('discord').setLevel(logging.WARNING)

# XP rewards
XP_REWARDS = {
    "project": 20,
    "artwork": 20,
    "suggestion": 10,
    "message": 5,
    "voice_minute": 1
}