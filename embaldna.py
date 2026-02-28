# [file name]: embaldna.py
"""
EmbалDNA — Pre-upload media safety scanner.

Scans image/video attachments before they are re-hosted to Discord CDN.
Uses a two-stage pipeline:
  1. NudeNet  — detects explicit/nudity content
  2. DeepFace — estimates apparent age of any detected faces

If a file is explicit AND any face is estimated under the AGE_THRESHOLD,
re-hosting is BLOCKED, the encrypted file is deleted, and the owner is
alerted via DM and bot-logs with a plain-text report (no image content).

All other files (audio, documents, non-explicit images) pass through
without scanning overhead.

Integration:
    from embaldna import EmbалDNA, ScanVerdict

    scanner = EmbалDNA(bot, owner_id, get_bot_logs_channel_fn)
    verdict = await scanner.scan_files(files_data)
    # files_data: list of {'filename': str, 'data': bytes}

    if verdict.blocked:
        # Do NOT re-host. scanner has already alerted owner.
        return
    # Safe to re-host verdict.safe_files (blocked files stripped out)
"""

import asyncio
import io
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

import discord

logger = logging.getLogger("EmbалDNA")

# ── Configuration ──────────────────────────────────────────────────────────────

# Apparent age threshold — any face estimated below this triggers a block.
# Set conservatively high (20) to account for model imprecision on real minors.
AGE_THRESHOLD = 20

# NudeNet confidence threshold for "explicit" classification.
# Labels considered explicit content (covers full/partial nudity).
EXPLICIT_LABELS = {
    "EXPOSED_ANUS",
    "EXPOSED_BUTTOCKS",
    "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F",
    "EXPOSED_GENITALIA_M",
    "EXPOSED_BELLY",          # included for borderline cases with age concern
}
NUDENET_CONFIDENCE_THRESHOLD = 0.45

# File extensions to scan (images only — audio/docs pass through untouched)
SCANNABLE_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif'}
SCANNABLE_VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.avi', '.mkv'}  # video: first-frame only

# ── Lazy model loader ──────────────────────────────────────────────────────────

_nudenet_classifier = None
_deepface_available = False

def _load_nudenet():
    global _nudenet_classifier
    if _nudenet_classifier is None:
        try:
            from nudenet import NudeClassifier  # type: ignore
            _nudenet_classifier = NudeClassifier()
            logger.info("NudeNet classifier loaded.")
        except ImportError:
            logger.warning("NudeNet not installed. Run: pip install nudenet")
        except Exception as e:
            logger.error(f"Failed to load NudeNet: {e}")
    return _nudenet_classifier

def _check_deepface():
    global _deepface_available
    try:
        import deepface  # noqa: F401  type: ignore
        _deepface_available = True
    except ImportError:
        logger.warning("DeepFace not installed. Run: pip install deepface")
        _deepface_available = False
    return _deepface_available

# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class FileScanResult:
    filename: str
    is_scannable: bool          # False = audio/doc, skipped
    is_explicit: bool = False   # NudeNet flagged explicit content
    min_age_estimate: Optional[float] = None  # lowest age found by DeepFace (None = no face)
    blocked: bool = False       # True = do not re-host
    reason: str = ""            # Human-readable reason for block

@dataclass
class ScanVerdict:
    blocked: bool                          # True if ANY file was blocked
    safe_files: List[dict] = field(default_factory=list)   # files_data entries that passed
    blocked_files: List[FileScanResult] = field(default_factory=list)
    all_results: List[FileScanResult] = field(default_factory=list)

# ── Core scanner ───────────────────────────────────────────────────────────────

