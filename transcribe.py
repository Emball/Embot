import discord
from discord import app_commands
import asyncio
import os
import tempfile
from pathlib import Path
import subprocess
import json
import time
import threading

MODULE_NAME = "TRANSCRIBE"

# Configuration - Reduced models to prevent memory issues
PROGRESSIVE_MODELS = ["base", "small"]
MAX_CONCURRENT_TRANSCRIPTIONS = 2
TEMP_DIR = Path("data/transcribe_temp")
TEMP_FILE_MAX_AGE = 3600

# Global model loading lock
MODEL_LOAD_LOCK = asyncio.Lock()


class TranscriptionManager:
    """Manages automatic transcription of voice messages"""
    
    def __init__(self, bot):
        self.bot = bot
        self._setup_directories()
        self.whisper_models = {}
        self.active_transcriptions = 0
        self.transcription_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSCRIPTIONS)
        self.model_loading = {}
        
        # Metrics tracking
        self.total_transcriptions = 0
        self.total_transcription_time = 0.0
        self.model_usage_count = {}
        
        self._check_dependencies()
        self._cleanup_old_temp_files()
    
    def _setup_directories(self):
        """Create temporary directory for audio processing"""
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.bot.logger.log(MODULE_NAME, f"Temp directory ready: {TEMP_DIR}")
    
    def _cleanup_old_temp_files(self):
        """Clean up old temporary files on startup"""
        try:
            current_time = time.time()
            cleaned = 0
            
            for file in TEMP_DIR.glob("vm_*"):
                if file.is_file():
                    file_age = current_time - file.stat().st_mtime
                    if file_age > TEMP_FILE_MAX_AGE:
                        file.unlink()
                        cleaned += 1
            
            if cleaned > 0:
                self.bot.logger.log(MODULE_NAME, f"Cleaned up {cleaned} old temp file(s)")
        except Exception as e:
            self.bot.logger.log(MODULE_NAME, f"Error cleaning temp files: {e}", "WARNING")
    
    def _check_dependencies(self):
        """Check if required dependencies are available"""
        try:
            subprocess.run(['ffmpeg', '-version'], 
                         capture_output=True, 
                         check=True, 
                         timeout=5)
            self.bot.logger.log(MODULE_NAME, "ffmpeg detected")
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            self.bot.logger.log(MODULE_NAME, 
                "ffmpeg not found - transcription will not work", "WARNING")
        
        try:
            import whisper
            self.bot.logger.log(MODULE_NAME, "Whisper available")
        except ImportError:
            self.bot.logger.log(MODULE_NAME, 
                "Whisper not installed - run: pip install openai-whisper", "ERROR")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to load Whisper", e)

    async def load_model(self, model_name):
        """Load a Whisper model with proper async handling and locking"""
        if model_name in self.whisper_models:
            return self.whisper_models[model_name]
        
        if model_name in self.model_loading:
            self.bot.logger.log(MODULE_NAME, f"Waiting for {model_name} model to finish loading...")
            while model_name in self.model_loading:
                await asyncio.sleep(0.1)
            return self.whisper_models.get(model_name)
        
        async with MODEL_LOAD_LOCK:
            if model_name in self.whisper_models:
                return self.whisper_models[model_name]
            
            self.model_loading[model_name] = True
            
            try:
                import whisper
                import torch
                
                loop = asyncio.get_event_loop()
                
                def _load_model():
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    self.bot.logger.log(MODULE_NAME, f"Loading Whisper model: {model_name} on {device.upper()}")
                    
                    model = whisper.load_model(model_name, device=device)
                    
                    if device == "cuda":
                        self.bot.logger.log(MODULE_NAME, f"GPU acceleration enabled: {torch.cuda.get_device_name(0)}")
                    else:
                        self.bot.logger.log(MODULE_NAME, "Running on CPU", "WARNING")
                    
                    return model
                
                model = await loop.run_in_executor(None, _load_model)
                self.whisper_models[model_name] = model
                
                return model
                
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, f"Failed to load model {model_name}", e)
                if model_name == "large":
                    self.bot.logger.log(MODULE_NAME, "Falling back to medium model")
                    return await self.load_model("medium")
                elif model_name == "medium":
                    self.bot.logger.log(MODULE_NAME, "Falling back to small model")
                    return await self.load_model("small")
                return None
            finally:
                self.model_loading.pop(model_name, None)
    
    def is_voice_message(self, message):
        """Check if a message is a voice message"""
        if not message.attachments:
            return False
        
        for attachment in message.attachments:
            if hasattr(attachment, 'is_voice_message') and attachment.is_voice_message():
                return True
            if attachment.content_type and 'audio' in attachment.content_type:
                if attachment.waveform is not None:
                    return True
        
        return False
    
    async def convert_to_wav(self, input_path, output_path):
        """Convert audio file to WAV format for Whisper"""
        try:
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-i', str(input_path),
                '-ar', '16000',
                '-ac', '1',
                '-c:a', 'pcm_s16le',
                '-y',
                str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                self.bot.logger.error(MODULE_NAME, 
                    f"ffmpeg conversion failed: {stderr.decode()}")
                return False
            
            return True
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Audio conversion error", e)
            return False
    
    async def transcribe_audio(self, audio_path, model_name):
        """Transcribe audio file using Whisper with error handling"""
        model = await self.load_model(model_name)
        if not model:
            return None
        
        try:
            start_time = time.time()
            
            loop = asyncio.get_event_loop()
            
            def _transcribe():
                try:
                    return model.transcribe(str(audio_path))
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, f"Transcription failed for {model_name}", e)
                    return None
            
            result = await loop.run_in_executor(None, _transcribe)
            
            if not result:
                return None
                
            elapsed = time.time() - start_time
            
            text = result.get('text', '').strip()
            
            if not text:
                return None
            
            language = result.get('language', 'unknown')
            
            self.model_usage_count[model_name] = self.model_usage_count.get(model_name, 0) + 1
            
            self.bot.logger.log(MODULE_NAME, 
                f"Transcribed with {model_name} in {elapsed:.2f}s | Language: {language} | Length: {len(text)} chars")
            
            return {
                'text': text,
                'language': language,
                'model': model_name,
                'elapsed_time': elapsed
            }
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Transcription error", e)
            return None
    
    async def download_and_convert(self, message):
        """Download voice message and convert to WAV"""
        try:
            attachment = message.attachments[0]
            
            original_ext = Path(attachment.filename).suffix or '.ogg'
            temp_original = TEMP_DIR / f"vm_{message.id}{original_ext}"
            temp_wav = TEMP_DIR / f"vm_{message.id}.wav"
            
            await attachment.save(temp_original)
            
            conversion_success = await self.convert_to_wav(temp_original, temp_wav)
            
            # Delete original ONLY if conversion succeeded
            try:
                if temp_original.exists():
                    temp_original.unlink()
            except Exception as e:
                self.bot.logger.log(MODULE_NAME, f"Could not delete original: {e}", "WARNING")
            
            if not conversion_success:
                if temp_wav.exists():
                    temp_wav.unlink()
                return None
            
            return temp_wav
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Download/convert error", e)
            return None
        
    async def use_cached_vm(self, vm_cache_path):
        """Use a cached VM file from VMS system instead of downloading"""
        try:
            # Check if file exists and is accessible
            if not vm_cache_path.exists():
                self.bot.logger.log(MODULE_NAME, f"Cached VM not found: {vm_cache_path}", "WARNING")
                return None
            
            # Check file extension - if already .wav, use directly
            if vm_cache_path.suffix.lower() == '.wav':
                self.bot.logger.log(MODULE_NAME, f"Using cached WAV directly: {vm_cache_path.name}")
                return vm_cache_path
            
            # Convert to WAV
            temp_wav = TEMP_DIR / f"vm_cached_{vm_cache_path.stem}.wav"
            
            conversion_success = await self.convert_to_wav(vm_cache_path, temp_wav)
            
            if not conversion_success:
                if temp_wav.exists():
                    temp_wav.unlink()
                return None
            
            self.bot.logger.log(MODULE_NAME, f"Converted cached VM to WAV: {vm_cache_path.name}")
            return temp_wav
            
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, f"Error using cached VM: {vm_cache_path}", e)
            return None
    
    def format_transcription(self, result):
        """Format transcription as plain quoted text"""
        text = result['text']
        return f"> {text}"
    
    def _get_audio_duration(self, audio_path):
        """Get audio duration in seconds using ffprobe"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 
                 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', 
                 str(audio_path)],
                capture_output=True,
                text=True,
                timeout=5
            )
            return float(result.stdout.strip())
        except:
            return 0.0
    
    async def progressive_transcription(self, message, skip_bot_vms=True):
        """Progressively transcribe with base -> small, editing the message each time"""
        # Skip bot's own voice messages if requested
        if skip_bot_vms and message.author.id == self.bot.user.id:
            self.bot.logger.log(MODULE_NAME, "Skipping bot's own VM")
            return
        
        async with self.transcription_semaphore:
            self.active_transcriptions += 1
            
            try:
                self.bot.logger.log(MODULE_NAME, 
                    f"Starting progressive transcription ({self.active_transcriptions}/{MAX_CONCURRENT_TRANSCRIPTIONS} active)")
                
                temp_files = []
                reply_message = None
                
                try:
                    channel_name = getattr(message.channel, 'name', 'DM')
                    async with message.channel.typing():
                        self.bot.logger.log(MODULE_NAME, 
                            f"Processing VM from {message.author.display_name} in #{channel_name}")
                        
                        wav_path = await self.download_and_convert(message)
                        if not wav_path:
                            self.bot.logger.log(MODULE_NAME, "Failed to prepare audio", "WARNING")
                            return
                        
                        temp_files.append(wav_path)
                        
                        try:
                            duration = self._get_audio_duration(wav_path)
                            self.bot.logger.log(MODULE_NAME, f"Audio duration: {duration:.1f}s")
                        except:
                            duration = None
                        
                        for i, model_name in enumerate(PROGRESSIVE_MODELS):
                            self.bot.logger.log(MODULE_NAME, 
                                f"[{i+1}/{len(PROGRESSIVE_MODELS)}] Transcribing with {model_name} model...")
                            
                            # Check if file still exists before transcribing
                            if not wav_path.exists():
                                self.bot.logger.log(MODULE_NAME, 
                                    f"WAV file missing before {model_name} transcription", "WARNING")
                                break
                            
                            result = await self.transcribe_audio(wav_path, model_name)
                            
                            if not result:
                                self.bot.logger.log(MODULE_NAME, 
                                    f"{model_name} transcription failed", "WARNING")
                                continue
                            
                            formatted_text = self.format_transcription(result)
                            
                            if reply_message is None:
                                reply_message = await message.reply(formatted_text, mention_author=False)
                                self.bot.logger.log(MODULE_NAME, 
                                    f"✓ Posted {model_name} transcription")
                            else:
                                try:
                                    await reply_message.edit(content=formatted_text)
                                    self.bot.logger.log(MODULE_NAME, 
                                        f"✓ Updated to {model_name} transcription")
                                except discord.NotFound:
                                    self.bot.logger.log(MODULE_NAME, 
                                        f"Reply message was deleted, cannot update", "WARNING")
                                    break
                                except Exception as e:
                                    self.bot.logger.error(MODULE_NAME, 
                                        f"Failed to update message to {model_name}", e)
                    
                    self.total_transcriptions += 1
                    self.bot.logger.log(MODULE_NAME, 
                        f"✓ Complete for {message.author.display_name}")
                
                except Exception as e:
                    self.bot.logger.error(MODULE_NAME, 
                        f"Error in progressive transcription", e)
                
                finally:
                    # Only delete temp files AFTER all models have processed
                    for temp_file in temp_files:
                        try:
                            if temp_file.exists():
                                temp_file.unlink()
                        except Exception as e:
                            self.bot.logger.log(MODULE_NAME, 
                                f"Failed to delete temp file: {temp_file}", "WARNING")
                
            finally:
                self.active_transcriptions -= 1
                self.bot.logger.log(MODULE_NAME, 
                    f"Transcription slot freed ({self.active_transcriptions}/{MAX_CONCURRENT_TRANSCRIPTIONS} active)")


def setup(bot):
    """Setup function called by main bot to initialize this module"""

    # Prevent duplicate listener registration
    listener_name = f"_{MODULE_NAME.lower()}_listener_registered"
    if hasattr(bot, listener_name):
        bot.logger.log(MODULE_NAME, "Module already setup, skipping duplicate registration")
        return
    setattr(bot, listener_name, True)

    bot.logger.log(MODULE_NAME, "Setting up transcribe module")
    
    transcribe_manager = TranscriptionManager(bot)
    bot.transcribe_manager = transcribe_manager
    
    @bot.listen('on_message')
    async def on_voice_message_transcribe(message):
        """Listen for voice messages and progressively transcribe them"""
        if message.author.bot:
            return
        
        # Skip DMs - they don't have channel.name
        if not message.guild:
            return
        
        if not transcribe_manager.is_voice_message(message):
            return
        
        channel_name = getattr(message.channel, 'name', 'DM')
        bot.logger.log(MODULE_NAME, 
            f"Detected VM from {message.author} in #{channel_name}")
        
        # Start progressive transcription in background (skip bot's own VMs)
        asyncio.create_task(transcribe_manager.progressive_transcription(message, skip_bot_vms=True))
    
    @bot.tree.context_menu(name="Transcribe Voice Message")
    async def transcribe_context(interaction: discord.Interaction, message: discord.Message):
        """Context menu command to transcribe any voice message"""
        try:
            if not transcribe_manager.is_voice_message(message):
                await interaction.response.send_message(
                    "❌ That message is not a voice message",
                    ephemeral=True
                )
                return
            
            await interaction.response.defer(ephemeral=True)
            
            bot.logger.log(MODULE_NAME, 
                f"Context menu transcription requested by {interaction.user} for message from {message.author}")
            
            temp_files = []
            
            try:
                wav_path = await transcribe_manager.download_and_convert(message)
                if not wav_path:
                    await interaction.followup.send(
                        "❌ Failed to process voice message",
                        ephemeral=True
                    )
                    return
                
                temp_files.append(wav_path)
                
                result = await transcribe_manager.transcribe_audio(wav_path, "base")
                
                if result:
                    formatted_text = transcribe_manager.format_transcription(result)
                    await interaction.followup.send(formatted_text, ephemeral=True)
                else:
                    await interaction.followup.send(
                        "❌ Failed to transcribe the voice message",
                        ephemeral=True
                    )
                
            finally:
                for temp_file in temp_files:
                    try:
                        if temp_file.exists():
                            temp_file.unlink()
                    except:
                        pass
            
        except Exception as e:
            bot.logger.error(MODULE_NAME, "Context menu transcribe failed", e)
            try:
                await interaction.followup.send(
                    "❌ An error occurred while transcribing",
                    ephemeral=True
                )
            except:
                pass
    
    bot.logger.log(MODULE_NAME, "Transcribe module setup complete")