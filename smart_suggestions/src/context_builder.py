"""Build a compact, history-aware context for the Ollama prompt."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# Domains that are usually uninteresting for suggestions
_SKIP_DOMAINS = {
    "sun", "zone", "updater", "persistent_notification", "person",
    "device_tracker",
}

# Domains that appear in entity_states (context) but NOT in available_actions
_CONTEXT_ONLY_DOMAINS = {"sensor", "weather", "binary_sensor"}

# Domains that ARE interesting as potential actions
_ACTION_DOMAINS = {
    "light", "switch", "climate", "media_player", "cover",
    "fan", "lock", "vacuum", "input_boolean", "automation", "script", "scene",
}

# States that count as "inactive" for dormancy filtering
_INACTIVE_STATES = {"off", "idle", "paused", "standby", "closed", "locked"}

# Max history entries per entity to keep context compact
_MAX_HISTORY_ENTRIES = 6
# Max total entities in context
_MAX_ENTITIES = 80
# Hours without a state change before an inactive actionable entity is considered dormant
_DORMANCY_HOURS = 72

# Stale detection thresholds
_STALE_HOURS = 4.0
_STALE_HOURS_NO_DOW = 6.0


def _time_period(hour: int) -> str:
    if 5 <= hour < 9:
        return "early morning"
    if 9 <= hour < 12:
        return "morning"
    if 12 <= hour < 14:
        return "midday"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 21:
        return "evening"
    if 21 <= hour < 23:
        return "late evening"
    return "night"


def _last_changed_hours(state: dict) -> float:
    """Return hours since this entity last changed state."""
    lc = state.get("last_changed", "")
    if not lc:
        return 999.0
    try:
        dt = datetime.fromisoformat(lc.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return 999.0


_MEDIA_ACTIVE_STATES = {"playing", "buffering", "paused"}


def _is_interesting(state: dict) -> bool:
    """Return True if this entity is worth including in context."""
    s = state.get("state", "")
    if s in ("unavailable", "unknown", ""):
        return False
    domain = state.get("entity_id", "").split(".")[0]
    if domain in _SKIP_DOMAINS:
        return False

    # Media players actively playing content always surface regardless of dormancy.
    if domain == "media_player" and s in _MEDIA_ACTIVE_STATES:
        return True

    # For actionable entities: drop ones that are inactive and haven't been
    # touched in a long time — they're dormant and just add noise.
    if domain in _ACTION_DOMAINS and domain != "scene":
        if s in _INACTIVE_STATES and _last_changed_hours(state) > _DORMANCY_HOURS:
            return False

    return True


def _is_actionable(domain: str) -> bool:
    return domain in _ACTION_DOMAINS


def _summarise_history(history: list) -> str:
    """Turn a list of state history dicts into a compact natural language snippet."""
    if not history:
        return ""
    # Deduplicate consecutive identical states
    deduped = [history[0]]
    for h in history[1:]:
        if h.get("state") != deduped[-1].get("state"):
            deduped.append(h)
    deduped = deduped[-_MAX_HISTORY_ENTRIES:]
    parts = []
    for h in deduped:
        ts = h.get("last_changed", "")[:16].replace("T", " ")
        st = h.get("state", "?")
        parts.append(f"{st} at {ts}")
    return " → ".join(parts)


def _extract_scene_patterns(history: dict, states: dict) -> dict:
    """Extract typical activation hours for scenes from their history."""
    patterns = {}
    for eid, hist in history.items():
        if not eid.startswith("scene."):
            continue
        activation_hours: list[int] = []
        for h in hist:
            ts = h.get("last_changed", "")
            if ts and h.get("state") not in ("unavailable", "unknown", ""):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    activation_hours.append(dt.hour)
                except (ValueError, TypeError):
                    pass
        if activation_hours:
            name = (
                states.get(eid, {})
                .get("attributes", {})
                .get("friendly_name", eid)
            )
            patterns[eid] = {
                "name": name,
                "typical_activation_hours": activation_hours,
            }
    return patterns


def _scene_time_relevance(typical_hours: list[int], current_hour: int) -> float:
    """Score 0–10 for how close current_hour is to a scene's typical activation hours."""
    if not typical_hours:
        return 0.0
    min_dist = min(min(abs(current_hour - h), 24 - abs(current_hour - h)) for h in typical_hours)
    return round(max(0.0, 10.0 - min_dist * 1.5), 1)


