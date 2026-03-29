# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

import asyncio
import os
import time
from typing import Optional
import numpy as np

try:
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler
    from amazon_transcribe.model import TranscriptEvent
    AWS_AVAILABLE = True
except ImportError:
    AWS_AVAILABLE = False
    TranscribeStreamingClient = None
    TranscriptResultStreamHandler = None

from .utils.logging import ColorizedLog

clog = ColorizedLog.randomize()

# AWS Configuration from environment
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")


class AWSHandler(TranscriptResultStreamHandler):
    """Handles AWS Transcribe streaming events with reliable speaker detection."""
    
    def __init__(self, output_stream, context_manager):
        super().__init__(output_stream)
        self.context_manager = context_manager
    
    async def handle_transcript_event(self, transcript_event: TranscriptEvent) -> None:
        """Process transcript events from AWS Transcribe stream with error handling."""
        try:
            results = transcript_event.transcript.results
            clog.log("info", f"[AWS] Received {len(results)} results from AWS")
            
            for result in results:
                # Process BOTH partial and final transcripts
                transcript_text = ""
                is_partial = result.is_partial
                
                if not result.alternatives:
                    continue
                
                alt = result.alternatives[0]
                transcript_text = alt.transcript.strip()
                
                if not transcript_text:
                    continue
                
                # FIX: Reliable speaker detection with proper null checking
                speaker_label = "Speaker 0"
                if hasattr(result, 'speaker_label') and result.speaker_label is not None:
                    speaker_label = f"Speaker {result.speaker_label}"
                
                log_type = "PARTIAL" if is_partial else "FINAL"
                clog.log("info", f"[AWS] {log_type} transcript: {speaker_label}: {transcript_text}")
                
                # Only store FINAL transcripts to context
                if not is_partial:
                    self.context_manager.update_transcript({
                        "speaker": speaker_label,
                        "text": transcript_text,
                        "is_final": True
                    })
                    clog.log("info", f"[AWS TRANSCRIPT STORED] {speaker_label}: {transcript_text}")
                
        except Exception as e:
            clog.log("error", f"[AWS ERROR] Failed to process transcript: {str(e)}")


class AWSTranscriber:
    """
    Manages a SINGLE persistent AWS Transcribe stream per connection.
    
    PCM chunks are sent to this stream without creating new streams.
    Results are auto-stored in ContextManager via async event handler.
    """
    
    def __init__(self, context_manager):
        """Initialize transcriber with context manager reference."""
        if not AWS_AVAILABLE:
            clog.log("warning", "[AWS] AWS Transcribe libraries not available")
            self.enabled = False
            return
        
        self.context_manager = context_manager
        self.client = TranscribeStreamingClient(region=AWS_REGION)
        self.stream = None
        self.enabled = True
        self.last_audio_time = 0  # Track last audio chunk for keepalive
        self.keepalive_task = None  # Task for sending silence keepalive
        clog.log("info", "[AWS] AWSTranscriber initialized (single persistent stream mode)")
    
    async def start(self) -> None:
        """
        Start a SINGLE persistent AWS Transcribe stream for this connection.
        
        This stream will live for the entire connection lifetime.
        PCM chunks are sent to it via send_audio().
        Results processed automatically in background.
        """
        if not self.enabled:
            clog.log("warning", "[AWS] AWS disabled, skipping stream start")
            return
        
        try:
            clog.log("info", "[AWS] Starting persistent stream...")
            
            # Create ONE stream that lives for entire connection
            self.stream = await self.client.start_stream_transcription(
                language_code="en-US",
                media_sample_rate_hz=16000,  # AWS standard
                media_encoding="pcm",
                show_speaker_label=True,  # Enable speaker diarization
            )
            
            # Start receive loop in background (runs for entire connection)
            asyncio.create_task(self._receive_loop())
            
            # Start keepalive task (sends silence to keep stream alive)
            self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            clog.log("info", "[AWS] Persistent stream created and listening with keepalive")
            
        except Exception as e:
            clog.log("error", f"[AWS] Failed to start stream: {e}")
            self.enabled = False
    
    async def send_audio(self, pcm_bytes: bytes) -> None:
        """
        Send PCM audio chunk to persistent stream.
        
        This is called for every PCM frame. Uses same stream (not new one).
        
        Args:
            pcm_bytes: int16 PCM audio bytes, 16kHz sample rate
        """
        if not self.enabled or not self.stream:
            return
        
        try:
            import time
            self.last_audio_time = time.time()  # Track for keepalive
            await self.stream.input_stream.send_audio_event(audio_chunk=pcm_bytes)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            clog.log("error", f"[AWS] Error sending audio: {e}")
    
    async def _receive_loop(self) -> None:
        """
        Background loop that listens for events from persistent stream.
        
        Runs entire connection lifetime.
        Auto-stores results in ContextManager.
        """
        if not self.stream:
            return
        
        try:
            handler = AWSHandler(self.stream.output_stream, self.context_manager)
            clog.log("info", "[AWS] Starting event handler for persistent stream")
            await handler.handle_events()
            clog.log("info", "[AWS] Event handler completed")
        except asyncio.CancelledError:
            clog.log("info", "[AWS] Receive loop cancelled")
            pass
        except Exception as e:
            clog.log("error", f"[AWS] Receive loop error: {e}")
        finally:
            clog.log("info", "[AWS] Persistent stream closed")
    
    async def _keepalive_loop(self) -> None:
        """
        Send silence frames every 5 seconds to keep AWS stream alive.
        
        AWS closes connections after ~15s of no audio.
        This keepalive prevents timeout by sending silence periodically.
        """
        if not self.stream:
            return
        
        try:
            import time as time_module
            while self.stream and self.enabled:
                await asyncio.sleep(5)  # Every 5 seconds
                
                # Check if we haven't sent real audio in last 10 seconds
                time_since_last_audio = time_module.time() - self.last_audio_time
                if time_since_last_audio > 8:  # If idle for 8+ seconds
                    # Send silence frame (1/100th of a second at 16kHz = 160 bytes of silence)
                    silence_frame = np.zeros(160, dtype=np.int16).tobytes()
                    try:
                        await self.stream.input_stream.send_audio_event(audio_chunk=silence_frame)
                        clog.log("info", "[AWS] Keepalive: sent silence frame to keep stream alive")
                    except Exception as e:
                        clog.log("info", f"[AWS] Keepalive failed (stream may have closed): {e}")
                        break
        except asyncio.CancelledError:
            clog.log("info", "[AWS] Keepalive loop cancelled")
        except Exception as e:
            clog.log("error", f"[AWS] Keepalive loop error: {e}")
    
    async def stop(self) -> None:
        """Close persistent stream (called on connection close)."""
        # Cancel keepalive task
        if self.keepalive_task:
            self.keepalive_task.cancel()
            try:
                await self.keepalive_task
            except asyncio.CancelledError:
                pass
        
        if self.stream:
            try:
                await self.stream.input_stream.end_stream()
                self.stream = None
                clog.log("info", "[AWS] Stream stopped")
            except Exception as e:
                clog.log("error", f"[AWS] Error stopping stream: {e}")

