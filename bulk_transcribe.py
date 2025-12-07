# bulk_transcribe.py (FIXED WINDOWS COMPATIBILITY)
import asyncio
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import whisper
import torch
import sys
import time
import logging
from collections import deque
import numpy as np
import warnings

# Suppress warnings
warnings.filterwarnings("ignore")

# Configuration
VMS_ROOT = Path("data/voice_messages")
CACHE_DIR = VMS_ROOT / "cache"
ARCHIVE_DIR = VMS_ROOT / "archived"
TRANSCRIPTS_FILE = VMS_ROOT / "transcripts.json"
TEMP_DIR = Path("data/bulk_transcribe_temp")
WHISPER_MODEL = "tiny"  # Use tiny for speed
MAX_CONCURRENT = 2  # Reduced for CPU
MIN_DURATION_SECONDS = 0.4
BATCH_SIZE = 50  # Save progress every N files

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bulk_transcribe.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class BulkTranscriber:
    def __init__(self):
        self.transcripts = {}
        self.model = None
        self.processing_active = False
        
        # Performance tracking
        self.stats = {
            'processed': 0,
            'failed': 0,
            'skipped_short': 0,
            'empty_audio': 0,
            'start_time': None
        }
        
        self._setup_directories()
        self._load_transcripts()
    
    def _setup_directories(self):
        """Create temporary directory for audio processing"""
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Temp directory ready: {TEMP_DIR}")
    
    def _load_transcripts(self):
        """Load existing transcripts from JSON file"""
        try:
            if TRANSCRIPTS_FILE.exists():
                with open(TRANSCRIPTS_FILE, 'r', encoding='utf-8') as f:
                    self.transcripts = json.load(f)
                logger.info(f"Loaded {len(self.transcripts)} existing transcripts")
            else:
                self.transcripts = {}
                logger.info("No existing transcripts found, starting fresh")
        except Exception as e:
            logger.error(f"Error loading transcripts: {e}")
            self.transcripts = {}
    
    def _save_transcripts(self):
        """Save transcripts to JSON file"""
        try:
            # Save to temporary file first, then rename (atomic)
            temp_file = TRANSCRIPTS_FILE.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.transcripts, f, indent=2, ensure_ascii=False)
            
            # Replace original file
            temp_file.replace(TRANSCRIPTS_FILE)
            logger.debug(f"Saved {len(self.transcripts)} transcripts")
        except Exception as e:
            logger.error(f"Error saving transcripts: {e}")
    
    def _get_file_creation_time(self, file_path):
        """Get the creation time of a file"""
        try:
            stat = file_path.stat()
            if hasattr(stat, 'st_birthtime'):
                return datetime.fromtimestamp(stat.st_birthtime, timezone.utc)
            else:
                return datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        except Exception as e:
            logger.error(f"Error getting file time for {file_path}: {e}")
            return datetime.now(timezone.utc)
    
    def get_vm_files(self, directory):
        """Get all voice message files"""
        files = []
        for file in directory.iterdir():
            if file.is_file() and file.suffix.lower() in ['.ogg', '.mp3', '.m4a', '.wav']:
                creation_time = self._get_file_creation_time(file)
                files.append((file, creation_time))
        return files
    
    def get_untranscribed_vms(self):
        """Get all untranscribed VMs"""
        all_files = []
        
        cache_files = self.get_vm_files(CACHE_DIR)
        all_files.extend(cache_files)
        
        archive_files = self.get_vm_files(ARCHIVE_DIR)
        all_files.extend(archive_files)
        
        # Filter untranscribed
        untranscribed = []
        for file_path, creation_time in all_files:
            file_key = str(file_path)
            if file_key not in self.transcripts:
                untranscribed.append((file_path, creation_time))
        
        logger.info(f"Found {len(untranscribed)} untranscribed VMs")
        return untranscribed
    
    def _run_ffmpeg_sync(self, input_path, output_path):
        """Run ffmpeg synchronously (avoids Windows async issues)"""
        try:
            cmd = [
                'ffmpeg', '-i', str(input_path),
                '-ar', '16000',
                '-ac', '1',
                '-c:a', 'pcm_s16le',
                '-y', str(output_path),
                '-hide_banner',
                '-loglevel', 'error'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                error_msg = result.stderr[:200] if result.stderr else "Unknown error"
                logger.warning(f"ffmpeg failed for {input_path.name}: {error_msg}")
                return False
            
            # Check if output file exists and has content
            if not output_path.exists() or output_path.stat().st_size == 0:
                logger.warning(f"Empty output for {input_path.name}")
                return False
            
            return True
            
        except subprocess.TimeoutExpired:
            logger.warning(f"ffmpeg timeout for {input_path.name}")
            return False
        except Exception as e:
            logger.error(f"ffmpeg error for {input_path.name}: {e}")
            return False
    
    def _transcribe_sync(self, audio_path, vm_path):
        """Transcribe audio synchronously"""
        if not self.model:
            logger.error("Model not loaded")
            return None
        
        try:
            start_time = time.time()
            
            # Use minimal settings
            result = self.model.transcribe(
                str(audio_path),
                fp16=False,
                verbose=None,
                task="transcribe",
                temperature=0.0,
                best_of=1,
                beam_size=1
            )
            
            elapsed = time.time() - start_time
            text = result.get('text', '').strip()
            
            if not text or len(text) < 2:
                return None
            
            language = result.get('language', 'unknown')
            
            logger.info(f"Transcribed {vm_path.name} in {elapsed:.1f}s - {len(text)} chars")
            
            return {
                'text': text,
                'language': language,
                'elapsed_time': elapsed
            }
            
        except Exception as e:
            logger.error(f"Transcription failed for {vm_path.name}: {str(e)[:100]}")
            return None
    
    def process_vm(self, vm_path, creation_time):
        """Process a single VM synchronously"""
        try:
            vm_key = str(vm_path)
            
            # Skip if already transcribed
            if vm_key in self.transcripts:
                return True
            
            logger.debug(f"Processing: {vm_path.name}")
            
            # Create temp WAV file
            temp_wav = TEMP_DIR / f"trans_{vm_path.stem}.wav"
            
            # Convert to WAV
            if not self._run_ffmpeg_sync(vm_path, temp_wav):
                # Mark as conversion failed
                self.transcripts[vm_key] = {
                    'text': '',
                    'language': 'conversion_failed',
                    'failed': True,
                    'processed_at': datetime.now(timezone.utc).isoformat()
                }
                self.stats['failed'] += 1
                return True
            
            # Transcribe
            result = self._transcribe_sync(temp_wav, vm_path)
            
            # Clean up temp file
            try:
                if temp_wav.exists():
                    temp_wav.unlink()
            except:
                pass
            
            if result:
                # Save transcript
                self.transcripts[vm_key] = {
                    'text': result['text'],
                    'language': result.get('language', 'unknown'),
                    'processed_at': datetime.now(timezone.utc).isoformat(),
                    'elapsed_time': result['elapsed_time']
                }
                self.stats['processed'] += 1
                return True
            else:
                # Mark as transcription failed
                self.transcripts[vm_key] = {
                    'text': '',
                    'language': 'transcription_failed',
                    'failed': True,
                    'processed_at': datetime.now(timezone.utc).isoformat()
                }
                self.stats['failed'] += 1
                return True
                
        except Exception as e:
            logger.error(f"Error processing {vm_path.name}: {e}")
            return False
    
    def load_model(self):
        """Load Whisper model"""
        logger.info(f"Loading Whisper model: {WHISPER_MODEL}")
        
        try:
            self.model = whisper.load_model(WHISPER_MODEL, device="cpu")
            logger.info("Model loaded successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            return False
    
    def run_sync(self):
        """Run transcription synchronously (no async)"""
        logger.info("=" * 60)
        logger.info("BULK TRANSCRIPTION STARTING (SYNC MODE)")
        logger.info(f"Model: {WHISPER_MODEL}")
        logger.info("=" * 60)
        
        # Load model
        if not self.load_model():
            logger.error("Failed to load model. Exiting.")
            return
        
        # Get untranscribed VMs
        untranscribed = self.get_untranscribed_vms()
        if not untranscribed:
            logger.info("No untranscribed VMs found. Exiting.")
            return
        
        total_vms = len(untranscribed)
        logger.info(f"Processing {total_vms} VMs")
        
        self.stats['start_time'] = time.time()
        last_save_time = self.stats['start_time']
        last_log_time = self.stats['start_time']
        
        try:
            for i, (vm_path, creation_time) in enumerate(untranscribed):
                # Process VM
                self.process_vm(vm_path, creation_time)
                
                # Save progress periodically
                current_time = time.time()
                if (i + 1) % BATCH_SIZE == 0 or current_time - last_save_time > 60:
                    self._save_transcripts()
                    last_save_time = current_time
                
                # Log progress periodically
                if current_time - last_log_time > 30 or (i + 1) % 100 == 0:
                    elapsed = current_time - self.stats['start_time']
                    processed_total = self.stats['processed'] + self.stats['failed']
                    
                    if elapsed > 0 and processed_total > 0:
                        vps = processed_total / elapsed
                        remaining = total_vms - (i + 1)
                        eta = remaining / vps if vps > 0 else 0
                        
                        logger.info(
                            f"Progress: {i + 1}/{total_vms} ({((i + 1)/total_vms*100):.1f}%) | "
                            f"Transcribed: {self.stats['processed']} | "
                            f"Failed: {self.stats['failed']} | "
                            f"Speed: {vps:.2f} VMs/s | "
                            f"ETA: {eta/60:.0f} min"
                        )
                    
                    last_log_time = current_time
        
        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            # Save final transcripts
            self._save_transcripts()
            
            # Print summary
            total_time = time.time() - self.stats['start_time']
            
            logger.info("=" * 60)
            logger.info("TRANSCRIPTION COMPLETE")
            logger.info("=" * 60)
            logger.info(f"Total VMs: {total_vms}")
            logger.info(f"Successfully transcribed: {self.stats['processed']}")
            logger.info(f"Failed: {self.stats['failed']}")
            logger.info(f"Total time: {total_time/60:.1f} min")
            logger.info(f"Total transcripts: {len(self.transcripts)}")
            
            if total_time > 0 and self.stats['processed'] > 0:
                avg_time = total_time / self.stats['processed']
                logger.info(f"Average time per VM: {avg_time:.1f}s")
            
            logger.info("=" * 60)
    
    def _cleanup_temp(self):
        """Clean up temporary files"""
        try:
            if TEMP_DIR.exists():
                for file in TEMP_DIR.glob("*"):
                    if file.is_file():
                        file.unlink()
                logger.info("Cleaned up temp directory")
        except Exception as e:
            logger.error(f"Error cleaning up temp directory: {e}")


def main():
    """Main entry point - synchronous version"""
    transcriber = BulkTranscriber()
    
    try:
        transcriber.run_sync()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        transcriber._cleanup_temp()
        logger.info("Done.")
        
        if sys.platform == "win32":
            input("\nPress Enter to exit...")


if __name__ == "__main__":
    # Use synchronous version to avoid Windows async issues
    main()