def _count_changes(entity_id: str, history: dict) -> int:
    """Count distinct state transitions for an entity in history."""
    entries = history.get(entity_id, [])
    if len(entries) <= 1:
        return 0
    deduped = [entries[0]]
    for h in entries[1:]:
        if h.get("state") != deduped[-1].get("state"):
            deduped.append(h)
    return len(deduped) - 1


def _is_stale(state: dict, history: dict, dow_patterns_by_id: dict) -> bool:
    """Return True if this entity is stale and should be excluded from available_actions."""
    domain = state.get("entity_id", "").split(".")[0]
    if domain == "media_player":
        return False
    s = state.get("state", "")
    if s in _INACTIVE_STATES or s in ("unavailable", "unknown", ""):
        return False
    eid = state.get("entity_id", "")
    hours_active = _last_changed_hours(state)
    changes = _count_changes(eid, history)
    pattern = dow_patterns_by_id.get(eid)
    if pattern:
        return hours_active > _STALE_HOURS and changes == 0 and pattern["matches_pattern"]
    return hours_active > _STALE_HOURS_NO_DOW and changes == 0


def build_dow_patterns(dow_history: dict, states: dict) -> list:
    """Compute what state each entity is typically in at this time on this day (past 4 weeks)."""
    patterns = []
    for eid, entries in dow_history.items():
        if not entries:
            continue
        state_counts = Counter(
            e.get("state") for e in entries
            if e.get("state") not in ("unavailable", "unknown", None, "")
        )
        total = sum(state_counts.values())
        if total < 2:
            continue
        typical_state, count = state_counts.most_common(1)[0]
        confidence = count / total
        if confidence < 0.6:
            continue
        current = states.get(eid, {}).get("state", "")
        name = states.get(eid, {}).get("attributes", {}).get("friendly_name", eid)
        patterns.append({
            "entity_id": eid,
            "name": name,
            "typical_state": typical_state,
            "confidence": round(confidence, 2),
            "sample_count": total,
            "current_state": current,
            "matches_pattern": current == typical_state,
        })
    return patterns


def build_context(states: dict, history: dict, feedback: dict | None = None, dow_history: dict | None = None) -> dict:
    """Assemble the full context dict for prompt building."""
    if feedback is None:
        feedback = {}
    now = datetime.now()
    scene_patterns = _extract_scene_patterns(history, states)

    dow_patterns_list = build_dow_patterns(dow_history or {}, states)
    dow_patterns_by_id = {p["entity_id"]: p for p in dow_patterns_list}

    ctx = {
        "current_time": now.strftime("%H:%M"),
        "current_date": now.strftime("%A, %B %d %Y"),
        "day_of_week": now.strftime("%A"),
        "time_period": _time_period(now.hour),
        "entity_states": {},
        "available_actions": [],
        "history_summaries": {},
        "scene_patterns": scene_patterns,
        "feedback_signals": feedback,
        "dow_patterns": dow_patterns_list,
    }

    # Collect interesting entities, capped for token budget
    interesting = [
        s for s in states.values() if _is_interesting(s)
    ][:_MAX_ENTITIES]

    for state in interesting:
        eid = state["entity_id"]
        attrs = state.get("attributes", {})
        domain = eid.split(".")[0]

        ctx["entity_states"][eid] = {
            "state": state.get("state"),
            "friendly_name": attrs.get("friendly_name", eid),
            "domain": domain,
        }

        # Add actionable entities to available_actions (not sensors/weather)
        if _is_actionable(domain):
            # Skip stale always-on entities that match their DOW pattern
            if _is_stale(state, history, dow_patterns_by_id):
                _LOGGER.debug("Skipping stale entity: %s", eid)
                continue

            hours_active = _last_changed_hours(state)
            changes = _count_changes(eid, history)

            entry: dict = {
                "entity_id": eid,
                "name": attrs.get("friendly_name", eid),
                "current_state": state.get("state"),
                "domain": domain,
                "type": "automation" if domain == "automation"
                        else "script" if domain == "script"
                        else "entity",
                "last_changed_hours": round(hours_active, 1),
                "change_count": changes,
            }
            if domain == "media_player" and state.get("state") in _MEDIA_ACTIVE_STATES:
                entry["active"] = True
            if domain == "scene" and eid in scene_patterns:
                entry["time_relevance"] = _scene_time_relevance(
                    scene_patterns[eid].get("typical_activation_hours", []),
                    now.hour,
                )
            ctx["available_actions"].append(entry)

        # Add history summary if available
        if eid in history:
            summary = _summarise_history(history[eid])
            if summary:
                ctx["history_summaries"][eid] = summary

    return ctx


