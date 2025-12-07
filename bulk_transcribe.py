# bulk_transcribe.py
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

# Configuration
VMS_ROOT = Path("data/voice_messages")
CACHE_DIR = VMS_ROOT / "cache"
ARCHIVE_DIR = VMS_ROOT / "archived"
TRANSCRIPTS_FILE = VMS_ROOT / "transcripts.json"
TEMP_DIR = Path("data/bulk_transcribe_temp")
WHISPER_MODEL = "base"
MAX_CONCURRENT = 4  # Number of parallel transcriptions

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
        self.new_vms_queue = deque()  # New VMs added during processing
        self.processing_active = False
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        
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
            with open(TRANSCRIPTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.transcripts, f, indent=2, ensure_ascii=False)
            logger.debug("Saved transcripts to file")
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
        """Get all voice message files from a directory with their creation times"""
        files = []
        for file in directory.iterdir():
            if file.is_file() and file.suffix.lower() in ['.ogg', '.mp3', '.m4a', '.wav']:
                creation_time = self._get_file_creation_time(file)
                files.append((file, creation_time))
        return files
    
    def get_untranscribed_vms(self):
        """Get all untranscribed VMs sorted by newest first"""
        all_files = []
        
        # Get cache files
        cache_files = self.get_vm_files(CACHE_DIR)
        all_files.extend(cache_files)
        
        # Get archive files
        archive_files = self.get_vm_files(ARCHIVE_DIR)
        all_files.extend(archive_files)
        
        # Filter untranscribed
        untranscribed = []
        for file_path, creation_time in all_files:
            file_key = str(file_path)
            if file_key not in self.transcripts:
                untranscribed.append((file_path, creation_time))
        
        # Sort by creation time (newest first)
        untranscribed.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"Found {len(untranscribed)} untranscribed VMs")
        return untranscribed
    
    def add_new_vm(self, vm_path):
        """Add a new VM to the front of the queue"""
        logger.info(f"Adding new VM to queue: {vm_path.name}")
        creation_time = self._get_file_creation_time(vm_path)
        self.new_vms_queue.appendleft((vm_path, creation_time))
    
    async def convert_to_wav(self, input_path, output_path):
        """Convert audio file to WAV format for Whisper (optimized)"""
        try:
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-i', str(input_path),
                '-ar', '16000',  # 16kHz sample rate
                '-ac', '1',      # Mono
                '-c:a', 'pcm_s16le',
                '-y',            # Overwrite output
                str(output_path),
                '-hide_banner',  # Cleaner output
                '-loglevel', 'error',  # Only show errors
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"ffmpeg conversion failed: {stderr.decode()}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return False
    
    async def transcribe_audio(self, audio_path, vm_path):
        """Transcribe audio file using Whisper"""
        if not self.model:
            return None
        
        try:
            start_time = time.time()
            
            def _transcribe():
                try:
                    return self.model.transcribe(
                        str(audio_path),
                        fp16=torch.cuda.is_available(),  # Use GPU if available
                        verbose=None  # Disable progress output for speed
                    )
                except Exception as e:
                    logger.error(f"Transcription failed: {e}")
                    return None
            
            result = await asyncio.get_event_loop().run_in_executor(None, _transcribe)
            
            if not result:
                return None
                
            elapsed = time.time() - start_time
            text = result.get('text', '').strip()
            
            if not text:
                return None
            
            language = result.get('language', 'unknown')
            
            logger.info(f"Transcribed {vm_path.name} in {elapsed:.2f}s | Language: {language} | {len(text)} chars")
            
            return {
                'text': text,
                'language': language,
                'model': WHISPER_MODEL,
                'elapsed_time': elapsed,
                'transcribed_at': datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            logger.error(f"Transcription error for {vm_path.name}: {e}")
            return None
    
    async def process_vm(self, vm_path, creation_time):
        """Process a single VM (convert and transcribe)"""
        async with self.semaphore:
            try:
                vm_key = str(vm_path)
                
                # Skip if already transcribed (check again in case of race condition)
                if vm_key in self.transcripts:
                    logger.debug(f"Skipping already transcribed: {vm_path.name}")
                    return True
                
                logger.debug(f"Processing: {vm_path.name}")
                
                # Create temp WAV file
                temp_wav = TEMP_DIR / f"trans_{vm_path.stem}.wav"
                
                # Convert to WAV
                conversion_success = await self.convert_to_wav(vm_path, temp_wav)
                if not conversion_success:
                    logger.warning(f"Failed to convert: {vm_path.name}")
                    return False
                
                # Transcribe
                result = await self.transcribe_audio(temp_wav, vm_path)
                
                # Clean up temp file
                if temp_wav.exists():
                    temp_wav.unlink()
                
                if result:
                    # Save transcript
                    self.transcripts[vm_key] = {
                        'text': result['text'],
                        'language': result.get('language', 'unknown'),
                        'keywords': self._extract_keywords(result['text']),
                        'transcribed_at': result['transcribed_at'],
                        'original_file': vm_path.name,
                        'file_size': vm_path.stat().st_size
                    }
                    
                    # Save periodically (every 10 transcriptions)
                    if len(self.transcripts) % 10 == 0:
                        self._save_transcripts()
                    
                    return True
                else:
                    logger.warning(f"Transcription failed for: {vm_path.name}")
                    return False
                    
            except Exception as e:
                logger.error(f"Error processing {vm_path.name}: {e}")
                return False
            finally:
                # Small delay to prevent overwhelming the system
                await asyncio.sleep(0.1)
    
    def _extract_keywords(self, text):
        """Extract keywords from transcript text (simplified)"""
        import re
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for'}
        words = re.findall(r'\b[a-z]{3,}\b', text.lower())
        keywords = [w for w in words if w not in stop_words]
        return keywords
    
    def load_model(self):
        """Load Whisper model with GPU support if available"""
        logger.info("Loading Whisper model...")
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Using device: {device.upper()}")
        
        if device == "cuda":
            logger.info(f"GPU: {torch.cuda.get_device_name(0)}")
            logger.info(f"GPU Memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} GB")
        
        try:
            self.model = whisper.load_model(WHISPER_MODEL, device=device)
            
            # Warm up the model
            logger.info("Warming up model...")
            dummy_audio = whisper.pad_or_trim(torch.randn(16000))
            self.model.transcribe(dummy_audio, fp16=(device == "cuda"), verbose=None)
            logger.info("Model warmed up and ready")
            
            return True
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            return False
    
    async def monitor_new_vms(self):
        """Monitor for new VMs being added while processing"""
        logger.info("Starting new VM monitor...")
        
        # Get initial list of VMs
        initial_vms = set()
        for file_path, _ in self.get_vm_files(CACHE_DIR) + self.get_vm_files(ARCHIVE_DIR):
            initial_vms.add(file_path)
        
        while self.processing_active:
            try:
                # Check for new VMs
                current_vms = set()
                for file_path, _ in self.get_vm_files(CACHE_DIR) + self.get_vm_files(ARCHIVE_DIR):
                    current_vms.add(file_path)
                
                # Find new VMs (not in initial list and not yet transcribed)
                new_vms = current_vms - initial_vms
                for vm_path in new_vms:
                    if str(vm_path) not in self.transcripts:
                        self.add_new_vm(vm_path)
                        initial_vms.add(vm_path)  # Add to initial to avoid duplicate detection
                
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Error in monitor: {e}")
                await asyncio.sleep(1)
    
    async def run(self):
        """Main processing loop"""
        logger.info("=" * 60)
        logger.info("BULK TRANSCRIPTION STARTING")
        logger.info("=" * 60)
        
        # Load model
        if not self.load_model():
            logger.error("Failed to load Whisper model. Exiting.")
            return
        
        # Get untranscribed VMs
        untranscribed = self.get_untranscribed_vms()
        if not untranscribed:
            logger.info("No untranscribed VMs found. Exiting.")
            return
        
        total_vms = len(untranscribed)
        logger.info(f"Processing {total_vms} VMs with {MAX_CONCURRENT} parallel workers")
        
        self.processing_active = True
        
        # Start monitoring for new VMs
        monitor_task = asyncio.create_task(self.monitor_new_vms())
        
        # Process VMs
        processed = 0
        failed = 0
        skipped = 0
        
        # Create a queue for all VMs to process
        process_queue = deque(untranscribed)
        
        start_time = time.time()
        
        try:
            while process_queue or self.new_vms_queue:
                # Prioritize new VMs first
                if self.new_vms_queue:
                    vm_path, creation_time = self.new_vms_queue.popleft()
                    logger.info(f"Processing NEW VM: {vm_path.name}")
                else:
                    vm_path, creation_time = process_queue.popleft()
                
                vm_key = str(vm_path)
                
                # Double-check if already transcribed
                if vm_key in self.transcripts:
                    logger.debug(f"Skipping already transcribed: {vm_path.name}")
                    skipped += 1
                    continue
                
                # Process VM
                success = await self.process_vm(vm_path, creation_time)
                
                if success:
                    processed += 1
                    
                    # Log progress
                    if processed % 10 == 0:
                        elapsed = time.time() - start_time
                        vms_per_second = processed / elapsed if elapsed > 0 else 0
                        remaining = len(process_queue) + len(self.new_vms_queue)
                        eta = remaining / vms_per_second if vms_per_second > 0 else 0
                        
                        logger.info(
                            f"Progress: {processed}/{total_vms} | "
                            f"Speed: {vms_per_second:.2f} VMs/s | "
                            f"Remaining: {remaining} | "
                            f"ETA: {eta:.0f}s"
                        )
                else:
                    failed += 1
                    logger.warning(f"Failed to process: {vm_path.name}")
                
                # Small delay to prevent overwhelming
                await asyncio.sleep(0.05)
        
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            self.processing_active = False
            monitor_task.cancel()
            
            # Save final transcripts
            self._save_transcripts()
            
            # Calculate statistics
            total_time = time.time() - start_time
            avg_time_per_vm = total_time / processed if processed > 0 else 0
            
            logger.info("=" * 60)
            logger.info("BULK TRANSCRIPTION COMPLETE")
            logger.info("=" * 60)
            logger.info(f"Processed: {processed}")
            logger.info(f"Failed: {failed}")
            logger.info(f"Skipped: {skipped}")
            logger.info(f"Total time: {total_time:.2f}s")
            logger.info(f"Average time per VM: {avg_time_per_vm:.2f}s")
            logger.info(f"Total transcripts: {len(self.transcripts)}")
            logger.info("=" * 60)
            
            # Clean up temp directory
            self._cleanup_temp()
    
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


async def main():
    """Main entry point"""
    transcriber = BulkTranscriber()
    
    try:
        await transcriber.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        logger.info("Bulk transcription finished. You can now close this window.")
        
        # Keep console open for viewing results
        if sys.platform == "win32":
            input("\nPress Enter to exit...")


if __name__ == "__main__":
    # Run the bulk transcription
    asyncio.run(main())