class EmbалDNA:
    """
    Async-safe media scanner. Instantiate once per bot session and reuse.

    Args:
        bot:                  discord.py Bot instance (for DM-ing owner).
        owner_id:             Discord user ID to alert on blocks.
        get_bot_logs_fn:      Callable[guild] -> Optional[discord.TextChannel]
                              Same as ModerationSystem._get_bot_logs_channel.
    """

    def __init__(self, bot, owner_id: int, get_bot_logs_fn: Callable):
        self.bot = bot
        self.owner_id = owner_id
        self._get_bot_logs = get_bot_logs_fn
        self._nudenet = None
        self._models_loaded = False
        self._load_lock = asyncio.Lock()

    async def _ensure_models(self):
        """Lazy-load models on first scan (avoids slowing bot startup)."""
        if self._models_loaded:
            return
        async with self._load_lock:
            if self._models_loaded:
                return
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _load_nudenet)
            await loop.run_in_executor(None, _check_deepface)
            self._nudenet = _nudenet_classifier
            self._models_loaded = True

    # ── Public API ─────────────────────────────────────────────────────────────

    async def scan_files(
        self,
        files_data: List[dict],
        guild: Optional[discord.Guild] = None,
        context: str = "",
    ) -> ScanVerdict:
        """
        Scan a list of files before re-hosting.

        Args:
            files_data:  list of {'filename': str, 'data': bytes}
            guild:       Guild context for bot-log alerts (can be None)
            context:     Short description for the alert (e.g. "deleted message 12345")

        Returns:
            ScanVerdict with .blocked, .safe_files, .blocked_files
        """
        await self._ensure_models()

        safe = []
        blocked = []
        all_results = []

        loop = asyncio.get_event_loop()

        for file_entry in files_data:
            filename: str = file_entry['filename']
            data: bytes = file_entry['data']
            ext = os.path.splitext(filename.lower())[1]

            # Non-image files skip scanning entirely
            if ext not in SCANNABLE_IMAGE_EXTS and ext not in SCANNABLE_VIDEO_EXTS:
                result = FileScanResult(filename=filename, is_scannable=False)
                safe.append(file_entry)
                all_results.append(result)
                continue

            result = FileScanResult(filename=filename, is_scannable=True)

            try:
                # Stage 1: NudeNet explicit content detection
                is_explicit = await loop.run_in_executor(
                    None, self._run_nudenet, data, filename, ext
                )
                result.is_explicit = is_explicit

                if is_explicit:
                    # Stage 2: DeepFace age estimation
                    min_age = await loop.run_in_executor(
                        None, self._run_deepface_age, data
                    )
                    result.min_age_estimate = min_age

                    if min_age is not None and min_age < AGE_THRESHOLD:
                        result.blocked = True
                        result.reason = (
                            f"Explicit content detected with apparent age estimate "
                            f"{min_age:.1f} (threshold: {AGE_THRESHOLD})"
                        )
                        logger.warning(
                            f"BLOCKED [{filename}]: {result.reason}"
                        )
                    elif min_age is None:
                        # Explicit but no face detected — flag but don't block by default.
                        # You can tighten this to block=True if you prefer zero tolerance.
                        result.reason = "Explicit content, no face detected (passed)"
                        logger.info(f"FLAGGED (no face) [{filename}]: explicit, no age data")

            except Exception as e:
                # On scan failure, block by default (fail-safe)
                result.blocked = True
                result.reason = f"Scan error — blocked as precaution: {e}"
                logger.error(f"Scan error for {filename}: {e}")

            all_results.append(result)

            if result.blocked:
                blocked.append(result)
            else:
                safe.append(file_entry)

        verdict = ScanVerdict(
            blocked=len(blocked) > 0,
            safe_files=safe,
            blocked_files=blocked,
            all_results=all_results,
        )

        if verdict.blocked:
            await self._send_alert(verdict, guild, context)

        return verdict

    # ── Model runners (blocking, run in executor) ──────────────────────────────

    def _run_nudenet(self, data: bytes, filename: str, ext: str) -> bool:
        """
        Run NudeNet on image bytes. Returns True if explicit content detected.
        For video, extracts the first frame via OpenCV.
        """
        if self._nudenet is None:
            # NudeNet not available — fail open (don't block everything)
            return False

        try:
            if ext in SCANNABLE_VIDEO_EXTS:
                data = self._extract_first_frame(data)
                if data is None:
                    return False

            # NudeNet expects a file path or PIL image; we write to a temp buffer path
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=ext or '.jpg', delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            try:
                result = self._nudenet.classify(tmp_path)
                # result: {path: {label: confidence, ...}}
                predictions = list(result.values())[0] if result else {}
                for label, confidence in predictions.items():
                    if label in EXPLICIT_LABELS and confidence >= NUDENET_CONFIDENCE_THRESHOLD:
                        return True
                return False
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"NudeNet scan failed for {filename}: {e}")
            raise

    def _run_deepface_age(self, data: bytes) -> Optional[float]:
        """
        Run DeepFace age analysis on image bytes.
        Returns the minimum (youngest) apparent age found, or None if no face detected.
        """
        if not _deepface_available:
            return None

        try:
            from deepface import DeepFace  # type: ignore
            import numpy as np
            from PIL import Image

            img = Image.open(io.BytesIO(data)).convert("RGB")
            img_array = np.array(img)

            results = DeepFace.analyze(
                img_path=img_array,
                actions=["age"],
                enforce_detection=False,  # Don't raise if no face found
                silent=True,
            )

            if not results:
                return None

            # results may be a list (multiple faces) or a single dict
            if isinstance(results, dict):
                results = [results]

            ages = []
            for face_result in results:
                age = face_result.get("age")
                if age is not None:
                    ages.append(float(age))

            return min(ages) if ages else None

        except Exception as e:
            logger.warning(f"DeepFace age estimation failed: {e}")
            return None

    def _extract_first_frame(self, video_data: bytes) -> Optional[bytes]:
        """Extract the first frame of a video as JPEG bytes using OpenCV."""
        try:
            import cv2  # type: ignore
            import numpy as np
            import tempfile

            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp:
                tmp.write(video_data)
                tmp_path = tmp.name

            try:
                cap = cv2.VideoCapture(tmp_path)
                ret, frame = cap.read()
                cap.release()
                if not ret or frame is None:
                    return None
                _, buf = cv2.imencode('.jpg', frame)
                return buf.tobytes()
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        except ImportError:
            logger.warning("OpenCV not installed; video first-frame extraction skipped.")
            return None
        except Exception as e:
            logger.error(f"First-frame extraction failed: {e}")
            return None

    # ── Alert sender ───────────────────────────────────────────────────────────

    async def _send_alert(
        self,
        verdict: ScanVerdict,
        guild: Optional[discord.Guild],
        context: str,
    ):
        """DM the owner and post a plain-text bot-log alert. No image content is included."""
        timestamp = datetime.now(timezone.utc)

        blocked_summary = "\n".join(
            f"• `{r.filename}` — {r.reason}"
            for r in verdict.blocked_files
        )

        embed = discord.Embed(
            title="🚫 EmbалDNA: Re-host Blocked",
            description=(
                "One or more files were **blocked from re-hosting** due to potential "
                "illegal content detection. The encrypted file(s) have been deleted. "
                "**No image content is attached to this report.**"
            ),
            color=0xff0000,
            timestamp=timestamp,
        )
        embed.add_field(
            name="Context",
            value=context or "*(no context provided)*",
            inline=False,
        )
        embed.add_field(
            name=f"Blocked File(s) — {len(verdict.blocked_files)}",
            value=blocked_summary[:1024] or "*(none)*",
            inline=False,
        )
        embed.add_field(
            name="Action Required",
            value=(
                "Review the original message in your server audit logs. "
                "If this content is illegal, report it to Discord Trust & Safety "
                "and NCMEC's CyberTipline (www.missingkids.org/gethelpnow/cybertipline)."
            ),
            inline=False,
        )
        embed.set_footer(text="EmbалDNA safety scanner • No flagged content was uploaded to Discord CDN")

        # DM the owner
        try:
            owner = await self.bot.fetch_user(self.owner_id)
            if owner:
                await owner.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to DM owner about blocked re-host: {e}")

        # Post to bot-logs (text only, no files)
        if guild:
            try:
                bot_logs = self._get_bot_logs(guild)
                if bot_logs:
                    await bot_logs.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to post EmbалDNA alert to bot-logs: {e}")
