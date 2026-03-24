# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

import collections
from typing import Dict, Optional, Any
from .utils.logging import ColorizedLog

clog = ColorizedLog.randomize()


class ContextManager:
    """
    Maintains multi-user speaker-aware conversation history.
    
    Stores last N utterances in [Speaker X]: text format.
    Prepends context to original prompt for model inference.
    """
    
    def __init__(self, developer_prompt: str = "", max_history: int = 15):
        """
        Initialize ContextManager with optional developer prompt.
        
        Args:
            developer_prompt: System role/persona instruction
            max_history: Maximum number of utterances to keep (FIFO)
        """
        self.developer_prompt = developer_prompt
        # deque automatically drops oldest items when max_history is exceeded
        self.history_queue = collections.deque(maxlen=max_history)
        self.current_partial = ""
        self.max_history = max_history
        
    def update_transcript(self, transcript_json: Dict[str, Any]) -> None:
        """
        Process incoming JSON from AWS Transcribe or manual updates.
        
        Args:
            transcript_json: Dict with keys:
                - 'speaker': Speaker label (e.g., "Speaker 0", "Moshi")
                - 'text': Transcribed/generated text
                - 'is_final': Boolean, True if final transcript
                - 'start_time': (optional) Start time
                - 'end_time': (optional) End time
        """
        speaker = transcript_json.get('speaker', 'Unknown')
        text = transcript_json.get('text', '')
        is_final = transcript_json.get('is_final', False)
        
        if is_final:
            # Format as a script line and lock into history
            formatted_line = f"[{speaker}]: {text}"
            self.history_queue.append(formatted_line)
            
            # Log to terminal
            clog.log("info", f"[CONTEXT] Final transcript stored: {formatted_line}")
            
            # Clear partial since the sentence is finished
            self.current_partial = ""
        else:
            # Update the latest "live" sentence (for real-time display, not stored)
            self.current_partial = f"[{speaker}]: {text}..."
            clog.log("info", f"[CONTEXT] Partial transcript: {self.current_partial}")
    
    def get_full_prompt(self) -> str:
        """
        Build complete prompt: Developer Role + History + Partial.
        Used for injecting context into model inference.
        
        Returns:
            Formatted context string suitable for model input
        """
        history_str = "\n".join(self.history_queue) if self.history_queue else ""
        
        # Production-ready context format
        context_block = (
            f"You are PersonaPlex, a real-time meeting assistant.\n"
            f"Understand multi-speaker conversation and respond naturally.\n\n"
            f"Conversation so far:\n"
            f"{history_str}\n\n"
            f"Respond based on the context above."
        )
        
        if self.developer_prompt:
            full_context = f"{self.developer_prompt}\n\n{context_block}"
        else:
            full_context = context_block
        
        return full_context
    
    def get_history_only(self) -> str:
        """
        Get ONLY the history without developer prompt.
        
        Returns:
            Formatted history string
        """
        return "\n".join(self.history_queue) if self.history_queue else ""
    
    def get_recent_context(self, max_chars: int = 2000) -> str:
        """
        Get context limited to recent N characters (prevent token overflow).
        Useful for production stability with token limits.
        
        Args:
            max_chars: Maximum character count to return (default 2000)
            
        Returns:
            Recent context string, truncated if needed
        """
        full_history = "\n".join(self.history_queue) if self.history_queue else ""
        if len(full_history) <= max_chars:
            return full_history
        # Return last N characters to keep recent context
        return full_history[-max_chars:]
    
    def clear_context(self) -> None:
        """Clear all stored history and partials."""
        self.history_queue.clear()
        self.current_partial = ""
        clog.log("info", "[CONTEXT] Context cleared")
    
    def get_history_size(self) -> int:
        """Return current number of stored utterances."""
        return len(self.history_queue)
    
    def log_state(self) -> None:
        """Log current context state to terminal."""
        clog.log("info", f"[CONTEXT] History size: {self.get_history_size()}/{self.max_history}")
        clog.log("info", f"[CONTEXT] Current state:\n{self.get_full_prompt()}")
