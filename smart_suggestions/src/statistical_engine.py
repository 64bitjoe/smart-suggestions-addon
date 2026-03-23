# smart_suggestions/src/statistical_engine.py
"""Deterministic scoring engine — no LLM. Real-time + background paths."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from const import _ACTION_DOMAINS, _INACTIVE_STATES

if TYPE_CHECKING:
    from pattern_store import PatternStore

_LOGGER = logging.getLogger(__name__)

_ROUTINE_WINDOW_MINUTES = 30
_SCENE_MATCH_THRESHOLD = 0.6
_DAY_ABBREV_MAP = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def score_scene_match(scene_eid: str, states: dict) -> float:
    """Return fraction of scene members already in their target state (0.0–1.0)."""
    scene_state = states.get(scene_eid, {})
    attrs = scene_state.get("attributes", {})
    entities_dict: dict = attrs.get("entities", {})
    if not entities_dict:
        return 0.0
    matching = sum(
        1 for member_eid, target in entities_dict.items()
        if isinstance(target, dict)
        and states.get(member_eid, {}).get("state") == target.get("state")
    )
    return round(matching / len(entities_dict), 2)


def _time_diff_minutes(t1: str, t2: str) -> float:
    """Return absolute minute difference between two HH:MM strings (circular, max 12h)."""
    try:
        h1, m1 = (int(x) for x in t1.split(":"))
        h2, m2 = (int(x) for x in t2.split(":"))
        total1 = h1 * 60 + m1
        total2 = h2 * 60 + m2
        diff = abs(total1 - total2)
        return min(diff, 1440 - diff)
    except (ValueError, AttributeError):
        return 999.0


def _routine_matches_now(routine: dict, now: datetime) -> tuple[bool, float]:
    """Return (matches, score_boost 0–40)."""
    today_abbrev = _DAY_ABBREV_MAP.get(now.weekday(), "")
    if today_abbrev not in routine.get("days", []):
        return False, 0.0
    current_time = now.strftime("%H:%M")
    diff = _time_diff_minutes(routine.get("typical_time", ""), current_time)
    if diff > _ROUTINE_WINDOW_MINUTES:
        return False, 0.0
    # Score: max 40 pts at 0 diff, scaling to 0 at window edge
    boost = max(0.0, 40.0 * (1.0 - diff / _ROUTINE_WINDOW_MINUTES))
    confidence = routine.get("confidence", 0.5)
    return True, round(boost * confidence, 1)


class StatisticalEngine:
    def __init__(self, pattern_store: "PatternStore", confidence_threshold: float = 0.6,
                 allowed_domains: list[str] | None = None, max_entities: int = 150) -> None:
        self._store = pattern_store
        self._confidence_threshold = confidence_threshold
        self._allowed_domains = set(allowed_domains) if allowed_domains else None
        self._max_entities = max_entities

    def score_realtime(self, states: dict) -> list[dict]:
        """Score all actionable entities. Scenes first. Returns sorted candidate list."""
        now = datetime.now(timezone.utc)
        routines_by_eid = {r["entity_id"]: r for r in self._store.get_routines()}
        correlations = self._store.get_correlations()
        anomalies_by_eid = {a["entity_id"]: a for a in self._store.get_active_anomalies()}

        # Separate scenes (always included) from other entities
        scene_eids = [eid for eid in states if eid.split(".")[0] == "scene"]
        other_eids = [
            eid for eid in states
            if eid.split(".")[0] != "scene"
            and eid.split(".")[0] in _ACTION_DOMAINS
            and (self._allowed_domains is None or eid.split(".")[0] in self._allowed_domains)
        ]

        # Hourly-seeded random sample of non-scene entities
        if len(other_eids) > self._max_entities:
            seed = int(time.time() // 3600)
            rng = random.Random(seed)
            other_eids = rng.sample(other_eids, self._max_entities)

        eids_to_score = scene_eids + other_eids

        candidates = []

        for eid in eids_to_score:
            state = states.get(eid, {})
            domain = eid.split(".")[0]
            s = state.get("state", "")
            if not s or s in ("unavailable", "unknown"):
                continue

            score = 0.0
            routine_match = False
            match_ratio = 0.0
            reason_parts = []

            # Scene-specific scoring
            if domain == "scene":
                match_ratio = score_scene_match(eid, states)
                if match_ratio >= _SCENE_MATCH_THRESHOLD:
                    score += match_ratio * 30
                    reason_parts.append(f"{int(match_ratio * 100)}% of members already in target state")

            # Routine match boost
            if eid in routines_by_eid:
                routine = routines_by_eid[eid]
                routine_match, boost = _routine_matches_now(routine, now)
                if routine_match:
                    score += boost
                    reason_parts.append(f"you usually do this around {routine.get('typical_time')} on {routine.get('days', [])}")

            # Anomaly boost
            if eid in anomalies_by_eid:
                anomaly = anomalies_by_eid[eid]
                score += 15
                reason_parts.append(anomaly.get("description", "unusual state"))

            # Active correlation boost — if entity_a is active, boost entity_b
            for corr in correlations:
                if corr.get("entity_b") == eid:
                    entity_a_state = states.get(corr.get("entity_a", ""), {}).get("state", "")
                    if entity_a_state not in _INACTIVE_STATES and entity_a_state not in ("unavailable", "unknown", ""):
                        score += corr.get("confidence", 0.5) * 20
                        reason_parts.append(corr.get("pattern", "correlated with active device"))

            # Only include entities with some signal
            if score > 0 or domain == "scene":
                name = state.get("attributes", {}).get("friendly_name", eid)
                candidates.append({
                    "entity_id": eid,
                    "name": name,
                    "domain": domain,
                    "type": "scene" if domain == "scene" else (
                        "automation" if domain == "automation" else
                        "script" if domain == "script" else "entity"
                    ),
                    "current_state": s,
                    "score": round(score, 1),
                    "match_ratio": match_ratio,
                    "routine_match": routine_match,
                    "reason": "; ".join(reason_parts) if reason_parts else "",
                    "can_save_as_automation": (
                        domain == "scene"
                        and routine_match
                        and eid in routines_by_eid
                        and routines_by_eid[eid].get("confidence", 0) >= self._confidence_threshold
                    ),
                    "automation_context": (
                        {
                            "typical_time": routines_by_eid[eid].get("typical_time"),
                            "days": routines_by_eid[eid].get("days", []),
                        }
                        if domain == "scene" and eid in routines_by_eid else None
                    ),
                })

        # Sort: scenes first (by score), then others
        candidates.sort(key=lambda c: (c["type"] != "scene", -c["score"]))
        return candidates

    async def analyze_correlations(self, history: dict, states: dict, window_minutes: int = 5) -> list[dict]:
        """Background task: scan history for co-occurrence correlations. O(n²) — run infrequently."""
        # Filter to actionable entities with history
        action_eids = [
            eid for eid in history
            if eid.split(".")[0] in _ACTION_DOMAINS and len(history[eid]) > 1
        ]

        # Build event timeline: (timestamp, entity_id, state) sorted by time
        events = []
        for eid in action_eids:
            for entry in history[eid]:
                ts_str = entry.get("last_changed", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    events.append((ts, eid, entry.get("state", "")))
                except (ValueError, TypeError):
                    continue
        events.sort(key=lambda e: e[0])

        # Count co-occurrence within window
        co_counts: dict[tuple, int] = defaultdict(int)
        total_counts: dict[str, int] = defaultdict(int)
        window = timedelta(minutes=window_minutes)

        for i, (ts_a, eid_a, state_a) in enumerate(events):
            if state_a in _INACTIVE_STATES or state_a in ("unavailable", "unknown"):
                continue
            total_counts[eid_a] += 1
            await asyncio.sleep(0)  # yield to event loop between iterations
            for j in range(i + 1, len(events)):
                ts_b, eid_b, state_b = events[j]
                if ts_b - ts_a > window:
                    break
                if eid_b != eid_a and state_b not in _INACTIVE_STATES:
                    co_counts[(eid_a, eid_b)] += 1

        correlations = []
        for (eid_a, eid_b), count in co_counts.items():
            if count < 3:  # minimum 3 occurrences
                continue
            total_a = total_counts.get(eid_a, 1)
            confidence = round(min(count / total_a, 1.0), 2)
            if confidence < 0.4:
                continue
            name_a = states.get(eid_a, {}).get("attributes", {}).get("friendly_name", eid_a)
            name_b = states.get(eid_b, {}).get("attributes", {}).get("friendly_name", eid_b)
            correlations.append({
                "entity_a": eid_a,
                "entity_b": eid_b,
                "pattern": f"{name_b} often changes within {window_minutes}min of {name_a}",
                "confidence": confidence,
                "window_minutes": window_minutes,
                "source": "statistical",
            })

        _LOGGER.info("Correlation scan: %d correlations found from %d entities", len(correlations), len(action_eids))
        return correlations
