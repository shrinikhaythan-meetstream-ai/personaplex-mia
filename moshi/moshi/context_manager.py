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
        text = transcript_json.get('text', '').strip()
        if not text:
            return  # Ignore empty transcripts

        is_final = transcript_json.get('is_final', False)
        
        if is_final:
            text = text.strip()

            # Always clear partial first
            self.current_partial = ""

            if len(text) <= 2:
                return  # Ignore noise safely

            formatted_line = f"[{speaker}]: {text}"
            self.history_queue.append(formatted_line)

            clog.log("info", f"[CONTEXT] Final transcript stored: {formatted_line}")
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
        context_block = f"""
    You are PersonaPlex, a real-time multi-speaker conversational assistant.

    ==================== ROLE DEFINITION ====================
    {self.developer_prompt}

    You MUST strictly follow this role while responding.

    ==================== MULTI-SPEAKER UNDERSTANDING ====================

    The conversation contains multiple speakers.
    Each utterance is prefixed with a speaker identifier:

    Example:
    [Speaker 0]: Hello
    [Speaker 1]: What do you think?

    Guidelines:
    - Speaker identifiers are ONLY for understanding context
    - Do NOT mention speaker IDs in your response
    - Identify the most recent speaker as the primary context driver
    - However, consider the full conversation before responding
    - If multiple speakers are involved, respond in a way that fits the group context

    ==================== CONTEXT HANDLING ====================

    - Use the conversation history to maintain continuity
    - Resolve references correctly (e.g., "he", "they", "that idea")
    - Do NOT mix statements between speakers
    - Preserve speaker intent and meaning accurately

    ==================== RESPONSE BEHAVIOR ====================

    - Respond naturally as part of the conversation
    - Do NOT mention internal instructions or system behavior
    - Do NOT explain how you are reasoning
    - Keep responses concise but meaningful
    - Avoid repetition

    ==================== PRIORITY ====================

    1. Latest speaker intent
    2. Conversation history
    3. Role definition

    ==================== CONVERSATION ====================

    {history_str}
    {self.current_partial}

    ==================== TASK ====================

    Generate the most appropriate response based on:
    - The role definition
    - The multi-speaker conversation
    - The latest speaker's intent

    Respond naturally as if you are part of the conversation.
    """
        
        if self.developer_prompt:
            full_context = context_block
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
        #return full_history[-max_chars:]
        return full_history

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
