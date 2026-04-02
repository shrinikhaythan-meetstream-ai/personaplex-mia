import os
import asyncio
import time
from deepgram import (
    DeepgramClient,
    LiveTranscriptionEvents,
    LiveOptions,
)
from .moshi.context_manager import ContextManager
from .moshi.utils.logging import ColorizedLog

clog = ColorizedLog.randomize()

class DeepgramTranscriber:
    """
    Manages a SINGLE persistent Deepgram stream using SDK v3.4.0.
    """
    def __init__(self, context_manager: ContextManager, 
                 model: str = "nova-3",
                 language: str = "en-US"):
        self.context_manager = context_manager
        
        # v3 handles the API key from environment variables automatically
        self.client = DeepgramClient()
        
        self.model = model
        self.language = language
        self.connection = None
        self.enabled = True
        self._audio_queue = asyncio.Queue()
        self.receive_task = None
        self.keepalive_task = None
        clog.log("info", "[Deepgram] Initialized (Verified SDK v3 Mode)")

    async def start(self) -> None:
        """Opens the v3 WebSocket connection to Deepgram."""
        if not self.enabled:
            return
        try:
            clog.log("info", "[Deepgram] Starting persistent stream...")
            
            # v3 SDK Initialization syntax
            self.connection = self.client.listen.websocket.v("1")

            # Setup event handlers with standard v3 signatures
            self.connection.on(LiveTranscriptionEvents.Open, lambda _, __, **kwargs: clog.log("info", "[Deepgram] Connection opened"))
            self.connection.on(LiveTranscriptionEvents.Transcript, self._on_message)
            self.connection.on(LiveTranscriptionEvents.Metadata, lambda _, metadata, **kwargs: clog.log("info", f"[Deepgram] Metadata received: {metadata}"))
            self.connection.on(LiveTranscriptionEvents.Close, lambda _, __, **kwargs: clog.log("info", "[Deepgram] Connection closed"))
            self.connection.on(LiveTranscriptionEvents.Error, lambda _, err, **kwargs: clog.log("error", f"[Deepgram ERROR] {err}"))

            options = LiveOptions(
                model=self.model,
                language=self.language,
                smart_format=True,
                interim_results=True,
                diarize=True,
                encoding="linear16",
                sample_rate=16000,
                channels=1
            )
            
            # Start the connection
            if self.connection.start(options):
                clog.log("info", "[Deepgram] Persistent stream created and listening")
                self.receive_task = asyncio.create_task(self._audio_send_loop())
                self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            else:
                clog.log("error", "[Deepgram] Failed to start connection handshake")
                self.enabled = False

        except Exception as e:
            clog.log("error", f"[Deepgram] Start failure: {e}")
            self.enabled = False

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Queues raw mic data for the background task."""
        if not self.enabled or not self.connection:
            return
        await self._audio_queue.put(pcm_bytes)

    async def _audio_send_loop(self) -> None:
        """Pushes queued audio to the v3 WebSocket."""
        while self.connection and self.enabled:
            pcm_bytes = await self._audio_queue.get()
            try:
                # v3 uses .send() for raw bytes
                self.connection.send(pcm_bytes) 
            except Exception as e:
                clog.log("error", f"[Deepgram] Error sending audio: {e}")

    def _on_message(self, _, result, **kwargs):
        """Processes v3 LiveResultResponse and separates speakers."""
        try:
            # v3 passes a result object where result.channel.alternatives contains the data
            if not result or not hasattr(result, "channel"):
                return
            
            alternatives = result.channel.alternatives
            if not alternatives:
                return
                
            words = alternatives[0].words
            is_final = getattr(result, "is_final", False)
            
            if not words:
                return

            # Grouping logic to handle multiple speakers in one message
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
                
        except Exception as e:
            clog.log("error", f"[Deepgram] Failed to parse message: {e}")

    def _flush_transcript(self, speaker_id, word_list, is_final):
        """Pushes transcripts to the PersonaPlex context manager."""
        transcript_text = " ".join(word_list)
        speaker_label = f"Speaker {speaker_id}"
        log_type = "FINAL" if is_final else "PARTIAL"
        
        # Only log FINALs to the terminal to keep it clean, but send everything to context
        if is_final:
            clog.log("info", f"[Deepgram] {log_type} transcript: {speaker_label}: {transcript_text}")
        
        self.context_manager.update_transcript({
            "speaker": speaker_label,
            "text": transcript_text,
            "is_final": is_final
        })

    async def _keepalive_loop(self) -> None:
        """Sends keepalive ping every 5 seconds for v3 SDK."""
        while self.connection and self.enabled:
            await asyncio.sleep(5)
            try:
                self.connection.keep_alive() 
            except Exception:
                break

    async def stop(self) -> None:
        """Cleanly closes the v3 connection."""
        self.enabled = False
        if self.keepalive_task:
            self.keepalive_task.cancel()
        if self.receive_task:
            self.receive_task.cancel()
        if self.connection:
            try:
                self.connection.finish()
                self.connection = None
                clog.log("info", "[Deepgram] Stream stopped")
            except Exception as e:
                clog.log("error", f"[Deepgram] Error stopping stream: {e}")                 