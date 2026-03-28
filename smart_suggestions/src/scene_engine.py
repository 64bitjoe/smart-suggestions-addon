"""Suggestion ranking. Applies feedback, noop filter, confidence labels, and diversity."""
from __future__ import annotations

import logging

from const import FEEDBACK_UPVOTE_MULTIPLIER, FEEDBACK_DOWNVOTE_MULTIPLIER, FEEDBACK_HARD_EXCLUDE_THRESHOLD

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
        # Note: confidence_threshold is passed through to callers for can_save_as_automation
        # eligibility checks; it does not filter suggestions at the ranking layer.
        self._confidence_threshold = confidence_threshold

    def rank(self, candidates: list[dict], states: dict, feedback: dict) -> list[dict]:
        """Apply feedback + noop filter, sort by score, enforce diversity, return top N."""
        # Apply feedback signals
        scored = []
        for c in candidates:
            eid = c["entity_id"]
            fb = feedback.get(eid, {})
            net = 0
            if isinstance(fb, dict):
                net = fb.get("up", 0) - fb.get("down", 0)
            # Hard exclude: net vote <= FEEDBACK_HARD_EXCLUDE_THRESHOLD
            if net <= FEEDBACK_HARD_EXCLUDE_THRESHOLD:
                _LOGGER.info("Excluding hard-downvoted entity: %s", eid)
                continue
            updated = dict(c)
            updated["score"] = c.get("score", 0) + (net * FEEDBACK_UPVOTE_MULTIPLIER if net > 0 else net * FEEDBACK_DOWNVOTE_MULTIPLIER)
            updated["confidence"] = _confidence_label(updated["score"], c.get("routine_match", False))
            # Assign action if not already set
            if "action" not in updated:
                domain = updated.get("domain", "")
                current_state = states.get(eid, {}).get("state", "")
                if domain == "scene":
                    updated["action"] = "activate"
                elif domain == "automation":
                    updated["action"] = "trigger"
                elif domain == "script":
                    updated["action"] = "turn_on"
                elif domain == "lock":
                    updated["action"] = "lock" if current_state == "unlocked" else "unlock"
                elif domain == "cover":
                    updated["action"] = "close_cover" if current_state == "open" else "open_cover"
                elif current_state == "on":
                    updated["action"] = "turn_off"
                else:
                    updated["action"] = "turn_on"
            scored.append(updated)

        # Remove noops
        scored = _remove_noops(scored, states)

        # Sort by score descending
        scored.sort(key=lambda c: -c.get("score", 0))

        # Enforce diversity: max 3 suggestions from any single domain
        diverse = []
        domain_counts: dict[str, int] = {}
        for c in scored:
            d = c.get("domain", "")
            if domain_counts.get(d, 0) >= 3:
                continue
            diverse.append(c)
            domain_counts[d] = domain_counts.get(d, 0) + 1
        return diverse[:self._max]
