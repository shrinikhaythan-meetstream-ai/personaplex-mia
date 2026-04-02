import os
import threading
import time
from typing import Generator, Any
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results

class DeepgramTranscriber:
    def __init__(self, 
                 model: str = "nova-3",
                 language: str = "en-US",
                 punctuate: bool = True,
                 smart_format: bool = True,
                 interim_results: bool = True,
                 diarize: bool = True):
        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise EnvironmentError("DEEPGRAM_API_KEY environment variable not set.")
        self.client = DeepgramClient(api_key)
        self.model = model
        self.language = language
        self.punctuate = punctuate
        self.smart_format = smart_format
        self.interim_results = interim_results
        self.diarize = diarize
        self.connection = None
        self._stop_keepalive = threading.Event()
        self._results = []
        self._lock = threading.Lock()

    def _on_message(self, message: Any):
        if isinstance(message, ListenV1Results):
            words = message.channel.alternatives[0].words if message.channel else []
            if words and message.is_final:
                with self._lock:
                    for w in words:
                        self._results.append({
                            "speaker": w.speaker,
                            "word": w.word
                        })

    def _keep_alive_loop(self):
        while not self._stop_keepalive.is_set():
            if self.connection:
                self.connection.send_keep_alive()
            time.sleep(5)

    def stream(self, audio_chunks: Generator[bytes, None, None]) -> Generator[dict, None, None]:
        """
        Accepts a generator of audio chunks (bytes) and yields dicts with 'speaker' and 'word'.
        """
        with self.client.listen.v1.connect(
            model=self.model,
            language=self.language,
            punctuate=self.punctuate,
            smart_format=self.smart_format,
            interim_results=self.interim_results,
            diarize=self.diarize,
        ) as connection:
            self.connection = connection
            connection.on(EventType.OPEN, lambda _: None)
            connection.on(EventType.MESSAGE, self._on_message)
            connection.on(EventType.CLOSE, lambda _: None)
            connection.on(EventType.ERROR, lambda err: print(f"Deepgram error: {err}"))

            # Start keep-alive thread
            ka_thread = threading.Thread(target=self._keep_alive_loop, daemon=True)
            ka_thread.start()

            connection.start_listening()
            try:
                for chunk in audio_chunks:
                    connection.send_media(chunk)
                    # Yield results as they come in
                    with self._lock:
                        while self._results:
                            yield self._results.pop(0)
            finally:
                self._stop_keepalive.set()
                ka_thread.join(timeout=1)
                connection.finish()

# Usage example (to be called by the main router):
# from deepgram_transcriber import DeepgramTranscriber
# transcriber = DeepgramTranscriber()
# for result in transcriber.stream(audio_chunks):
#     print(result)  # {'speaker': ..., 'word': ...}
