import os
import asyncio
import time
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveTranscriptionEvents,
)
from .moshi.context_manager import ContextManager
from .moshi.utils.logging import ColorizedLog

clog = ColorizedLog.randomize()

class DeepgramTranscriber:
    def __init__(self, context_manager: ContextManager, 
                 model: str = "nova-3",
                 language: str = "en-US"):
        self.context_manager = context_manager
        # v6+ logic: initialize without arguments to use environment variables
        self.client = DeepgramClient()
        self.model = model
        self.language = language
        self.connection = None
        self.enabled = True
        self._audio_queue = asyncio.Queue()
        self.receive_task = None
        self.keepalive_task = None
        clog.log("info", "[Deepgram] Initialized with latest SDK")

    async def start(self) -> None:
        if not self.enabled: return
        try:
            clog.log("info", "[Deepgram] Connecting to WebSocket...")
            # The most stable way to access the live client in v3/v6
            self.connection = self.client.listen.websocket.v("1")

            # Setup event handlers using the event names directly
            self.connection.on(LiveTranscriptionEvents.Open, lambda *args, **kwargs: clog.log("info", "[Deepgram] Connection opened"))
            self.connection.on(LiveTranscriptionEvents.Transcript, self._on_message)
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

            if self.connection.start(options):
                self.receive_task = asyncio.create_task(self._audio_send_loop())
                self.keepalive_task = asyncio.create_task(self._keepalive_loop())
                clog.log("info", "[Deepgram] Stream active and listening")
        except Exception as e:
            clog.log("error", f"[Deepgram] Start failure: {e}")
            self.enabled = False

    async def send_audio(self, pcm_bytes: bytes) -> None:
        if self.enabled and self.connection:
            await self._audio_queue.put(pcm_bytes)

    async def _audio_send_loop(self) -> None:
        while self.enabled and self.connection:
            pcm_bytes = await self._audio_queue.get()
            try:
                self.connection.send(pcm_bytes)
            except Exception as e:
                clog.log("error", f"[Deepgram] Send error: {e}")

    def _on_message(self, *args, **kwargs):
        # Handle different SDK return signatures safely
        result = args[1] if len(args) > 1 else None
        if not result or not hasattr(result, 'channel'): return

        try:
            words = result.channel.alternatives[0].words
            is_final = getattr(result, 'is_final', False)
            if not words: return

            transcript_text = " ".join([w.word for w in words])
            speaker_id = getattr(words[0], 'speaker', 0)
            speaker_label = f"Speaker {speaker_id}"

            self.context_manager.update_transcript({
                "speaker": speaker_label,
                "text": transcript_text,
                "is_final": is_final
            })
            
            if is_final:
                clog.log("info", f"[Deepgram] FINAL: {speaker_label}: {transcript_text}")
        except Exception as e:
            clog.log("error", f"[Deepgram] Callback error: {e}")

    async def _keepalive_loop(self) -> None:
        while self.enabled and self.connection:
            await asyncio.sleep(5)
            try:
                self.connection.keep_alive()
            except: break

    async def stop(self) -> None:
        self.enabled = False
        if self.connection:
            try:
                self.connection.finish()
            except: pass
            self.connection = None
            clog.log("info", "[Deepgram] Stream stopped")

            