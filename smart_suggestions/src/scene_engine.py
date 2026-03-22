"""Scene-first suggestion ranking. Applies feedback, noop filter, confidence labels."""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


def _remove_noops(candidates: list[dict], states: dict) -> list[dict]:
    """Remove turn_on/turn_off suggestions that match current state. Scenes are never filtered."""
    out = []
    for c in candidates:
        action = c.get("action", "")
        eid = c.get("entity_id", "")
        current = states.get(eid, {}).get("state", "")
        if action == "turn_off" and current == "off":
            continue
        if action == "turn_on" and current == "on":
            continue
        out.append(c)
    return out


def _confidence_label(score: float, routine_match: bool) -> str:
    if routine_match or score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


class SceneEngine:
    def __init__(self, max_suggestions: int = 7, confidence_threshold: float = 0.6) -> None:
        self._max = max_suggestions
        self._confidence_threshold = confidence_threshold

    def rank(self, candidates: list[dict], states: dict, feedback: dict) -> list[dict]:
        """Apply feedback + noop filter, sort scenes first, return top N."""
        # Apply feedback signals
        scored = []
        for c in candidates:
            eid = c["entity_id"]
            fb = feedback.get(eid, {})
            net = 0
            if isinstance(fb, dict):
                net = fb.get("up", 0) - fb.get("down", 0)
            # Hard exclude: net vote <= -3
            if net <= -3:
                _LOGGER.info("Excluding hard-downvoted entity: %s", eid)
                continue
            updated = dict(c)
            updated["score"] = c.get("score", 0) + (net * 8 if net > 0 else net * 10)
            updated["confidence"] = _confidence_label(updated["score"], c.get("routine_match", False))
            # Assign action if not already set
            if "action" not in updated:
                if updated.get("domain") == "scene":
                    updated["action"] = "activate"
                elif updated.get("current_state") == "on":
                    updated["action"] = "turn_off"
                else:
                    updated["action"] = "turn_on"
            scored.append(updated)

        # Remove noops
        scored = _remove_noops(scored, states)

        # Sort: scenes first (by score), then others (by score)
        scored.sort(key=lambda c: (c.get("type") != "scene", -c.get("score", 0)))

        return scored[:self._max]
