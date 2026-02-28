# [file name]: moderation.py
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import re
from datetime import datetime, timedelta
import asyncio
from typing import Optional, Union, Dict, List
import json
import os
from pathlib import Path
import io
from PIL import Image, ImageDraw, ImageFont
import pytz
import tempfile
from collections import deque
from cryptography.fernet import Fernet

MODULE_NAME = "MODERATION"

# ==================== CONFIGURATION ====================

OWNER_ID = 1328822521084117033  # from mod_oversight

CONFIG = {
    "owner_id": OWNER_ID,
    "channels": {
        "join_logs_channel_id": 1229868495307669608,
        "bot_logs_channel_id": 1229871835978666115
    },
    "elevated_roles": ["Moderator", "Admin", "Owner"],
    "moderation": {
        "min_reason_length": 10,
        "muted_role_name": "Muted"
    },
    "oversight": {
        "report_time_cst": "00:00",
        "context_message_count": 30,
        "invite_cleanup_days": 7
    }
}

# Severity categories
CHILD_SAFETY = ["child porn", "Teen leaks"]
RACIAL_SLURS = ["chink", "beaner", "n i g g e r", "nigger", "nigger'", "Nigger", 
                "niggers", "niiger", "niigger"]
TOS_VIOLATIONS = []
BANNED_WORDS = [
    "embis", "embis'", "Embis", "embis!", "Embis!", "embis's", "embiss", "embiz",
    "https://www.youtube.com/watch?v=fXvOrWWB3Vg", "https://youtu.be/fXvOrWWB3Vg",
    "https://youtu.be/fXvOrWWB3Vg?si=rSS11Yf2si_MVauu", "leaked porn", "nudes leak",
    "mbis", "m'bis", "Mbis", "mbs", "mebis", "Michael Blake Sinclair", 
    "Michael Sinclair", "montear", "www.youtube.com/watch?v=fXvOrWWB3Vg", 
    "youtube.com/watch?v=fXvOrWWB3Vg"
]

ELEVATED_ROLES = CONFIG["elevated_roles"]
MIN_REASON_LENGTH = CONFIG["moderation"]["min_reason_length"]
MUTED_ROLE_NAME = CONFIG["moderation"]["muted_role_name"]
CONTEXT_MESSAGE_COUNT = CONFIG["oversight"]["context_message_count"]

# Standard Errors
ERROR_NO_PERMISSION = "❌ You need a moderation role (Moderator, Admin, or Owner) to use this command."
ERROR_REASON_REQUIRED = "❌ You must provide a reason for this action."
ERROR_REASON_TOO_SHORT = f"❌ Reason must be at least {MIN_REASON_LENGTH} characters long."
ERROR_CANNOT_ACTION_SELF = "❌ You cannot perform this action on yourself."
ERROR_CANNOT_ACTION_BOT = "❌ I cannot perform this action on myself."
ERROR_HIGHER_ROLE = "❌ You cannot perform this action on someone with a higher or equal role."

# ==================== HELPERS ====================

def has_elevated_role(member: discord.Member) -> bool:
    if member.guild.owner_id == member.id:
        return True
    return any(role.name in ELEVATED_ROLES for role in member.roles)

def validate_reason(reason: Optional[str]) -> tuple:
    if not reason or reason.strip() == "" or reason == "No reason provided":
        return False, ERROR_REASON_REQUIRED
    if len(reason) < MIN_REASON_LENGTH:
        return False, ERROR_REASON_TOO_SHORT
    return True, None

def parse_duration(duration: str) -> tuple:
    """Parse a duration string like '10m', '2h', '1d'. Returns (seconds, label)."""
    if not duration:
        return None, "Permanent"
    m = re.match(r'^(\d+)([smhd])$', duration.lower())
    if not m:
        return None, "Permanent"
    value, unit = int(m.group(1)), m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    labels      = {'s': 'second', 'm': 'minute', 'h': 'hour', 'd': 'day'}
    seconds = value * multipliers[unit]
    label   = f"{value} {labels[unit]}{'s' if value != 1 else ''}"
    return seconds, label

def get_event_logger(bot):
    """Return the logger's EventLogger if available."""
    return getattr(bot, '_logger_event_logger', None)

def matches_banned_term(term: str, content_lower: str) -> bool:
    """
    Match a banned term against lowercased message content.
    - URLs (contain '://' or 'www.') use a plain substring match since word
      boundaries don't apply to URLs.
    - Everything else uses \\b word-boundary matching so that 'embis' won't
      fire on 'fembis', and 'mbis' won't fire on 'crumbs'.
    """
    term_lower = term.lower()
    if "://" in term_lower or "www." in term_lower:
        return term_lower in content_lower
    return bool(re.search(r'\b' + re.escape(term_lower) + r'\b', content_lower))

# ==================== UNIFIED CONTEXT ====================

class ModContext:
    """
    Wraps either a discord.Interaction (slash) or commands.Context (prefix)
    into a single interface so command logic never has to branch on which one it got.
    """
    def __init__(self, source):
        self._source = source
        self._replied = False

        if isinstance(source, discord.Interaction):
            self.guild = source.guild
            self.channel = source.channel
            self.author = source.user
            self.bot = source.client
            self.message = None
        else:
            self.guild = source.guild
            self.channel = source.channel
            self.author = source.author
            self.bot = source.bot
            self.message = source.message

    async def reply(self, content=None, *, embed=None, ephemeral=False, delete_after=None):
        msg_obj = None
        if isinstance(self._source, discord.Interaction):
            if not self._replied:
                self._replied = True
                await self._source.response.send_message(
                    content=content, embed=embed, ephemeral=ephemeral)
                if not ephemeral:
                    try:
                        msg_obj = await self._source.original_response()
                    except Exception:
                        pass
            else:
                msg_obj = await self._source.followup.send(
                    content=content, embed=embed, ephemeral=ephemeral)
        else:
            msg_obj = await self._source.send(content=content, embed=embed)
            if delete_after and msg_obj:
                await msg_obj.delete(delay=delete_after)
        
        return msg_obj.id if msg_obj else None

    async def error(self, message: str):
        if isinstance(self._source, discord.Interaction):
            await self.reply(message, ephemeral=True)
        else:
            await self.reply(message, delete_after=8)

    async def defer(self):
        if isinstance(self._source, discord.Interaction):
            await self._source.response.defer()
        self._replied = True

    async def followup(self, content=None, *, embed=None, ephemeral=False):
        if isinstance(self._source, discord.Interaction):
            msg = await self._source.followup.send(
                content=content, embed=embed, ephemeral=ephemeral)
            return msg.id if msg else None
        else:
            msg = await self._source.send(content=content, embed=embed)
            return msg.id if msg else None

# ==================== VIEWS & MODALS ====================

class BanAppealView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @ui.button(label="Submit Appeal", style=discord.ButtonStyle.primary, emoji="📝")
    async def appeal_button(self, interaction: discord.Interaction, button: ui.Button):
        # The oversight system is now part of the bot's moderation attribute
        if not hasattr(interaction.client, 'moderation') or not hasattr(interaction.client.moderation, 'submit_appeal'):
            await interaction.response.send_message("❌ Appeal system not available.", ephemeral=True)
            return
        modal = BanAppealModal(interaction.client.moderation, self.guild_id)
        await interaction.response.send_modal(modal)

class ActionReviewView(ui.View):
    """View with buttons for reviewing mod actions"""
    def __init__(self, moderation_system, action_id: str, action: Dict):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.action_id = action_id
        self.action = action

    @ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.moderation.approve_action(self.action_id)
        if success:
            await interaction.response.send_message("✅ Action approved and removed from pending.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to approve action.", ephemeral=True)

    @ui.button(label="Revert", style=discord.ButtonStyle.red, emoji="↩️")
    async def revert_button(self, interaction: discord.Interaction, button: ui.Button):
        guild = self.moderation.bot.get_guild(self.action['guild_id'])
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
            return
        success = await self.moderation.revert_action(self.action_id, guild)
        if success:
            await interaction.response.send_message("↩️ Action reverted successfully.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to revert action.", ephemeral=True)

    @ui.button(label="View Chat", style=discord.ButtonStyle.gray, emoji="💬")
    async def view_chat_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.action.get('channel_id') or not self.action.get('message_id'):
            await interaction.response.send_message("❌ No chat link available.", ephemeral=True)
            return
        guild_id = self.action['guild_id']
        channel_id = self.action['channel_id']
        message_id = self.action['message_id']
        jump_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        await interaction.response.send_message(f"📍 [Jump to message]({jump_link})", ephemeral=True)

