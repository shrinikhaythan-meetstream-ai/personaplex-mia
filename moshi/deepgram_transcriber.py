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

clog = ColorizedLog.log_to_console = True # Ensure logging is active
clog = ColorizedLog.randomize()

class DeepgramTranscriber:
    """
    Manages a SINGLE persistent Deepgram stream.
    Uses 'speech_final' and 'UtteranceEnd' to guarantee transcripts are stored.
    """
    def __init__(self, context_manager: ContextManager, 
                 model: str = "nova-3",
                 language: str = "en-US"):
        self.context_manager = context_manager
        self.client = DeepgramClient()
        self.model = model
        self.language = language
        self.connection = None
        self.enabled = True
        self._audio_queue = asyncio.Queue()
        self.receive_task = None
        self.keepalive_task = None
        
        # Internal buffer to store the last 'Partial' transcript
        # Used as a fallback if UtteranceEnd fires without a speech_final
        self.last_partial = {"speaker": "Speaker 0", "text": ""}

    async def start(self) -> None:
        if not self.enabled: return
        try:
            clog.log("info", "[Deepgram] Starting stream with UtteranceEnd & Endpointing...")
            self.connection = self.client.listen.websocket.v("1")

            # Setup Event Handlers
            self.connection.on(LiveTranscriptionEvents.Open, lambda _, __, **kwargs: clog.log("info", "[Deepgram] Connection opened"))
            self.connection.on(LiveTranscriptionEvents.Transcript, self._on_message)
            self.connection.on(LiveTranscriptionEvents.UtteranceEnd, self._on_utterance_end)
            self.connection.on(LiveTranscriptionEvents.Close, lambda _, __, **kwargs: clog.log("info", "[Deepgram] Connection closed"))
            self.connection.on(LiveTranscriptionEvents.Error, lambda _, err, **kwargs: clog.log("error", f"[Deepgram ERROR] {err}"))

            options = LiveOptions(
                model=self.model,
                language=self.language,
                smart_format=True,
                interim_results=True,
                diarize=True,
                # Trigger speech_final after 300ms of silence
                endpointing=300,
                # Trigger UtteranceEnd event after 1000ms gap in words (Fail-safe)
                utterance_end_ms=1000,
                encoding="linear16",
                sample_rate=16000,
                channels=1
            )

            if self.connection.start(options):
                self.receive_task = asyncio.create_task(self._audio_send_loop())
                self.keepalive_task = asyncio.create_task(self._keepalive_loop())
                clog.log("info", "[Deepgram] Stream active: Triggering on speech_final & UtteranceEnd")
        except Exception as e:
            clog.log("error", f"[Deepgram] Start failure: {e}")
            self.enabled = False

    def _on_message(self, _, result, **kwargs):
        """Rule 1: Trigger when speech_final=true is received."""
        if not result or not hasattr(result, 'channel'): return
        
        try:
            alternatives = result.channel.alternatives
            if not alternatives or not alternatives[0].words: return
            
            words = alternatives[0].words
            is_final = getattr(result, 'is_final', False)
            speech_final = getattr(result, 'speech_final', False)
            
            transcript_text = " ".join([w.word for w in words])
            speaker_id = getattr(words[0], 'speaker', 0)
            speaker_label = f"Speaker {speaker_id}"

            # If it's a finished thought (speech_final), force the Context Manager to QUEUE it
            should_store = speech_final or is_final

            self.context_manager.update_transcript({
                "speaker": speaker_label,
                "text": transcript_text,
                "is_final": should_store
            })

            if should_store:
                clog.log("info", f"[Deepgram] STORED (SpeechFinal): {speaker_label}: {transcript_text}")
                self.last_partial = {"speaker": speaker_label, "text": ""} # Clear fallback
            else:
                # Save as fallback in case UtteranceEnd fires next
                self.last_partial = {"speaker": speaker_label, "text": transcript_text}

        except Exception as e:
            clog.log("error", f"[Deepgram] Message parsing error: {e}")

    def _on_utterance_end(self, _, utterance_end, **kwargs):
        """Rule 2: Trigger on UtteranceEnd if no preceding speech_final occurred."""
        if self.last_partial["text"]:
            speaker = self.last_partial["speaker"]
            text = self.last_partial["text"]
            
            clog.log("info", f"[Deepgram] STORED (UtteranceEnd Safety): {speaker}: {text}")
            
            # Force the Context Manager to move this from 'current_partial' to the 'history_queue'
            self.context_manager.update_transcript({
                "speaker": speaker,
                "text": text,
                "is_final": True
            })
            
            # Clear fallback to prevent double-storing
            self.last_partial = {"speaker": speaker, "text": ""}

    async def send_audio(self, pcm_bytes: bytes) -> None:
        if self.enabled and self.connection:
            await self._audio_queue.put(pcm_bytes)

    async def _audio_send_loop(self) -> None:
        while self.enabled and self.connection:
            pcm_bytes = await self._audio_queue.get()
            try:
                self.connection.send(pcm_bytes)
            except: break

    async def _keepalive_loop(self) -> None:
        while self.enabled and self.connection:
            await asyncio.sleep(5)
            try:
                self.connection.keep_alive()
            except: break

    async def stop(self) -> None:
        self.enabled = False
        if self.connection:
            try: self.connection.finish()
            except: pass
            self.connection = None
            clog.log("info", "[Deepgram] Stream stopped")