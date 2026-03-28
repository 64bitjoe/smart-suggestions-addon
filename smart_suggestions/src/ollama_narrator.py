"""Backward-compatible re-export — narrator logic lives in narrator.py."""
from narrator import OllamaNarrator, AINarrator, _build_prompt, _apply_reasons

__all__ = ["OllamaNarrator", "AINarrator", "_build_prompt", "_apply_reasons"]