class AppealReviewView(ui.View):
    """View with buttons for reviewing ban appeals"""
    def __init__(self, moderation_system, appeal_id: str):
        super().__init__(timeout=None)
        self.moderation = moderation_system
        self.appeal_id = appeal_id

    @ui.button(label="Accept Appeal", style=discord.ButtonStyle.green, emoji="✅")
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.moderation.approve_appeal(self.appeal_id)
        if success:
            await interaction.response.send_message("✅ Appeal accepted and user unbanned.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to accept appeal.", ephemeral=True)

    @ui.button(label="Deny Appeal", style=discord.ButtonStyle.red, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: ui.Button):
        success = await self.moderation.deny_appeal(self.appeal_id)
        if success:
            await interaction.response.send_message("❌ Appeal denied.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("❌ Failed to deny appeal.", ephemeral=True)

class BanAppealModal(ui.Modal, title="Ban Appeal"):
    """Modal for submitting a ban appeal"""
    appeal_text = ui.TextInput(
        label="Why should you be unbanned?",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why you believe the ban should be lifted...",
        required=True,
        max_length=1000
    )

    def __init__(self, moderation_system, guild_id: int):
        super().__init__()
        self.moderation = moderation_system
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        appeal_id = await self.moderation.submit_appeal(
            interaction.user.id,
            self.guild_id,
            self.appeal_text.value
        )
        embed = discord.Embed(
            title="✅ Appeal Submitted",
            description="Your ban appeal has been submitted and will be reviewed.",
            color=0x2ecc71,
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="What happens next?",
            value="The server owner will review your appeal and you'll be notified of the decision.",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ==================== MEDIA SAFETY SCANNER ====================

from dataclasses import dataclass, field as _field

# Labels NudeNet considers explicit
_EXPLICIT_LABELS = {
    "EXPOSED_ANUS", "EXPOSED_BUTTOCKS", "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F", "EXPOSED_GENITALIA_M", "EXPOSED_BELLY",
}
_NUDENET_THRESHOLD = 0.45
_AGE_THRESHOLD     = 20   # block if any face appears younger than this
_SCAN_IMAGE_EXTS   = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif'}
_SCAN_VIDEO_EXTS   = {'.mp4', '.mov', '.webm', '.avi', '.mkv'}

# Lazy model handles — loaded once on first scan
_nudenet_model     = None
_deepface_ready    = False

def _load_nudenet():
    global _nudenet_model
    if _nudenet_model is None:
        try:
            from nudenet import NudeClassifier  # type: ignore
            _nudenet_model = NudeClassifier()
        except ImportError:
            pass  # NudeNet not installed — scanner will fail-open for nudity stage
        except Exception as e:
            logging.getLogger("MediaScanner").error(f"NudeNet load failed: {e}")
    return _nudenet_model

def _load_deepface():
    global _deepface_ready
    try:
        import deepface  # noqa  type: ignore
        _deepface_ready = True
    except ImportError:
        _deepface_ready = False
    return _deepface_ready

import logging as _logging
_scanner_log = _logging.getLogger("MediaScanner")

@dataclass
class _FileScanResult:
    filename: str
    scannable: bool
    explicit: bool = False
    min_age: Optional[float] = None
    blocked: bool = False
    reason: str = ""

@dataclass
class ScanVerdict:
    blocked: bool
    safe_files: list = _field(default_factory=list)
    blocked_files: list = _field(default_factory=list)

class MediaScanner:
    """Pre-upload CSAM safety scanner. Instantiated once inside ModerationSystem."""

    def __init__(self, bot, owner_id: int, get_bot_logs_fn):
        self.bot          = bot
        self.owner_id     = owner_id
        self._get_bot_logs = get_bot_logs_fn
        self._models_loaded = False
        self._load_lock   = asyncio.Lock()

    async def _ensure_models(self):
        if self._models_loaded:
            return
        async with self._load_lock:
            if self._models_loaded:
                return
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _load_nudenet)
            await loop.run_in_executor(None, _load_deepface)
            self._models_loaded = True

    async def scan_files(self, files_data: list, guild=None, context: str = "") -> ScanVerdict:
        """Scan a files_data list ({'filename', 'data'}) before re-hosting. Returns ScanVerdict."""
        await self._ensure_models()
        loop   = asyncio.get_event_loop()
        safe, blocked = [], []

        for entry in files_data:
            filename: str = entry['filename']
            data: bytes   = entry['data']
            ext = os.path.splitext(filename.lower())[1]

            if ext not in _SCAN_IMAGE_EXTS and ext not in _SCAN_VIDEO_EXTS:
                safe.append(entry)
                continue

            result = _FileScanResult(filename=filename, scannable=True)
            try:
                is_explicit = await loop.run_in_executor(None, self._nudenet_scan, data, ext)
                result.explicit = is_explicit
                if is_explicit:
                    min_age = await loop.run_in_executor(None, self._deepface_age, data)
                    result.min_age = min_age
                    if min_age is not None and min_age < _AGE_THRESHOLD:
                        result.blocked = True
                        result.reason  = f"Explicit + apparent age {min_age:.1f} < {_AGE_THRESHOLD}"
            except Exception as e:
                result.blocked = True
                result.reason  = f"Scan error (blocked as precaution): {e}"
                _scanner_log.error(f"Scan error [{filename}]: {e}")

            if result.blocked:
                blocked.append(result)
                _scanner_log.warning(f"BLOCKED [{filename}]: {result.reason}")
            else:
                safe.append(entry)

        verdict = ScanVerdict(blocked=bool(blocked), safe_files=safe, blocked_files=blocked)
        if verdict.blocked:
            await self._alert(verdict, guild, context)
        return verdict

    # ── Blocking model calls (run in executor) ─────────────────────────────────

    def _nudenet_scan(self, data: bytes, ext: str) -> bool:
        if _nudenet_model is None:
            return False
        import tempfile
        if ext in _SCAN_VIDEO_EXTS:
            data = self._first_frame(data)
            if data is None:
                return False
            ext = '.jpg'
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            result = _nudenet_model.classify(path)
            preds  = list(result.values())[0] if result else {}
            return any(
                lbl in _EXPLICIT_LABELS and conf >= _NUDENET_THRESHOLD
                for lbl, conf in preds.items()
            )
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def _deepface_age(self, data: bytes) -> Optional[float]:
        if not _deepface_ready:
            return None
        try:
            from deepface import DeepFace  # type: ignore
            import numpy as np
            from PIL import Image as _Image
            arr     = np.array(_Image.open(io.BytesIO(data)).convert("RGB"))
            results = DeepFace.analyze(arr, actions=["age"], enforce_detection=False, silent=True)
            if isinstance(results, dict):
                results = [results]
            ages = [float(r["age"]) for r in results if r.get("age") is not None]
            return min(ages) if ages else None
        except Exception as e:
            _scanner_log.warning(f"DeepFace age error: {e}")
            return None

    def _first_frame(self, video_data: bytes) -> Optional[bytes]:
        try:
            import cv2, numpy as np, tempfile  # type: ignore
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp:
                tmp.write(video_data)
                path = tmp.name
            try:
                cap = cv2.VideoCapture(path)
                ret, frame = cap.read()
                cap.release()
                if not ret or frame is None:
                    return None
                _, buf = cv2.imencode('.jpg', frame)
                return buf.tobytes()
            finally:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        except ImportError:
            return None
        except Exception as e:
            _scanner_log.error(f"First-frame extraction failed: {e}")
            return None

    # ── Alert (no image content ever included) ─────────────────────────────────

    async def _alert(self, verdict: ScanVerdict, guild, context: str):
        summary = "\n".join(f"• `{r.filename}` — {r.reason}" for r in verdict.blocked_files)
        embed = discord.Embed(
            title="🚫 Re-host Blocked — Potential Illegal Content",
            description=(
                "One or more files were **blocked from re-hosting**. "
                "Encrypted copies have been deleted. "
                "**No image content is included in this report.**"
            ),
            color=0xff0000,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Context",          value=context or "*(none)*",   inline=False)
        embed.add_field(name=f"Blocked ({len(verdict.blocked_files)})", value=summary[:1024], inline=False)
        embed.add_field(
            name="Action Required",
            value=(
                "Review via Discord audit logs. If content is illegal, report to "
                "Discord Trust & Safety and NCMEC CyberTipline (missingkids.org)."
            ),
            inline=False,
        )
        embed.set_footer(text="No flagged content was uploaded to Discord CDN")
        try:
            owner = await self.bot.fetch_user(self.owner_id)
            if owner:
                await owner.send(embed=embed)
        except Exception:
            pass
        if guild:
            try:
                ch = self._get_bot_logs(guild)
                if ch:
                    await ch.send(embed=embed)
            except Exception:
                pass


# ==================== MODERATION SYSTEM (UNIFIED) ====================

class ModerationSystem:
    """
    Unified moderation and oversight system.
    Handles all moderation actions, strikes, mutes, role persistence,
    auto-mod, and oversight (logging, appeals, daily reports).
    """

    def __init__(self, bot):
        self.bot = bot
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)

        # Per-process Fernet key — generated fresh each run.
        self._fernet = Fernet(Fernet.generate_key())

        # Encrypted media staging directory
        self.media_dir = data_dir / "media_cache"
        self.media_dir.mkdir(exist_ok=True)

        # Pre-upload safety scanner
        self.scanner = MediaScanner(bot, OWNER_ID, self._get_bot_logs_channel)

        # Data files
        self.roles_file = data_dir / "member_roles.json"
        self.strikes_file = data_dir / "moderation_strikes.json"
        self.mutes_file = data_dir / "muted_users.json"
        self.oversight_file = data_dir / "mod_oversight_data.json"
        self.appeals_file = data_dir / "ban_appeals.json"
        self.invites_file = data_dir / "ban_reversal_invites.json"

        # Load all data
        self.role_cache = self._load_json(self.roles_file, {})
        self.strikes = self._load_json(self.strikes_file, {})
        self.mutes = self._load_json(self.mutes_file, {})
        self.pending_actions = self._load_json(self.oversight_file, {})
        self.appeals = self._load_json(self.appeals_file, {})
        self.invites = self._load_json(self.invites_file, {})

        # Bot-log cache: tracks last 500 messages sent to bot-logs so deletions can be countered.
        # bot_log_cache maps discord message_id -> log record dict
        # bot_log_order is a deque of message_ids in insertion order for LRU eviction
        BOT_LOG_CACHE_SIZE = 500
        self._bot_log_cache: Dict[int, Dict] = {}
        self._bot_log_order: deque = deque()
        self._bot_log_cache_size = BOT_LOG_CACHE_SIZE

        # Tracks message_ids of deletion-warning messages (perpetual resend on delete)
        # warning_message_id -> original_log_id
        self._deletion_warnings: Dict[int, str] = {}

        # Deletion attempt log: list of dicts for daily report
        # Each: {'log_id', 'deleter', 'deleter_id', 'timestamp', 'original_title'}
        self.deletion_attempts: list = []

        # Message cache for context (guild_id -> channel_id -> list)
        self.message_cache = {}

        # Media cache index: message_id -> {'files': [{'filename': str, 'path': Path, 'content_type': str}], 'author_id': int, 'guild_id': int}
        # Actual file bytes are stored AES-encrypted on disk under self.media_dir and deleted on eviction/re-host.
        self.media_cache = {}

        # Tracked embeds for deletion monitoring
        self.tracked_embeds = {}  # message_id -> {'action_id': str, 'type': str}

        # Start background tasks
        self.check_expired_mutes.start()
        self.cleanup_invites.start()
        self.send_daily_report.start()

        self.bot.logger.log(MODULE_NAME, "Moderation system initialized")

    # ==================== JSON HELPERS ====================

    def _load_json(self, filepath, default):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to load {filepath}", e)
            return default

    def _save_json(self, filepath, data):
        try:
            # Write to temporary file first for atomicity
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, filepath)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to save {filepath}", e)

    # ==================== ROLE PERSISTENCE ====================

    def save_member_roles(self, member: discord.Member):
        gk, uk = str(member.guild.id), str(member.id)
        self.role_cache.setdefault(gk, {})[uk] = {
            'role_ids': [r.id for r in member.roles if r.id != member.guild.id],
            'saved_at': datetime.utcnow().isoformat(),
            'username': str(member)
        }
        self._save_json(self.roles_file, self.role_cache)

    async def restore_member_roles(self, member: discord.Member):
        gk, uk = str(member.guild.id), str(member.id)
        saved = self.role_cache.get(gk, {}).get(uk)
        if not saved:
            return
        roles = [member.guild.get_role(rid) for rid in saved.get('role_ids', [])]
        roles = [r for r in roles if r]
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Role persistence - restoring previous roles")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to restore roles for {member}", e)

    # ==================== STRIKE SYSTEM ====================

    def add_strike(self, user_id, reason) -> int:
        key = str(user_id)
        self.strikes.setdefault(key, []).append({
            'timestamp': datetime.utcnow().isoformat(),
            'reason': reason
        })
        self._save_json(self.strikes_file, self.strikes)
        return len(self.strikes[key])

    def get_strikes(self, user_id) -> int:
        return len(self.strikes.get(str(user_id), []))

    def get_strike_details(self, user_id) -> list:
        return self.strikes.get(str(user_id), [])

    def clear_strikes(self, user_id) -> bool:
        key = str(user_id)
        if key in self.strikes:
            del self.strikes[key]
            self._save_json(self.strikes_file, self.strikes)
            return True
        return False

    # ==================== MUTE MANAGER ====================

    def add_mute(self, guild_id, user_id, reason, moderator, duration_seconds=None):
        gk, uk = str(guild_id), str(user_id)
        expiry = None
        if duration_seconds:
            expiry = (datetime.utcnow() + timedelta(seconds=duration_seconds)).isoformat()
        self.mutes.setdefault(gk, {})[uk] = {
            'user_id': user_id,
            'reason': reason,
            'moderator': str(moderator),
            'timestamp': datetime.utcnow().isoformat(),
            'duration_seconds': duration_seconds,
            'expiry_time': expiry
        }
        self._save_json(self.mutes_file, self.mutes)

    def remove_mute(self, guild_id, user_id):
        gk, uk = str(guild_id), str(user_id)
        if gk in self.mutes and uk in self.mutes[gk]:
            del self.mutes[gk][uk]
            self._save_json(self.mutes_file, self.mutes)

    def is_muted(self, guild_id, user_id) -> bool:
        return str(user_id) in self.mutes.get(str(guild_id), {})

    def get_expired_mutes(self) -> list:
        expired, now = [], datetime.utcnow()
        for gk, users in self.mutes.items():
            for uk, data in users.items():
                expiry = data.get('expiry_time')
                if expiry:
                    try:
                        if now >= datetime.fromisoformat(expiry):
                            expired.append({
                                'guild_id': int(gk),
                                'user_id': data['user_id'],
                                'user_key': uk,
                                'guild_key': gk
                            })
                    except (ValueError, AttributeError):
                        pass
        return expired

    # ==================== OVERSIGHT: CONTEXT & CACHE ====================

    # ==================== MEDIA CACHE HELPERS ====================

    def _encrypt_to_disk(self, message_id: int, index: int, data: bytes) -> Path:
        """Encrypt raw attachment bytes and write to a uniquely named file on disk."""
        encrypted = self._fernet.encrypt(data)
        path = self.media_dir / f"{message_id}_{index}.enc"
        path.write_bytes(encrypted)
        return path

    def _decrypt_from_disk(self, path: Path) -> bytes:
        """Read an encrypted file from disk and return the original bytes."""
        return self._fernet.decrypt(path.read_bytes())

    def _delete_media_files(self, message_id: int):
        """Delete all encrypted files on disk for a given message and remove from index."""
        entry = self.media_cache.pop(message_id, None)
        if not entry:
            return
        for f in entry['files']:
            try:
                f['path'].unlink(missing_ok=True)
            except Exception:
                pass

    async def cache_message(self, message: discord.Message):
        """Cache a message for context logging and encrypt any media attachments to disk."""
        if message.guild is None or message.author.bot:
            return
        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        if guild_id not in self.message_cache:
            self.message_cache[guild_id] = {}
        if channel_id not in self.message_cache[guild_id]:
            self.message_cache[guild_id][channel_id] = []

        # Download and encrypt each attachment to disk
        downloaded = []
        for idx, att in enumerate(message.attachments):
            self.bot.logger.log(MODULE_NAME,
                f"[MEDIA-CACHE] Attempting to cache attachment '{att.filename}' "
                f"(id={att.id}, size={att.size}, content_type={att.content_type}) "
                f"for msg {message.id}")
            try:
                data = await att.read()
                self.bot.logger.log(MODULE_NAME,
                    f"[MEDIA-CACHE] Downloaded {len(data)} bytes for '{att.filename}' (msg {message.id})")
                path = self._encrypt_to_disk(message.id, idx, data)
                self.bot.logger.log(MODULE_NAME,
                    f"[MEDIA-CACHE] Encrypted to disk: {path} for '{att.filename}' (msg {message.id})")
                downloaded.append({
                    'filename': att.filename,
                    'path': path,
                    'content_type': att.content_type or 'application/octet-stream',
                    'url': att.url,
                })
            except Exception as e:
                self.bot.logger.log(MODULE_NAME,
                    f"[MEDIA-CACHE] FAILED to cache attachment '{att.filename}' "
                    f"for msg {message.id}: {e}", "WARNING")

        if downloaded:
            self.media_cache[message.id] = {
                'files': downloaded,
                'author_id': message.author.id,
                'guild_id': message.guild.id,
            }
            self.bot.logger.log(MODULE_NAME,
                f"[MEDIA-CACHE] Stored {len(downloaded)} file(s) in media_cache for msg {message.id}")
        elif message.attachments:
            self.bot.logger.log(MODULE_NAME,
                f"[MEDIA-CACHE] msg {message.id} had {len(message.attachments)} attachment(s) "
                f"but none were successfully cached", "WARNING")

        msg_data = {
            'id': message.id,
            'author': str(message.author),
            'author_id': message.author.id,
            'content': message.content,
            'timestamp': message.created_at.isoformat(),
            'attachments': [att.url for att in message.attachments],
            'embeds': len(message.embeds)
        }
        self.message_cache[guild_id][channel_id].append(msg_data)
        # Keep only last 100 messages per channel; evict + delete encrypted files for pushed-out message
        if len(self.message_cache[guild_id][channel_id]) > 100:
            evicted = self.message_cache[guild_id][channel_id].pop(0)
            evicted_id = evicted.get('id')
            if evicted_id:
                self._delete_media_files(evicted_id)

    def get_context_messages(self, guild_id: int, channel_id: int, around_message_id: int, count: int = None) -> List[Dict]:
        """Get messages around a specific message ID."""
        if count is None:
            count = CONTEXT_MESSAGE_COUNT
        guild_key = str(guild_id)
        channel_key = str(channel_id)
        if guild_key not in self.message_cache or channel_key not in self.message_cache[guild_key]:
            return []
        messages = self.message_cache[guild_key][channel_key]
        target_idx = None
        for i, msg in enumerate(messages):
            if msg['id'] == around_message_id:
                target_idx = i
                break
        if target_idx is None:
            return messages[-count:]
        half = count // 2
        start = max(0, target_idx - half)
        end = min(len(messages), target_idx + half + 1)
        return messages[start:end]

    def generate_context_screenshot(self, messages: List[Dict], highlighted_msg_id: Optional[int] = None) -> io.BytesIO:
        """Generate a synthetic screenshot of message context."""
        width = 800
        line_height = 60
        padding = 20
        height = len(messages) * line_height + padding * 2
        img = Image.new('RGB', (width, height), color='#36393f')
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
            font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except:
            font = ImageFont.load_default()
            font_bold = font
        y = padding
        for msg in messages:
            if highlighted_msg_id and msg['id'] == highlighted_msg_id:
                draw.rectangle([0, y - 5, width, y + line_height - 5], fill='#4a4d52')
            timestamp = datetime.fromisoformat(msg['timestamp']).strftime("%H:%M")
            author_text = f"{msg['author']} - {timestamp}"
            draw.text((padding, y), author_text, fill='#7289da', font=font_bold)
            content = msg['content'][:100]
            if msg['content'] and len(msg['content']) > 100:
                content += "..."
            if not content and msg['attachments']:
                content = "[Attachment]"
            if not content and msg['embeds'] > 0:
                content = "[Embed]"
            if not content:
                content = "[Empty message]"
            draw.text((padding, y + 20), content, fill='#dcddde', font=font)
            y += line_height
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer

    # ==================== OVERSIGHT: LOGGING ACTIONS ====================

    async def log_mod_action(self, action_data: Dict) -> Optional[str]:
        """
        Log a moderation action. Returns action ID if logged, None if ignored (mute/warn/timeout).
        """
        if action_data['action'] in ['mute', 'warn', 'timeout']:
            return None
        action_id = f"{action_data['guild_id']}_{action_data['action']}_{int(datetime.utcnow().timestamp())}"
        context_messages = []
        if 'message_id' in action_data and 'channel_id' in action_data:
            context_messages = self.get_context_messages(
                action_data['guild_id'],
                action_data['channel_id'],
                action_data['message_id']
            )
        action_record = {
            'id': action_id,
            'action': action_data['action'],
            'moderator_id': action_data['moderator_id'],
            'moderator': action_data['moderator'],
            'user_id': action_data.get('user_id'),
            'user': action_data.get('user'),
            'reason': action_data['reason'],
            'guild_id': action_data['guild_id'],
            'channel_id': action_data.get('channel_id'),
            'message_id': action_data.get('message_id'),
            'timestamp': datetime.utcnow().isoformat(),
            'context_messages': context_messages,
            'duration': action_data.get('duration'),
            'additional': action_data.get('additional', {}),
            'flags': [],
            'embed_ids': {'inchat': None, 'botlog': None},
            'status': 'pending'
        }
        self.pending_actions[action_id] = action_record
        self._save_json(self.oversight_file, self.pending_actions)
        self.bot.logger.log(MODULE_NAME, f"Logged mod action: {action_id} by {action_data['moderator']}")
        return action_id

    def resolve_pending_action(self, user_id: int, action_type: str):
        """Remove pending actions for a user when manually undone."""
        to_delete = []
        for aid, act in self.pending_actions.items():
            if act.get('user_id') == user_id and act.get('action') == action_type and act.get('status') == 'pending':
                to_delete.append(aid)
        if to_delete:
            for aid in to_delete:
                del self.pending_actions[aid]
            self._save_json(self.oversight_file, self.pending_actions)
            self.bot.logger.log(MODULE_NAME, f"Resolved {len(to_delete)} pending {action_type} for user {user_id}")
            return True
        return False

    async def send_cached_media_to_logs(self, guild: discord.Guild, message_id: int, author_str: str, reason: str, extra_content: str = None):
        """Decrypt cached media from disk and upload to bot-logs with fresh Discord-hosted links."""
        bot_logs = self._get_bot_logs_channel(guild)
        if not bot_logs:
            return
        cached = self.media_cache.get(message_id)
        if not cached or not cached['files']:
            return
        files = []
        for f in cached['files']:
            try:
                data = self._decrypt_from_disk(f['path'])
                files.append(discord.File(fp=io.BytesIO(data), filename=f['filename']))
            except Exception as e:
                self.bot.logger.log(MODULE_NAME, f"Failed to decrypt cached file {f['filename']}: {e}", "WARNING")
        if not files:
            return
        embed = discord.Embed(
            title=reason,
            color=discord.Color.orange(),
            timestamp=__import__('datetime').datetime.utcnow(),
        )
        embed.add_field(name="User", value=author_str, inline=True)
        embed.add_field(name="Message ID", value=str(message_id), inline=True)
        if extra_content:
            embed.add_field(name="Message Content", value=extra_content[:1024] or "*empty*", inline=False)
        embed.set_footer(text=f"{len(files)} attachment(s) re-hosted below")
        await self.send_bot_log(guild, embed, files_data=cached['files'])

    def track_embed(self, message_id: int, action_id: str, embed_type: str):
        """Track an embed for deletion monitoring."""
        self.tracked_embeds[message_id] = {'action_id': action_id, 'type': embed_type}
        if action_id in self.pending_actions:
            self.pending_actions[action_id]['embed_ids'][embed_type] = message_id
            self._save_json(self.oversight_file, self.pending_actions)

    async def handle_embed_deletion(self, message_id: int):
        """Handle when a tracked embed is deleted."""
        if message_id not in self.tracked_embeds:
            return
        info = self.tracked_embeds.pop(message_id)
        action_id = info['action_id']
        embed_type = info['type']
        if action_id not in self.pending_actions:
            return
        action = self.pending_actions[action_id]
        if embed_type == 'inchat':
            if 'inchat_deleted' not in action['flags']:
                action['flags'].append('inchat_deleted')
        else:
            if 'botlog_deleted' not in action['flags']:
                action['flags'].append('botlog_deleted')
        inchat_deleted = 'inchat_deleted' in action['flags']
        botlog_deleted = 'botlog_deleted' in action['flags']
        if inchat_deleted and botlog_deleted:
            if 'red_flag' not in action['flags']:
                action['flags'].append('red_flag')
                self.bot.logger.log(MODULE_NAME, f"🚩 RED FLAG: Both embeds deleted for action {action_id}", "WARNING")
        elif inchat_deleted or botlog_deleted:
            if 'yellow_flag' not in action['flags']:
                action['flags'].append('yellow_flag')
                self.bot.logger.log(MODULE_NAME, f"⚠️ YELLOW FLAG: Embed deleted for action {action_id}", "WARNING")
        self._save_json(self.oversight_file, self.pending_actions)

    # ==================== OVERSIGHT: ACTION REVIEW ====================

    async def approve_action(self, action_id: str) -> bool:
        if action_id not in self.pending_actions:
            return False
        action = self.pending_actions[action_id]
        action['status'] = 'approved'
        action['reviewed_at'] = datetime.utcnow().isoformat()
        del self.pending_actions[action_id]
        self._save_json(self.oversight_file, self.pending_actions)
        self.bot.logger.log(MODULE_NAME, f"Action {action_id} approved")
        return True

    async def revert_action(self, action_id: str, guild: discord.Guild) -> bool:
        if action_id not in self.pending_actions:
            return False
        action = self.pending_actions[action_id]
        if action['action'] == 'ban':
            return await self._revert_ban(action, guild)
        elif action['action'] == 'mute':
            return await self._revert_mute(action, guild)
        elif action['action'] == 'kick':
            action['status'] = 'reverted'
            action['reviewed_at'] = datetime.utcnow().isoformat()
            self._save_json(self.oversight_file, self.pending_actions)
            self.bot.logger.log(MODULE_NAME, f"Kick action {action_id} marked reverted (cannot undo)")
            return True
        # For other actions, just mark reverted
        action['status'] = 'reverted'
        action['reviewed_at'] = datetime.utcnow().isoformat()
        self._save_json(self.oversight_file, self.pending_actions)
        return True

    async def _revert_ban(self, action: Dict, guild: discord.Guild) -> bool:
        try:
            user_id = action['user_id']
            user = await self.bot.fetch_user(user_id)
            await guild.unban(user, reason="Ban reverted after review")
            invite_link = await self._create_ban_reversal_invite(guild, user_id)
            try:
                embed = discord.Embed(
                    title="Ban Reverted",
                    description=f"After reviewing your case, we've decided to revert your ban from **{guild.name}**.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Rejoin Server", value=f"You can rejoin using this invite:\n{invite_link}", inline=False)
                embed.set_footer(text="This invite is for you only and will not expire")
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            bot_logs = self._get_bot_logs_channel(guild)
            if bot_logs:
                log_embed = discord.Embed(
                    title="Ban Reverted (Review System)",
                    description=f"**{user}** ({user_id}) has been unbanned after review.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
                log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
                await self.send_bot_log(guild, log_embed)
            action['status'] = 'reverted'
            action['reviewed_at'] = datetime.utcnow().isoformat()
            del self.pending_actions[action['id']]
            self._save_json(self.oversight_file, self.pending_actions)
            self.bot.logger.log(MODULE_NAME, f"Ban reverted for user {user_id}")
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to revert ban {action['id']}", e)
            return False

    async def _revert_mute(self, action: Dict, guild: discord.Guild) -> bool:
        try:
            user_id = action['user_id']
            member = guild.get_member(user_id)
            if not member:
                return False
            muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
            if muted_role and muted_role in member.roles:
                await member.remove_roles(muted_role, reason="Mute reverted after review")
            try:
                embed = discord.Embed(
                    title="Mute Reverted",
                    description=f"After reviewing your case, your mute in **{guild.name}** has been reverted.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                await member.send(embed=embed)
            except discord.Forbidden:
                pass
            bot_logs = self._get_bot_logs_channel(guild)
            if bot_logs:
                log_embed = discord.Embed(
                    title="Mute Reverted (Review System)",
                    description=f"**{member}** has been unmuted after review.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Original Reason", value=action['reason'], inline=False)
                log_embed.add_field(name="Original Moderator", value=action['moderator'], inline=True)
                await self.send_bot_log(guild, log_embed)
            action['status'] = 'reverted'
            action['reviewed_at'] = datetime.utcnow().isoformat()
            del self.pending_actions[action['id']]
            self._save_json(self.oversight_file, self.pending_actions)
            self.bot.logger.log(MODULE_NAME, f"Mute reverted for user {user_id}")
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to revert mute {action['id']}", e)
            return False

    async def _create_ban_reversal_invite(self, guild: discord.Guild, user_id: int) -> str:
        try:
            channel = None
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).create_instant_invite:
                    channel = ch
                    break
            if not channel:
                return "Could not create invite - no suitable channel"
            invite = await channel.create_invite(
                max_uses=1,
                max_age=0,
                unique=True,
                reason=f"Ban reversal for user {user_id}"
            )
            key = f"{guild.id}_{user_id}"
            self.invites[key] = {
                'code': invite.code,
                'user_id': user_id,
                'guild_id': guild.id,
                'created_at': datetime.utcnow().isoformat()
            }
            self._save_json(self.invites_file, self.invites)
            return invite.url
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to create ban reversal invite", e)
            return "Error creating invite"

    def _get_bot_logs_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        bot_logs_id = CONFIG["channels"]["bot_logs_channel_id"]
        if bot_logs_id:
            return guild.get_channel(bot_logs_id)
        # Fallback to logger module
        logger = get_event_logger(self.bot)
        if logger:
            return logger.get_bot_logs_channel(guild)
        return None

    def _register_bot_log(self, message_id: int, log_id: str, embed: discord.Embed,
                           files_data: list = None, is_warning: bool = False,
                           warning_for_log_id: str = None):
        """Register a sent bot-log message in the rolling cache."""
        record = {
            'log_id': log_id,
            'message_id': message_id,
            'embed': {
                'title': embed.title,
                'description': embed.description,
                'color': embed.color.value if embed.color else 0,
                'fields': [{'name': f.name, 'value': f.value, 'inline': f.inline} for f in embed.fields],
                'footer': embed.footer.text if embed.footer else None,
                'image_url': embed.image.url if embed.image else None,
                'author_name': embed.author.name if embed.author else None,
                'author_icon': embed.author.icon_url if embed.author else None,
            },
            'files_data': files_data or [],  # list of {'filename': str, 'data': bytes}
            'is_warning': is_warning,
            'warning_for_log_id': warning_for_log_id,
            'timestamp': datetime.utcnow().isoformat(),
        }
        self._bot_log_cache[message_id] = record
        self._bot_log_order.append(message_id)
        # Evict oldest if over capacity
        while len(self._bot_log_order) > self._bot_log_cache_size:
            oldest_id = self._bot_log_order.popleft()
            self._bot_log_cache.pop(oldest_id, None)

    async def send_bot_log(self, guild: discord.Guild, embed: discord.Embed,
                           files_data: list = None, log_id: str = None) -> Optional[int]:
        """Send an embed to bot-logs and register it in the cache. Returns message_id.
        Any image/video files are scanned by MediaScanner before upload."""
        bot_logs = self._get_bot_logs_channel(guild)
        if not bot_logs:
            return None
        if log_id is None:
            log_id = f"LOG-{int(datetime.utcnow().timestamp() * 1000)}"

        # Scan files through MediaScanner before uploading
        if files_data:
            verdict = await self.scanner.scan_files(
                files_data,
                guild=guild,
                context=f"bot-log send (log_id={log_id})",
            )
            if verdict.blocked and not verdict.safe_files:
                # All files blocked — send embed-only with a note, no files
                embed.add_field(
                    name="⚠️ Attachment(s) Withheld",
                    value="One or more files were blocked by MediaScanner. The server owner has been alerted.",
                    inline=False,
                )
                files_data = None
            elif verdict.blocked:
                # Some files blocked — only upload the safe ones
                files_data = verdict.safe_files

        try:
            if files_data:
                discord_files = [
                    discord.File(fp=io.BytesIO(f['data']), filename=f['filename'])
                    for f in files_data
                ]
                msg = await bot_logs.send(embed=embed, files=discord_files)
            else:
                msg = await bot_logs.send(embed=embed)
            self._register_bot_log(msg.id, log_id, embed, files_data=files_data)
            return msg.id
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to send bot log: {e}")
            return None

    async def handle_bot_log_deletion(self, message_id: int, deleter: discord.Member, guild: discord.Guild):
        """Called when a bot-logs message is deleted by an elevated user."""
        record = self._bot_log_cache.get(message_id)
        if not record:
            return

        log_id = record['log_id']
        original_embed_data = record['embed']
        timestamp = datetime.utcnow()

        self.deletion_attempts.append({
            'log_id': log_id,
            'deleter': str(deleter),
            'deleter_id': deleter.id,
            'timestamp': timestamp.isoformat(),
            'original_title': original_embed_data.get('title') or '(no title)',
            'is_warning': record.get('is_warning', False),
        })

        self.bot.logger.log(MODULE_NAME,
            f"⚠️ Bot-log deletion attempted by {deleter} (ID: {deleter.id}) for log {log_id}", "WARNING")

        # Rebuild original embed, turn it red, append deletion attempt info — one single message
        embed = discord.Embed(
            title=original_embed_data.get('title'),
            description=original_embed_data.get('description'),
            color=0xff0000,
            timestamp=timestamp,
        )
        for field in original_embed_data.get('fields', []):
            embed.add_field(name=field['name'], value=field['value'], inline=field['inline'])
        if original_embed_data.get('author_name'):
            embed.set_author(
                name=original_embed_data['author_name'],
                icon_url=original_embed_data.get('author_icon') or discord.Embed.Empty,
            )
        if original_embed_data.get('image_url'):
            embed.set_image(url=original_embed_data['image_url'])
        embed.add_field(name="🚨 Deletion Attempted By",
                        value=f"{deleter.mention} (`{deleter}` | `{deleter.id}`)", inline=False)
        original_footer = original_embed_data.get('footer') or ''
        embed.set_footer(text=f"{original_footer + ' • ' if original_footer else ''}Log ID: {log_id} • Deleting this will cause it to repost")

        new_msg_id = await self.send_bot_log(guild, embed,
                                              files_data=record.get('files_data'),
                                              log_id=log_id)
        if new_msg_id:
            self._deletion_warnings[new_msg_id] = log_id

    # ==================== OVERSIGHT: APPEALS ====================

    async def submit_appeal(self, user_id: int, guild_id: int, appeal_text: str) -> str:
        appeal_id = f"{guild_id}_{user_id}_{int(datetime.utcnow().timestamp())}"
        appeal_data = {
            'id': appeal_id,
            'user_id': user_id,
            'guild_id': guild_id,
            'appeal_text': appeal_text,
            'submitted_at': datetime.utcnow().isoformat(),
            'status': 'pending'
        }
        self.appeals[appeal_id] = appeal_data
        self._save_json(self.appeals_file, self.appeals)
        self.bot.logger.log(MODULE_NAME, f"Appeal submitted: {appeal_id}")
        return appeal_id

    async def approve_appeal(self, appeal_id: str) -> bool:
        if appeal_id not in self.appeals:
            return False
        appeal = self.appeals[appeal_id]
        try:
            guild = self.bot.get_guild(appeal['guild_id'])
            if not guild:
                return False
            user = await self.bot.fetch_user(appeal['user_id'])
            await guild.unban(user, reason="Appeal approved")
            invite_link = await self._create_ban_reversal_invite(guild, appeal['user_id'])
            try:
                embed = discord.Embed(
                    title="Ban Appeal Approved",
                    description=f"Your appeal for **{guild.name}** has been approved!",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Rejoin Server", value=f"You can rejoin using this invite:\n{invite_link}", inline=False)
                embed.set_footer(text="Welcome back!")
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            bot_logs = self._get_bot_logs_channel(guild)
            if bot_logs:
                log_embed = discord.Embed(
                    title="Ban Appeal Approved",
                    description=f"**{user}** has been unbanned after appeal approval.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Appeal Text", value=appeal['appeal_text'][:1024], inline=False)
                await self.send_bot_log(guild, log_embed)
            appeal['status'] = 'approved'
            appeal['reviewed_at'] = datetime.utcnow().isoformat()
            del self.appeals[appeal_id]
            self._save_json(self.appeals_file, self.appeals)
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to approve appeal {appeal_id}", e)
            return False

    async def deny_appeal(self, appeal_id: str) -> bool:
        if appeal_id not in self.appeals:
            return False
        appeal = self.appeals[appeal_id]
        try:
            guild = self.bot.get_guild(appeal['guild_id'])
            user = await self.bot.fetch_user(appeal['user_id'])
            try:
                embed = discord.Embed(
                    title="Ban Appeal Denied",
                    description=f"Your appeal for **{guild.name}** has been reviewed and denied.",
                    color=0xe74c3c,
                    timestamp=datetime.utcnow()
                )
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            appeal['status'] = 'denied'
            appeal['reviewed_at'] = datetime.utcnow().isoformat()
            del self.appeals[appeal_id]
            self._save_json(self.appeals_file, self.appeals)
            return True
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Failed to deny appeal {appeal_id}", e)
            return False

    # ==================== OVERSIGHT: DAILY REPORT ====================

    async def send_action_review(self, owner: discord.User, action_id: str, action: Dict):
        embed = discord.Embed(
            title=f"🔍 {action['action'].upper()} Action Review",
            color=0xe74c3c if 'red_flag' in action['flags'] else
                  0xf39c12 if 'yellow_flag' in action['flags'] else 0x5865f2,
            timestamp=datetime.fromisoformat(action['timestamp'])
        )
        if action['flags']:
            flags_text = []
            if 'red_flag' in action['flags']:
                flags_text.append("🚩 **RED FLAG** - Both embeds deleted")
            elif 'yellow_flag' in action['flags']:
                flags_text.append("⚠️ **YELLOW FLAG** - Embed deleted")
            if 'inchat_deleted' in action['flags']:
                flags_text.append("❌ In-chat embed deleted")
            if 'botlog_deleted' in action['flags']:
                flags_text.append("❌ Bot-log embed deleted")
            embed.add_field(name="⚠️ Flags", value="\n".join(flags_text), inline=False)
        embed.add_field(name="Moderator", value=f"{action['moderator']} (ID: {action['moderator_id']})", inline=True)
        if action.get('user'):
            embed.add_field(name="User", value=f"{action['user']} (ID: {action['user_id']})", inline=True)
        embed.add_field(name="Reason", value=action['reason'], inline=False)
        if action.get('duration'):
            embed.add_field(name="Duration", value=action['duration'], inline=True)
        if action['context_messages']:
            embed.add_field(name="Context", value=f"{len(action['context_messages'])} messages logged", inline=True)
        view = ActionReviewView(self, action_id, action)
        await owner.send(embed=embed, view=view)
        if action['context_messages']:
            img = self.generate_context_screenshot(action['context_messages'], action.get('message_id'))
            await owner.send(file=discord.File(img, "context.png"))

    async def send_appeal_review(self, owner: discord.User, appeal_id: str, appeal: Dict):
        embed = discord.Embed(
            title="📝 Ban Appeal Review",
            color=0x9b59b6,
            timestamp=datetime.fromisoformat(appeal['submitted_at'])
        )
        embed.add_field(name="User ID", value=str(appeal['user_id']), inline=True)
        embed.add_field(name="Guild ID", value=str(appeal['guild_id']), inline=True)
        embed.add_field(name="Appeal Text", value=appeal['appeal_text'][:1024], inline=False)
        view = AppealReviewView(self, appeal_id)
        await owner.send(embed=embed, view=view)

    async def generate_daily_report(self):
        """Generate and send the daily report: only covers deletion attempts."""
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            if not owner:
                return

            # ── Bot-log deletion attempts ──
            botlog_attempts = list(self.deletion_attempts)
            self.deletion_attempts.clear()  # Reset for next period

            # ── Mod action deletion attempts (red/yellow flags from oversight) ──
            red_flags = []
            yellow_flags = []
            for action_id, action in self.pending_actions.items():
                flags = action.get('flags', [])
                if 'red_flag' in flags:
                    red_flags.append((action_id, action))
                elif 'yellow_flag' in flags:
                    yellow_flags.append((action_id, action))

            total_issues = len(botlog_attempts) + len(red_flags) + len(yellow_flags)

            if total_issues == 0:
                embed = discord.Embed(
                    title="📊 Daily Integrity Report",
                    description="✅ No deletion attempts or mod-action flags in the last 24 hours.",
                    color=0x2ecc71,
                    timestamp=datetime.utcnow()
                )
                await owner.send(embed=embed)
                return

            embed = discord.Embed(
                title="📊 Daily Integrity Report",
                description=(
                    f"**{len(botlog_attempts)}** bot-log deletion attempt(s)\n"
                    f"**{len(red_flags)}** 🚩 red-flag mod action(s)\n"
                    f"**{len(yellow_flags)}** ⚠️ yellow-flag mod action(s)"
                ),
                color=0xff4500,
                timestamp=datetime.utcnow()
            )
            await owner.send(embed=embed)

            # Detail: bot-log deletion attempts
            if botlog_attempts:
                detail = discord.Embed(
                    title="🗑️ Bot-Log Deletion Attempts",
                    color=0xff0000,
                    timestamp=datetime.utcnow()
                )
                for attempt in botlog_attempts[:20]:  # cap at 20 fields
                    detail.add_field(
                        name=f"Log `{attempt['log_id']}`",
                        value=(
                            f"**By:** {attempt['deleter']} (`{attempt['deleter_id']}`)\n"
                            f"**Original:** {attempt['original_title']}\n"
                            f"**At:** {attempt['timestamp'][:19].replace('T', ' ')} UTC"
                        ),
                        inline=False
                    )
                if len(botlog_attempts) > 20:
                    detail.set_footer(text=f"...and {len(botlog_attempts) - 20} more.")
                await owner.send(embed=detail)

            # Detail: red/yellow flag mod actions
            if red_flags or yellow_flags:
                for action_id, action in (red_flags + yellow_flags)[:10]:
                    await self.send_action_review(owner, action_id, action)

            self.bot.logger.log(MODULE_NAME, "Daily integrity report sent to owner")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to generate daily report", e)

    # ==================== BACKGROUND TASKS ====================

    @tasks.loop(minutes=1)
    async def check_expired_mutes(self):
        try:
            for mute in self.get_expired_mutes():
                guild = self.bot.get_guild(mute['guild_id'])
                if not guild:
                    continue
                member = guild.get_member(mute['user_id'])
                if not member:
                    self.remove_mute(mute['guild_id'], mute['user_id'])
                    continue
                muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
                if muted_role and muted_role in member.roles:
                    try:
                        await member.remove_roles(muted_role, reason="Mute duration expired")
                        self.bot.logger.log(MODULE_NAME, f"Auto-unmuted {member}")
                    except Exception as e:
                        self.bot.logger.error(MODULE_NAME, f"Failed to auto-unmute {member}", e)
                self.remove_mute(mute['guild_id'], mute['user_id'])
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Error in mute expiry checker", e)

    @check_expired_mutes.before_loop
    async def before_check_expired_mutes(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def cleanup_invites(self):
        """Clean up unused ban reversal invites older than configured days."""
        try:
            cleanup_days = CONFIG["oversight"]["invite_cleanup_days"]
            cutoff = datetime.utcnow() - timedelta(days=cleanup_days)
            to_delete = []
            for key, data in self.invites.items():
                created = datetime.fromisoformat(data['created_at'])
                if created < cutoff:
                    try:
                        guild = self.bot.get_guild(data['guild_id'])
                        if guild:
                            invites = await guild.invites()
                            for inv in invites:
                                if inv.code == data['code']:
                                    await inv.delete(reason="Unused ban reversal invite cleanup")
                                    break
                    except:
                        pass
                    to_delete.append(key)
            for key in to_delete:
                del self.invites[key]
            if to_delete:
                self._save_json(self.invites_file, self.invites)
                self.bot.logger.log(MODULE_NAME, f"Cleaned up {len(to_delete)} old ban reversal invites")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to cleanup old invites", e)

    @cleanup_invites.before_loop
    async def before_cleanup_invites(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def send_daily_report(self):
        """Send daily report at configured CST time."""
        try:
            cst = pytz.timezone('America/Chicago')
            now = datetime.now(cst)
            target = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            await self.generate_daily_report()
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to send daily report", e)

    @send_daily_report.before_loop
    async def before_send_daily_report(self):
        await self.bot.wait_until_ready()


# ==================== UNIFIED COMMAND LOGIC ====================

async def _do_ban(ctx: ModContext, mod_system: ModerationSystem,
                  user: discord.User, reason: str, delete_days: int = 0, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if user == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if user == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    member = ctx.guild.get_member(user.id)
    if member and member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)

    delete_days = max(0, min(7, delete_days))
    try:
        # Send Appeal DM
        try:
            dm = discord.Embed(title="🔨 You have been banned",
                               description=f"You have been banned from **{ctx.guild.name}**",
                               color=0x992d22, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.add_field(name="📝 Appeal Process",
                         value="If you believe this ban was unjustified, submit an appeal below.",
                         inline=False)
            dm.set_footer(text="Appeals are reviewed by server staff")
            await user.send(embed=dm, view=BanAppealView(ctx.guild.id))
        except discord.Forbidden:
            pass

        # Perform Action
        if not fake:
            await ctx.guild.ban(user, reason=f"{reason} - By {ctx.author}",
                                delete_message_days=delete_days)

        # In-Chat Response
        embed = discord.Embed(title="✅ User Banned",
                              description=f"{user.mention} has been banned.",
                              color=0x992d22, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        # Logs
        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_ban(ctx.guild, user, ctx.author, reason, delete_days, ctx.channel)

        # Oversight logging
        action_id = await mod_system.log_mod_action({
            'action': 'ban',
            'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': user.id,
            'user': str(user),
            'reason': reason,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'duration': None,
            'additional': {'delete_days': delete_days}
        })
        if inchat_msg_id and action_id:
            mod_system.track_embed(inchat_msg_id, action_id, 'inchat')
        if botlog_msg_id and action_id:
            mod_system.track_embed(botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} banned {user}")

    except discord.Forbidden:
        await ctx.error("❌ I don't have permission to ban this user.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to ban the user.")
        ctx.bot.logger.error(MODULE_NAME, "Ban failed", e)

async def _do_unban(ctx: ModContext, mod_system: ModerationSystem,
                    user_id: str, reason: str = "No reason provided", fake: bool = False):
    if not ctx.author.guild_permissions.ban_members:
        return await ctx.error("❌ You don't have permission to unban members.")
    try:
        user = await ctx.bot.fetch_user(int(user_id))
        if not fake:
            await ctx.guild.unban(user, reason=f"{reason} - By {ctx.author}")
            mod_system.resolve_pending_action(user.id, 'ban')
        embed = discord.Embed(title="✅ User Unbanned",
                              description=f"{user.mention} has been unbanned.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unbanned {user}")
    except ValueError:
        await ctx.error("❌ Invalid user ID.")
    except discord.NotFound:
        await ctx.error("❌ User not found or not banned.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unban.")
        ctx.bot.logger.error(MODULE_NAME, "Unban failed", e)

async def _do_kick(ctx: ModContext, mod_system: ModerationSystem,
                   member: discord.Member, reason: str, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)
    try:
        try:
            dm = discord.Embed(title="👢 You have been kicked",
                               description=f"You have been kicked from **{ctx.guild.name}**",
                               color=0xe67e22, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.set_footer(text="You can rejoin if you have an invite link")
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        if not fake:
            await member.kick(reason=f"{reason} - By {ctx.author}")

        embed = discord.Embed(title="✅ Member Kicked",
                              description=f"{member.mention} has been kicked.",
                              color=0xe67e22, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_kick(ctx.guild, member, ctx.author, reason, ctx.channel)

        action_id = await mod_system.log_mod_action({
            'action': 'kick',
            'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': member.id,
            'user': str(member),
            'reason': reason,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None
        })
        if inchat_msg_id and action_id:
            mod_system.track_embed(inchat_msg_id, action_id, 'inchat')
        if botlog_msg_id and action_id:
            mod_system.track_embed(botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} kicked {member}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to kick the member.")
        ctx.bot.logger.error(MODULE_NAME, "Kick failed", e)

async def _do_timeout(ctx: ModContext, mod_system: ModerationSystem,
                      member: discord.Member, duration: int, reason: str, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if not (1 <= duration <= 40320):
        return await ctx.error("❌ Duration must be between 1 and 40320 minutes.")
    try:
        if not fake:
            await member.timeout(datetime.utcnow() + timedelta(minutes=duration),
                                 reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(title="✅ Member Timed Out",
                              description=f"{member.mention} timed out for **{duration}** minutes.",
                              color=0xe74c3c, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Duration", value=f"{duration} minutes", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_timeout(ctx.guild, member, ctx.author, reason,
                                 f"{duration} minutes", ctx.channel)

        # Timeout is excluded from oversight logging (by log_mod_action returning None)
        # But we still want to track embeds if oversight ignores it? No, we don't track.
        # No action_id returned, so no embed tracking.

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} timed out {member} for {duration}m")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to timeout the member.")
        ctx.bot.logger.error(MODULE_NAME, "Timeout failed", e)

async def _do_untimeout(ctx: ModContext, mod_system: ModerationSystem, member: discord.Member, fake: bool = False):
    if not ctx.author.guild_permissions.moderate_members and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to moderate members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        if not fake:
            await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        embed = discord.Embed(title="✅ Timeout Removed",
                              description=f"{member.mention}'s timeout has been removed.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} removed timeout from {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to remove the timeout.")
        ctx.bot.logger.error(MODULE_NAME, "Untimeout failed", e)

async def _do_mute(ctx: ModContext, mod_system: ModerationSystem,
                   member: discord.Member, reason: str = "No reason provided",
                   duration: Optional[str] = None, fake: bool = False):
    if not ctx.author.guild_permissions.manage_roles and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to mute members.")
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)

    duration_seconds, duration_str = parse_duration(duration or "")
    try:
        muted_role = discord.utils.get(ctx.guild.roles, name=MUTED_ROLE_NAME)
        if not muted_role:
            muted_role = await ctx.guild.create_role(
                name=MUTED_ROLE_NAME, color=discord.Color.dark_gray(),
                reason="Creating Muted role for moderation")
            for ch in ctx.guild.channels:
                try:
                    await ch.set_permissions(muted_role, send_messages=False, speak=False)
                except Exception:
                    pass

        if not fake:
            await member.add_roles(muted_role, reason=reason)
            mod_system.add_mute(ctx.guild.id, member.id, reason, ctx.author, duration_seconds)

        try:
            dm = discord.Embed(title="🔇 You Have Been Muted",
                               description=f"You have been muted in **{ctx.guild.name}**.",
                               color=0xf39c12, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Duration", value=duration_str, inline=True)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="✅ Member Muted",
                              description=f"{member.mention} has been muted.",
                              color=0xf39c12, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_mute(ctx.guild, member, ctx.author, reason, duration_str, ctx.channel)

        # Mute excluded from oversight
        # No action_id, no tracking.

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} muted {member} for {duration_str}")

    except discord.Forbidden:
        await ctx.error("❌ I don't have permission to mute this member.")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to mute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Mute failed", e)

async def _do_unmute(ctx: ModContext, mod_system: ModerationSystem, member: discord.Member, fake: bool = False):
    if not ctx.author.guild_permissions.manage_roles and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to manage roles.")
    muted_role = discord.utils.get(ctx.guild.roles, name=MUTED_ROLE_NAME)
    if not muted_role or muted_role not in member.roles:
        return await ctx.error("❌ This member is not muted.")
    try:
        if not fake:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
            mod_system.remove_mute(ctx.guild.id, member.id)
        embed = discord.Embed(title="✅ Member Unmuted",
                              description=f"{member.mention} has been unmuted.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unmuted {member}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unmute the member.")
        ctx.bot.logger.error(MODULE_NAME, "Unmute failed", e)

async def _do_softban(ctx: ModContext, mod_system: ModerationSystem,
                      member: discord.Member, reason: str, delete_days: int = 7, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.error(ERROR_HIGHER_ROLE)

    delete_days = max(0, min(7, delete_days))
    try:
        if not fake:
            await member.ban(reason=f"Softban: {reason} - By {ctx.author}",
                             delete_message_days=delete_days)
            await ctx.guild.unban(member, reason=f"Softban unban - By {ctx.author}")

        embed = discord.Embed(
            title="✅ Member Softbanned",
            description=f"{member.mention} softbanned (messages deleted, can rejoin).",
            color=0x992d22, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_softban(ctx.guild, member, ctx.author, reason, delete_days, ctx.channel)

        action_id = await mod_system.log_mod_action({
            'action': 'softban',
            'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': member.id,
            'user': str(member),
            'reason': reason,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'delete_days': delete_days}
        })
        if action_id:
            if inchat_msg_id:
                mod_system.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                mod_system.track_embed(botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} softbanned {member}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to softban the member.")
        ctx.bot.logger.error(MODULE_NAME, "Softban failed", e)

async def _do_warn(ctx: ModContext, mod_system: ModerationSystem,
                   member: discord.Member, reason: str, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    if member == ctx.author:
        return await ctx.error(ERROR_CANNOT_ACTION_SELF)
    if member == ctx.bot.user:
        return await ctx.error(ERROR_CANNOT_ACTION_BOT)
    try:
        strike_count = mod_system.get_strikes(member.id) + 1 if fake else mod_system.add_strike(member.id, reason)
        try:
            dm = discord.Embed(title="⚠️ Warning",
                               description=f"You have been warned in **{ctx.guild.name}**",
                               color=0xf39c12, timestamp=datetime.utcnow())
            dm.add_field(name="Reason", value=reason, inline=False)
            dm.add_field(name="Moderator", value=str(ctx.author), inline=True)
            dm.add_field(name="Total Warnings", value=str(strike_count), inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="⚠️ Member Warned",
                              description=f"{member.mention} has been warned.",
                              color=0xf39c12, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Total Warnings", value=str(strike_count), inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_warn(ctx.guild, member, ctx.author, reason, strike_count, ctx.channel)

        # Warn excluded from oversight

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} warned {member}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to warn the member.")
        ctx.bot.logger.error(MODULE_NAME, "Warn failed", e)

async def _do_warnings(ctx: ModContext, mod_system: ModerationSystem, member: discord.Member):
    if not ctx.author.guild_permissions.manage_messages and not has_elevated_role(ctx.author):
        return await ctx.error("❌ You don't have permission to view warnings.")
    strikes = mod_system.get_strike_details(member.id)
    if not strikes:
        return await ctx.reply(f"✅ **{member}** has no warnings.", ephemeral=True)
    embed = discord.Embed(title=f"⚠️ Warnings for {member}",
                          description=f"Total warnings: **{len(strikes)}**",
                          color=0xf39c12, timestamp=datetime.utcnow())
    for i, s in enumerate(strikes[-10:], 1):
        ts = datetime.fromisoformat(s['timestamp']).strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(name=f"Warning {i}",
                        value=f"**Reason:** {s['reason']}\n**Date:** {ts}", inline=False)
    if len(strikes) > 10:
        embed.set_footer(text=f"Showing last 10 of {len(strikes)} warnings")
    await ctx.reply(embed=embed, ephemeral=True)

async def _do_clearwarnings(ctx: ModContext, mod_system: ModerationSystem, member: discord.Member):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.error("❌ You need Administrator permission to clear warnings.")
    if mod_system.clear_strikes(member.id):
        embed = discord.Embed(title="✅ Warnings Cleared",
                              description=f"All warnings cleared for **{member}**.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} cleared warnings for {member}")
    else:
        await ctx.reply(f"**{member}** has no warnings to clear.", ephemeral=True)

async def _do_purge(ctx: ModContext, mod_system: ModerationSystem,
                    amount: int, target: Optional[discord.Member] = None, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    if not (1 <= amount <= 100):
        return await ctx.error("❌ Amount must be between 1 and 100.")

    if not isinstance(ctx._source, discord.Interaction):
        try:
            await ctx._source.message.delete()
        except Exception:
            pass

    await ctx.defer()
    try:
        check = (lambda m: m.author.id == target.id) if target else (lambda m: True)
        deleted = [] if fake else await ctx.channel.purge(limit=amount, check=check)
        if fake:
            deleted = [None] * amount  # simulate deleted count for embed

        reason = f"Purged {len(deleted)} message(s)" + (f" from {target}" if target else "")
        embed = discord.Embed(
            title="✅ Messages Purged",
            description=f"Deleted **{len(deleted)}** messages"
                        f"{f' from {target.mention}' if target else ''}.",
            color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
        inchat_msg_id = await ctx.followup(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_purge(ctx.guild, ctx.author, len(deleted), ctx.channel, target)

        action_id = await mod_system.log_mod_action({
            'action': 'purge',
            'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': target.id if target else None,
            'user': str(target) if target else None,
            'reason': reason,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'amount': len(deleted)}
        })
        if action_id:
            if inchat_msg_id:
                mod_system.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                mod_system.track_embed(botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} purged {len(deleted)} messages")

    except Exception as e:
        await ctx.followup("❌ An error occurred while trying to purge messages.", ephemeral=True)
        ctx.bot.logger.error(MODULE_NAME, "Purge failed", e)

async def _do_slowmode(ctx: ModContext, mod_system: ModerationSystem,
                       seconds: int, channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("❌ You don't have permission to manage channels.")
    if not (0 <= seconds <= 21600):
        return await ctx.error("❌ Slowmode must be between 0 and 21600 seconds.")
    target = channel or ctx.channel
    try:
        await target.edit(slowmode_delay=seconds, reason=f"Slowmode set by {ctx.author}")
        if seconds == 0:
            embed = discord.Embed(title="✅ Slowmode Disabled",
                                  description=f"Slowmode disabled in {target.mention}.",
                                  color=0x2ecc71)
        else:
            embed = discord.Embed(title="✅ Slowmode Enabled",
                                  description=f"Slowmode set to **{seconds}s** in {target.mention}.",
                                  color=0x3498db)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} set slowmode to {seconds}s in {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to set slowmode.")
        ctx.bot.logger.error(MODULE_NAME, "Slowmode failed", e)

async def _do_lock(ctx: ModContext, mod_system: ModerationSystem,
                   reason: str, channel: Optional[discord.TextChannel] = None, fake: bool = False):
    if not has_elevated_role(ctx.author):
        return await ctx.error(ERROR_NO_PERMISSION)
    ok, err = validate_reason(reason)
    if not ok:
        return await ctx.error(err)
    target = channel or ctx.channel
    try:
        if not fake:
            await target.set_permissions(ctx.guild.default_role, send_messages=False,
                                         reason=f"{reason} - By {ctx.author}")
        embed = discord.Embed(title="🔒 Channel Locked",
                              description=f"{target.mention} has been locked.",
                              color=0xe74c3c, timestamp=datetime.utcnow())
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        inchat_msg_id = await ctx.reply(embed=embed)

        el = get_event_logger(ctx.bot)
        botlog_msg_id = None
        if el:
            botlog_msg_id = await el.log_lock(ctx.guild, ctx.author, reason, target)

        action_id = await mod_system.log_mod_action({
            'action': 'lock',
            'moderator_id': ctx.author.id,
            'moderator': str(ctx.author),
            'user_id': None,
            'user': None,
            'reason': reason,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'message_id': ctx.message.id if ctx.message else None,
            'additional': {'channel': target.id}
        })
        if action_id:
            if inchat_msg_id:
                mod_system.track_embed(inchat_msg_id, action_id, 'inchat')
            if botlog_msg_id:
                mod_system.track_embed(botlog_msg_id, action_id, 'botlog')

        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} locked {target.name}")

    except Exception as e:
        await ctx.error("❌ An error occurred while trying to lock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Lock failed", e)

async def _do_unlock(ctx: ModContext, mod_system: ModerationSystem,
                     channel: Optional[discord.TextChannel] = None):
    if not ctx.author.guild_permissions.manage_channels:
        return await ctx.error("❌ You don't have permission to manage channels.")
    target = channel or ctx.channel
    try:
        await target.set_permissions(ctx.guild.default_role, send_messages=None,
                                     reason=f"Unlocked by {ctx.author}")
        embed = discord.Embed(title="🔓 Channel Unlocked",
                              description=f"{target.mention} has been unlocked.",
                              color=0x2ecc71, timestamp=datetime.utcnow())
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        await ctx.reply(embed=embed)
        ctx.bot.logger.log(MODULE_NAME, f"{ctx.author} unlocked {target.name}")
    except Exception as e:
        await ctx.error("❌ An error occurred while trying to unlock the channel.")
        ctx.bot.logger.error(MODULE_NAME, "Unlock failed", e)


# ==================== SETUP ====================

def setup(bot):
    # Create unified moderation system
    mod_system = ModerationSystem(bot)
    bot._mod_system = mod_system  # expose for MediaScanner scanner sharing

    # Attach to bot for backward compatibility
    bot.moderation_manager = mod_system
    bot.mod_oversight = mod_system
    bot.moderation = mod_system  # new unified attribute

    # ---- SLASH COMMANDS ----
    @bot.tree.command(name="ban", description="Ban a user from the server")
    @app_commands.describe(user="User to ban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 0)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_ban(interaction: discord.Interaction, user: discord.User,
                        reason: str, delete_days: Optional[int] = 0, fake: bool = False):
        await _do_ban(ModContext(interaction), mod_system, user, reason, delete_days, fake=fake)

    @bot.tree.command(name="unban", description="Unban a user from the server")
    @app_commands.describe(user_id="User ID to unban", reason="Reason for unban",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_unban(interaction: discord.Interaction, user_id: str,
                          reason: Optional[str] = "No reason provided", fake: bool = False):
        await _do_unban(ModContext(interaction), mod_system, user_id, reason, fake=fake)

    @bot.tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(kick_members=True)
    async def slash_kick(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_kick(ModContext(interaction), mod_system, member, reason, fake=fake)

    @bot.tree.command(name="timeout", description="Timeout a member")
    @app_commands.describe(member="Member to timeout", duration="Duration in minutes",
                           reason="Reason (min 10 chars)", fake="Simulate without executing")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_timeout(interaction: discord.Interaction, member: discord.Member,
                            duration: int, reason: str, fake: bool = False):
        await _do_timeout(ModContext(interaction), mod_system, member, duration, reason, fake=fake)

    @bot.tree.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="Member to remove timeout from",
                           fake="Simulate without executing")
    @app_commands.default_permissions(moderate_members=True)
    async def slash_untimeout(interaction: discord.Interaction, member: discord.Member,
                               fake: bool = False):
        await _do_untimeout(ModContext(interaction), mod_system, member, fake=fake)

    @bot.tree.command(name="mute", description="Mute a member")
    @app_commands.describe(member="Member to mute", reason="Reason for mute",
                           duration="Duration e.g. 10m, 1h, 1d (empty = permanent)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_mute(interaction: discord.Interaction, member: discord.Member,
                         reason: str = "No reason provided", duration: Optional[str] = None,
                         fake: bool = False):
        await _do_mute(ModContext(interaction), mod_system, member, reason, duration, fake=fake)

    @bot.tree.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Member to unmute", fake="Simulate without executing")
    @app_commands.default_permissions(manage_roles=True)
    async def slash_unmute(interaction: discord.Interaction, member: discord.Member,
                            fake: bool = False):
        await _do_unmute(ModContext(interaction), mod_system, member, fake=fake)

    @bot.tree.command(name="softban", description="Softban a member (ban+unban to delete messages)")
    @app_commands.describe(member="Member to softban", reason="Reason (min 10 chars)",
                           delete_days="Days of messages to delete (0-7, default 7)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(ban_members=True)
    async def slash_softban(interaction: discord.Interaction, member: discord.Member,
                            reason: str, delete_days: Optional[int] = 7, fake: bool = False):
        await _do_softban(ModContext(interaction), mod_system, member, reason, delete_days, fake=fake)

    @bot.tree.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason (min 10 chars)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warn(interaction: discord.Interaction, member: discord.Member,
                         reason: str, fake: bool = False):
        await _do_warn(ModContext(interaction), mod_system, member, reason, fake=fake)

    @bot.tree.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
        await _do_warnings(ModContext(interaction), mod_system, member)

    @bot.tree.command(name="clearwarnings", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.default_permissions(administrator=True)
    async def slash_clearwarnings(interaction: discord.Interaction, member: discord.Member):
        await _do_clearwarnings(ModContext(interaction), mod_system, member)

    @bot.tree.command(name="purge", description="Delete multiple messages")
    @app_commands.describe(amount="Number of messages to delete (1-100)",
                           user="Only delete messages from this user (optional)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_messages=True)
    async def slash_purge(interaction: discord.Interaction, amount: int,
                          user: Optional[discord.Member] = None, fake: bool = False):
        await _do_purge(ModContext(interaction), mod_system, amount, user, fake=fake)

    @bot.tree.command(name="slowmode", description="Set channel slowmode")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)",
                           channel="Channel to apply to (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_slowmode(interaction: discord.Interaction, seconds: int,
                             channel: Optional[discord.TextChannel] = None):
        await _do_slowmode(ModContext(interaction), mod_system, seconds, channel)

    @bot.tree.command(name="lock", description="Lock a channel")
    @app_commands.describe(reason="Reason for locking (min 10 chars)",
                           channel="Channel to lock (default: current)",
                           fake="Simulate without executing")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_lock(interaction: discord.Interaction, reason: str,
                         channel: Optional[discord.TextChannel] = None, fake: bool = False):
        await _do_lock(ModContext(interaction), mod_system, reason, channel, fake=fake)

    @bot.tree.command(name="unlock", description="Unlock a channel")
    @app_commands.describe(channel="Channel to unlock (default: current)")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_unlock(interaction: discord.Interaction,
                           channel: Optional[discord.TextChannel] = None):
        await _do_unlock(ModContext(interaction), mod_system, channel)

    # ---- PREFIX COMMANDS ----
    @bot.command(name="ban")
    async def prefix_ban(ctx, user: discord.User = None, *, args: str = ""):
        if not user:
            return await ctx.send("❌ Usage: `?ban @user <reason> [delete_days] [fake]`", delete_after=8)
        parts_all = args.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: args = " ".join(parts_all[:-1])
        delete_days = 0
        reason = args
        parts = args.rsplit(None, 1)
        if len(parts) == 2 and parts[-1].isdigit() and int(parts[-1]) <= 7:
            delete_days = int(parts[-1])
            reason = parts[0]
        await _do_ban(ModContext(ctx), mod_system, user, reason, delete_days, fake=fake)

    @bot.command(name="unban")
    async def prefix_unban(ctx, user_id: str = None, *, reason: str = "No reason provided"):
        if not user_id:
            return await ctx.send("❌ Usage: `?unban <user_id> [reason]`", delete_after=8)
        parts_all = reason.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: reason = " ".join(parts_all[:-1]) or "No reason provided"
        await _do_unban(ModContext(ctx), mod_system, user_id, reason, fake=fake)

    @bot.command(name="kick")
    async def prefix_kick(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?kick @member <reason>`", delete_after=8)
        parts_all = reason.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: reason = " ".join(parts_all[:-1])
        await _do_kick(ModContext(ctx), mod_system, member, reason, fake=fake)

    @bot.command(name="timeout")
    async def prefix_timeout(ctx, member: discord.Member = None,
                              duration: int = None, *, reason: str = ""):
        if not member or duration is None:
            return await ctx.send("❌ Usage: `?timeout @member <minutes> <reason>`", delete_after=8)
        parts_all = reason.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: reason = " ".join(parts_all[:-1])
        await _do_timeout(ModContext(ctx), mod_system, member, duration, reason, fake=fake)

    @bot.command(name="untimeout")
    async def prefix_untimeout(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?untimeout @member`", delete_after=8)
        await _do_untimeout(ModContext(ctx), mod_system, member, fake=fake.lower() == "fake")

    @bot.command(name="mute")
    async def prefix_mute(ctx, member: discord.Member = None, *, args: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?mute @member [duration] [reason]`", delete_after=8)
        duration = None
        reason = args or "No reason provided"
        parts = args.split(None, 1)
        if parts and re.match(r'^\d+[smhd]$', parts[0].lower()):
            duration = parts[0]
            reason = parts[1] if len(parts) > 1 else "No reason provided"
        fake = reason.split()[-1].lower() == "fake" if reason.split() else False
        if fake: reason = " ".join(reason.split()[:-1]) or "No reason provided"
        await _do_mute(ModContext(ctx), mod_system, member, reason, duration, fake=fake)

    @bot.command(name="unmute")
    async def prefix_unmute(ctx, member: discord.Member = None, fake: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?unmute @member`", delete_after=8)
        await _do_unmute(ModContext(ctx), mod_system, member, fake=fake.lower() == "fake")

    @bot.command(name="softban")
    async def prefix_softban(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?softban @member <reason>`", delete_after=8)
        parts_all = reason.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: reason = " ".join(parts_all[:-1])
        await _do_softban(ModContext(ctx), mod_system, member, reason, fake=fake)

    @bot.command(name="warn")
    async def prefix_warn(ctx, member: discord.Member = None, *, reason: str = ""):
        if not member:
            return await ctx.send("❌ Usage: `?warn @member <reason>`", delete_after=8)
        parts_all = reason.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: reason = " ".join(parts_all[:-1])
        await _do_warn(ModContext(ctx), mod_system, member, reason, fake=fake)

    @bot.command(name="warnings")
    async def prefix_warnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("❌ Usage: `?warnings @member`", delete_after=8)
        await _do_warnings(ModContext(ctx), mod_system, member)

    @bot.command(name="clearwarnings")
    async def prefix_clearwarnings(ctx, member: discord.Member = None):
        if not member:
            return await ctx.send("❌ Usage: `?clearwarnings @member`", delete_after=8)
        await _do_clearwarnings(ModContext(ctx), mod_system, member)

    @bot.command(name="purge")
    async def prefix_purge(ctx, amount: int = None, member: discord.Member = None, fake: str = ""):
        if amount is None:
            return await ctx.send("❌ Usage: `?purge <amount> [@member]`", delete_after=8)
        await _do_purge(ModContext(ctx), mod_system, amount, member, fake=fake.lower() == "fake")

    @bot.command(name="slowmode")
    async def prefix_slowmode(ctx, seconds: int = None,
                               channel: discord.TextChannel = None):
        if seconds is None:
            return await ctx.send("❌ Usage: `?slowmode <seconds> [#channel]`", delete_after=8)
        await _do_slowmode(ModContext(ctx), mod_system, seconds, channel)

    @bot.command(name="lock")
    async def prefix_lock(ctx, channel: Optional[discord.TextChannel] = None, *, reason: str = ""):
        parts_all = reason.split()
        fake = parts_all[-1].lower() == "fake" if parts_all else False
        if fake: reason = " ".join(parts_all[:-1])
        await _do_lock(ModContext(ctx), mod_system, reason, channel, fake=fake)

    @bot.command(name="unlock")
    async def prefix_unlock(ctx, channel: discord.TextChannel = None):
        await _do_unlock(ModContext(ctx), mod_system, channel)

    # ---- OVERSIGHT COMMANDS ----
    @bot.tree.command(name="report", description="[Owner only] Trigger the moderation report immediately")
    async def report_command(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ This command is restricted to the bot owner.", ephemeral=True)
            return
        await interaction.response.send_message("📊 Generating report...", ephemeral=True)
        try:
            await mod_system.generate_daily_report()
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Manual report generation failed", e)

    # ---- EVENT LISTENERS ----
    @bot.listen()
    async def on_message(message):
        if message.author.bot or not message.guild:
            return

        # Cache for oversight
        await mod_system.cache_message(message)

        content_lower = message.content.lower()

        # Auto-mod: Child Safety
        for word in CHILD_SAFETY:
            if matches_banned_term(word, content_lower):
                try:
                    await message.delete()
                    await message.guild.ban(message.author,
                                            reason=f"Auto-ban: Child safety violation - '{word}'")
                    bot.logger.log(MODULE_NAME, f"AUTO-BAN: {message.author} child safety", "WARNING")
                    el = get_event_logger(bot)
                    if el:
                        await el.log_autoban(message.guild, message.author,
                                             "Child safety violation", message.channel)
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-ban failed", e)
                return

        # Auto-mod: Racial Slurs
        for word in RACIAL_SLURS:
            if matches_banned_term(word, content_lower):
                try:
                    await message.delete()
                    count = mod_system.add_strike(
                        message.author.id, f"Racial slur: '{word}'")
                    if count >= 2:
                        await message.guild.ban(
                            message.author, reason="Auto-ban: Repeated racial slurs (2 strikes)")
                        bot.logger.log(MODULE_NAME,
                                       f"AUTO-BAN: {message.author} repeated slurs", "WARNING")
                        el = get_event_logger(bot)
                        if el:
                            await el.log_autoban_strike(
                                message.guild, message.author, count,
                                "Repeated racial slurs", message.channel)
                    else:
                        try:
                            dm = discord.Embed(
                                title="⚠️ Warning - Strike 1/2",
                                description="Your message was deleted for inappropriate language.",
                                color=0xf39c12)
                            dm.add_field(name="Action",
                                         value="Second strike = automatic ban.", inline=False)
                            await message.author.send(embed=dm)
                        except Exception:
                            pass
                        bot.logger.log(MODULE_NAME, f"STRIKE 1: {message.author} slur", "WARNING")
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Auto-mod slur handling failed", e)
                return

        # Auto-mod: Banned Words
        for word in BANNED_WORDS:
            if matches_banned_term(word, content_lower):
                try:
                    await message.delete()
                    bot.logger.log(MODULE_NAME, f"Deleted banned word from {message.author}")
                except Exception as e:
                    bot.logger.error(MODULE_NAME, "Message deletion failed", e)
                return

    @bot.listen()
    async def on_message_delete(message):
        """Track moderation embed deletions, bot-log deletion attempts, and re-host cached media."""
        await mod_system.handle_embed_deletion(message.id)

        if not message.guild:
            return

        # ── Bot-log deletion protection ──
        bot_logs_channel = mod_system._get_bot_logs_channel(message.guild)
        if bot_logs_channel and message.channel.id == bot_logs_channel.id:
            if message.id in mod_system._bot_log_cache:
                # Determine who deleted it via audit log
                deleter = None
                try:
                    await asyncio.sleep(0.75)  # give audit log time to populate
                    async for entry in message.guild.audit_logs(
                        limit=10, action=discord.AuditLogAction.message_delete
                    ):
                        age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                        if age < 15 and entry.target.id == message.author.id:
                            deleter = entry.user
                            break
                except Exception as e:
                    mod_system.bot.logger.log(MODULE_NAME, f"Audit log fetch failed: {e}", "WARNING")

                # If the bot deleted it itself (e.g. purge command), ignore
                if deleter and deleter.id == message.guild.me.id:
                    return

                # Act if elevated user deleted it, OR if we couldn't identify the deleter
                # (fail-safe: unknown = treat as suspicious)
                if deleter is None or has_elevated_role(deleter):
                    await mod_system.handle_bot_log_deletion(
                        message.id, deleter or message.guild.me, message.guild
                    )
                elif message.id in mod_system._deletion_warnings:
                    # This was a deletion-warning message — resend regardless of who deleted it
                    original_log_id = mod_system._deletion_warnings.pop(message.id)
                    # Rebuild and resend the warning perpetually
                    warning_embed = discord.Embed(
                        title="🚨 Bot-Log Deletion Warning — REPOSTED",
                        description=(
                            f"A deletion warning for log `{original_log_id}` was itself deleted.\n"
                            f"This report will continue to reappear every time it is deleted."
                        ),
                        color=0xff0000,
                        timestamp=discord.utils.utcnow(),
                    )
                    warning_embed.add_field(name="Original Log ID", value=original_log_id, inline=True)
                    warning_embed.set_footer(text="Deleting this message will cause it to repost again.")
                    try:
                        new_warn_msg = await bot_logs_channel.send(embed=warning_embed)
                        mod_system._register_bot_log(
                            new_warn_msg.id, f"WARN-{original_log_id}", warning_embed,
                            is_warning=True, warning_for_log_id=original_log_id
                        )
                        mod_system._deletion_warnings[new_warn_msg.id] = original_log_id
                    except Exception as e:
                        mod_system.bot.logger.error(MODULE_NAME, f"Failed to repost deletion warning: {e}")
            return  # Don't process bot-log channel messages further below

        # ── Cached media re-hosting for regular channel messages ──
        if not message.author.bot and message.id in mod_system.media_cache:
            guild_id = str(message.guild.id)
            channel_id = str(message.channel.id)
            cached = mod_system.media_cache.get(message.id)
            mod_system.bot.logger.log(MODULE_NAME,
                f"[REHOST] Deletion detected for msg {message.id} — "
                f"found in media_cache with {len(cached['files']) if cached else 0} file(s)")
            rehosted = []
            if cached:
                for f in cached['files']:
                    mod_system.bot.logger.log(MODULE_NAME,
                        f"[REHOST] Decrypting '{f['filename']}' from {f['path']} for msg {message.id}")
                    try:
                        data = mod_system._decrypt_from_disk(f['path'])
                        mod_system.bot.logger.log(MODULE_NAME,
                            f"[REHOST] Decrypted {len(data)} bytes for '{f['filename']}' (msg {message.id})")
                        rehosted.append({'filename': f['filename'], 'data': data})
                    except Exception as e:
                        mod_system.bot.logger.log(MODULE_NAME,
                            f"[REHOST] FAILED to decrypt '{f['filename']}' for msg {message.id}: {e}", "WARNING")
            mod_system.bot.logger.log(MODULE_NAME,
                f"[REHOST] {len(rehosted)} file(s) successfully decrypted for msg {message.id}; "
                f"passing to logger")
            mod_system._delete_media_files(message.id)
            channel_msgs = mod_system.message_cache.get(guild_id, {}).get(channel_id, [])
            mod_system.message_cache[guild_id][channel_id] = [
                m for m in channel_msgs if m['id'] != message.id
            ]
            # Mark this message as handled so logger.py's on_message_delete skips it
            if not hasattr(bot, '_deletion_log_handled'):
                bot._deletion_log_handled = set()
            bot._deletion_log_handled.add(message.id)
            # Directly invoke the logger with the re-hosted files — avoids the race
            # condition where logger's listener fires before this one populates
            # _pending_rehosted_media, causing the log to use the original (expiring) URL.
            event_logger = get_event_logger(bot)
            if event_logger:
                mod_system.bot.logger.log(MODULE_NAME,
                    f"[REHOST] Calling event_logger.log_message_delete for msg {message.id} "
                    f"with rehosted_files={'YES (' + str(len(rehosted)) + ' files)' if rehosted else 'None (fallback to original URL)'}")
                await event_logger.log_message_delete(message, rehosted_files=rehosted if rehosted else None)
            else:
                mod_system.bot.logger.log(MODULE_NAME,
                    f"[REHOST] WARN: event_logger not found on bot — deletion log for msg {message.id} will NOT be sent", "WARNING")
        elif not message.author.bot and message.attachments:
            mod_system.bot.logger.log(MODULE_NAME,
                f"[REHOST] msg {message.id} was deleted with {len(message.attachments)} attachment(s) "
                f"but was NOT in media_cache — original URL will be used as fallback")

    @bot.listen()
    async def on_message_edit(before, after):
        """Detect when a user removes attachments from a message after sending."""
        if not after.guild or after.author.bot:
            return

        before_att_ids = {att.id for att in before.attachments}
        after_att_ids = {att.id for att in after.attachments}
        removed_ids = before_att_ids - after_att_ids

        if not removed_ids:
            return

        # Find which attachments were removed (by id match from cached data)
        guild_id = str(after.guild.id)
        channel_id = str(after.channel.id)

        # Update the message cache to reflect new attachment list
        channel_msgs = mod_system.message_cache.get(guild_id, {}).get(channel_id, [])
        for msg in channel_msgs:
            if msg['id'] == after.id:
                msg['attachments'] = [att.url for att in after.attachments]
                break

        # Collect removed files from media_cache and decrypt them for re-hosting
        cached = mod_system.media_cache.get(after.id)
        removed_files = []
        if cached:
            removed_filenames = {
                att.filename for att in before.attachments if att.id in removed_ids
            }
            kept = []
            for f in cached['files']:
                if f['filename'] in removed_filenames:
                    try:
                        data = mod_system._decrypt_from_disk(f['path'])
                        removed_files.append({'filename': f['filename'], 'data': data})
                    except Exception as e:
                        mod_system.bot.logger.log(MODULE_NAME, f"Failed to decrypt removed attachment {f['filename']}: {e}", "WARNING")
                    finally:
                        f['path'].unlink(missing_ok=True)
                else:
                    kept.append(f)
            # Update cache index to only reflect remaining files
            if kept:
                mod_system.media_cache[after.id]['files'] = kept
            else:
                mod_system.media_cache.pop(after.id, None)

        if not removed_files:
            # No locally cached copies — log what we know without media
            bot_logs = mod_system._get_bot_logs_channel(after.guild)
            if bot_logs:
                embed = discord.Embed(
                    title="✂️ Attachment Removed from Message",
                    color=discord.Color.yellow(),
                    timestamp=__import__('datetime').datetime.utcnow(),
                )
                embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
                description = f"**{after.author.mention} removed an attachment in {after.channel.mention}**"
                if after.content:
                    description += f"\n{after.content}"
                embed.description = description
                removed_names = ", ".join(att.filename for att in before.attachments if att.id in removed_ids)
                embed.add_field(name="Removed File(s)", value=removed_names or "unknown", inline=False)
                embed.add_field(name="Note", value="File was not in local cache; original URLs may be expired.", inline=False)
                embed.set_footer(text=f"Author: {after.author.id} | Message ID: {after.id}")
                await mod_system.send_bot_log(after.guild, embed)
            return

        # We have cached files — build embed matching deletion log style
        image_exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        audio_exts = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus', '.mp4', '.mov', '.webm')

        image_files = [f for f in removed_files if f['filename'].lower().endswith(image_exts)]
        other_files  = [f for f in removed_files if not f['filename'].lower().endswith(image_exts)]

        embed = discord.Embed(
            color=discord.Color.yellow(),
            timestamp=__import__('datetime').datetime.utcnow(),
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        description = f"**{after.author.mention} removed an attachment in {after.channel.mention}**"
        if after.content:
            description += f"\n{after.content}"
        embed.description = description
        embed.set_footer(text=f"Author: {after.author.id} | Message ID: {after.id}")

        bot_logs = mod_system._get_bot_logs_channel(after.guild)

        if image_files and not other_files:
            embed.set_image(url=f"attachment://{image_files[0]['filename']}")
            await mod_system.send_bot_log(after.guild, embed, files_data=removed_files)
        elif other_files:
            has_audio = any(f['filename'].lower().endswith(audio_exts) for f in other_files)
            label = "audio" if has_audio else "file"
            embed.add_field(name="Attachment", value=f"*{label} hosted above*", inline=False)
            # Files must appear above embed — send files first as plain message, then embed via send_bot_log
            if bot_logs:
                discord_files = [discord.File(fp=io.BytesIO(f['data']), filename=f['filename']) for f in removed_files]
                await bot_logs.send(files=discord_files)
            await mod_system.send_bot_log(after.guild, embed)
        else:
            await mod_system.send_bot_log(after.guild, embed, files_data=removed_files)

    @bot.listen()
    async def on_member_remove(member):
        mod_system.save_member_roles(member)

    @bot.listen()
    async def on_member_join(member):
        await mod_system.restore_member_roles(member)

    bot.logger.log(MODULE_NAME, "Moderation setup complete")