def build_prompt(ctx: dict, max_suggestions: int) -> str:
    entity_states_json = json.dumps(ctx["entity_states"], indent=2)
    actions_json = json.dumps(ctx["available_actions"], indent=2)
    history_json = json.dumps(ctx["history_summaries"], indent=2)
    scene_patterns_json = json.dumps(ctx["scene_patterns"], indent=2)
    dow_patterns_json = json.dumps(ctx.get("dow_patterns", []), indent=2)
    day_of_week = ctx.get("day_of_week", "")
    current_time = ctx["current_time"]
    feedback = ctx.get("feedback_signals", {})
    feedback_section = ""
    if feedback:
        feedback_json = json.dumps(feedback, indent=2)
        feedback_section = f"""
FEEDBACK HISTORY (user upvotes/downvotes on past suggestions):
{feedback_json}
Entities with more upvotes should be ranked higher. Entities with net negative votes should be ranked lower or omitted.
"""

    return f"""You are a smart home assistant. Based on the current context and recent history, suggest the most relevant actions a user might want to take RIGHT NOW.

CONTEXT:
- Time: {current_time} ({ctx['time_period']} on {ctx['current_date']})
- Entity States: {entity_states_json}
- Recent History (state transitions): {history_json}

SCENE USAGE PATTERNS:
The following shows which hours of the day each scene is typically activated (from history).
Rank scenes higher when the current hour ({datetime.now().hour}) is close to their typical activation hours.
{scene_patterns_json}

DAY-OF-WEEK PATTERNS (same {day_of_week}, ±2h of {current_time}, past 4 weeks):
{dow_patterns_json}

CRITICAL: Entities where "matches_pattern" is FALSE are your HIGHEST PRIORITY suggestions —
the user's own weekly routine says this entity should currently be in a different state than it is.
Entities where "matches_pattern" is TRUE are behaving as expected — deprioritize them unless other
strong signals apply (active media, scene timing, explicit user feedback).
{feedback_section}
AVAILABLE ACTIONS:
{actions_json}

Return ONLY a valid JSON array (no markdown, no explanation) with up to {max_suggestions} suggestions ranked by relevance. Each suggestion must have:
- "entity_id": the entity_id to act on
- "name": friendly display name
- "action": one of "toggle", "turn_on", "turn_off", "trigger", "navigate"
- "action_data": optional dict of service call data
- "reason": a SHORT 1-sentence explanation of WHY this is suggested right now
- "icon": a Material Design icon name (e.g. "mdi:lightbulb")
- "type": one of "entity", "automation", "script"
- "section": one of "suggested", "scene", or "stretch"

RANKING (follow strictly):
1. DOW pattern mismatch (matches_pattern=false) → TOP PRIORITY
2. High change_count or low last_changed_hours → recently active, likely relevant
3. NEVER suggest an action matching current state (no turn_off on "off", no turn_on on "on")
4. DOW pattern match → LOW priority unless strong secondary reason
5. 1-2 "stretch" suggestions (section: "stretch"): unexpected, delightful, pattern-based (e.g. "vacuum hasn't run in days", "it's Friday night", "sunset is soon")
6. scene.* → section "scene"; others → "suggested" or "stretch"

Only suggest actions that make contextual sense. Use history to understand patterns."""
