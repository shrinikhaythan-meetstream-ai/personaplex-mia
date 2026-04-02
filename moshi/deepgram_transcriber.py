import os
import asyncio
import time
from typing import Optional
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results
from .moshi.context_manager import ContextManager
from .moshi.utils.logging import ColorizedLog

clog = ColorizedLog.randomize()

class DeepgramTranscriber:
    """
    Manages a SINGLE persistent Deepgram stream per connection.
    PCM chunks are sent to this stream without creating new streams.
    Results are auto-stored in ContextManager via async event handler.
    """
    def __init__(self, context_manager: ContextManager, 
                 model: str = "nova-3",
                 language: str = "en-US",
                 punctuate: bool = True,
                 smart_format: bool = True,
                 interim_results: bool = True,
                 diarize: bool = True):
        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise EnvironmentError("DEEPGRAM_API_KEY environment variable not set.")
        self.context_manager = context_manager
        
        # Uses DEEPGRAM_API_KEY from environment variables automatically
        self.client = DeepgramClient()
        
        self.model = model
        self.language = language
        self.punctuate = punctuate
        self.smart_format = smart_format
        self.interim_results = interim_results
        self.diarize = diarize
        self.connection = None
        self.enabled = True
        self.last_audio_time = 0
        self.keepalive_task = None
        self.receive_task = None
        self._audio_queue = asyncio.Queue()
        clog.log("info", "[Deepgram] DeepgramTranscriber initialized (single persistent stream mode)")

    async def start(self) -> None:
        """
        Start a SINGLE persistent Deepgram stream for this connection.
        PCM chunks are sent to it via send_audio().
        Results processed automatically in background.
        """
        if not self.enabled:
            clog.log("warning", "[Deepgram] Deepgram disabled, skipping stream start")
            return
        try:
            clog.log("info", "[Deepgram] Starting persistent stream...")
            self.connection = self.client.listen.v1.connect(
                model=self.model,
                language=self.language,
                punctuate=self.punctuate,
                smart_format=self.smart_format,
                interim_results=self.interim_results,
                diarize=self.diarize,
                encoding="linear16",   # Required for raw PCM
                sample_rate=16000,     # Required for raw PCM
                channels=1             # Required for raw PCM
            )
            
            # Safe lambda signatures with **kwargs to prevent crashes
            self.connection.on(EventType.OPEN, lambda _, **kwargs: clog.log("info", "[Deepgram] Connection opened"))
            self.connection.on(EventType.CLOSE, lambda _, **kwargs: clog.log("info", "[Deepgram] Connection closed"))
            self.connection.on(EventType.ERROR, lambda _, err, **kwargs: clog.log("error", f"[Deepgram ERROR] {err}"))
            self.connection.on(EventType.MESSAGE, self._on_message)
            
            self.connection.start_listening()
            self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            self.receive_task = asyncio.create_task(self._audio_send_loop())
            clog.log("info", "[Deepgram] Persistent stream created and listening with keepalive")
        except Exception as e:
            clog.log("error", f"[Deepgram] Failed to start stream: {e}")
            self.enabled = False

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """
        Send PCM audio chunk to persistent stream.
        This is called for every PCM frame. Uses same stream (not new one).
        Args:
            pcm_bytes: int16 PCM audio bytes, 16kHz sample rate
        """
        if not self.enabled or not self.connection:
            return
        self.last_audio_time = time.time()
        await self._audio_queue.put(pcm_bytes)

    async def _audio_send_loop(self) -> None:
        """
        Background loop that sends audio from queue to Deepgram stream.
        """
        while self.connection and self.enabled:
            pcm_bytes = await self._audio_queue.get()
            try:
                self.connection.send_media(pcm_bytes)
            except Exception as e:
                clog.log("error", f"[Deepgram] Error sending audio: {e}")

    def _on_message(self, self_obj, message, **kwargs):
        """
        Correctly typed callback that processes results and cleanly separates speakers.
        """
        if isinstance(message, ListenV1Results):
            words = message.channel.alternatives[0].words if message.channel else []
            is_final = getattr(message, 'is_final', False)
            
            if not words:
                return

            current_speaker = getattr(words[0], 'speaker', 0)
            current_phrase = []

            for w in words:
                spk = getattr(w, 'speaker', 0)
                if spk == current_speaker:
                    current_phrase.append(w.word)
                else:
                    self._flush_transcript(current_speaker, current_phrase, is_final)
                    current_speaker = spk
                    current_phrase = [w.word]
            
            if current_phrase:
                self._flush_transcript(current_speaker, current_phrase, is_final)

    def _flush_transcript(self, speaker_id, word_list, is_final):
        """Helper to cleanly push transcripts to the context manager."""
        transcript_text = " ".join(word_list)
        speaker_label = f"Speaker {speaker_id}"
        log_type = "FINAL" if is_final else "PARTIAL"
        
        clog.log("info", f"[Deepgram] {log_type} transcript: {speaker_label}: {transcript_text}")
        
        self.context_manager.update_transcript({
            "speaker": speaker_label,
            "text": transcript_text,
            "is_final": is_final
        })

    async def _keepalive_loop(self) -> None:
        """
        Send keepalive every 5 seconds to keep Deepgram stream alive.
        """
        while self.connection and self.enabled:
            await asyncio.sleep(5)
            try:
                self.connection.send_keep_alive()
            except Exception as e:
                clog.log("info", f"[Deepgram] Keepalive failed (stream may have closed): {e}")
                break

    async def stop(self) -> None:
        """
        Close persistent stream (called on connection close).
        """
        self.enabled = False
        if self.keepalive_task:
            self.keepalive_task.cancel()
            try:
                await self.keepalive_task
            except asyncio.CancelledError:
                pass
        if self.receive_task:
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
        if self.connection:
            try:
                self.connection.finish()
                self.connection = None
                clog.log("info", "[Deepgram] Stream stopped")
            except Exception as e:
                clog.log("error", f"[Deepgram] Error stopping stream: {e}")