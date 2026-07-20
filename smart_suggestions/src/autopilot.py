"""Pure helpers for graduated-trust auto-run decisions."""
from __future__ import annotations

from lifecycle import LIFECYCLE_AUTOPILOT

MAX_AUTORUNS_PER_HOUR = 10


def partition_matches(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split context-matched rows into (to_execute, to_suggest)."""
    execute = [r for r in items if r.get("lifecycle") == LIFECYCLE_AUTOPILOT]
    suggest = [r for r in items if r.get("lifecycle") != LIFECYCLE_AUTOPILOT]
    return execute, suggest


def within_throttle(recent_autoruns: int) -> bool:
    return recent_autoruns < MAX_AUTORUNS_PER_HOUR
