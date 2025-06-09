# config.py
VERSION = "1.0"
GUILD_ID = 1229868495307669605
PROJECTS_CHANNEL_NAME = "projects"
ARTWORK_CHANNEL_NAME = "artwork"
PROJECT_DISCUSSION_CHANNEL_NAME = "project-discussion"
GENERAL_CHANNEL_NAME = "general"
ANNOUNCEMENTS_CHANNEL_NAME = "announcements"
TRACKER_CHANNEL_NAME = "tracker-updates"
REACTION_EMOJIS = ["🔥", "😐", "🗑️"]
MAX_CACHE = 5000

# Google Sheets
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SHEET_ID = "1x9tTOOqH5WpKOoptdQzABSN_x8oZbMgzIGlGH9w1IKA"
CREDS_FILE = "creds.json"

# Leveling system
REACTION_WEIGHTS = {
    "🔥": 5,
    "😐": 0,
    "🗑️": -5
}