"""Build a compact, history-aware context for the Ollama prompt."""
from __future__ import annotations

import json
import logging
from datetime import datetime

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

# Max history entries per entity to keep context compact
_MAX_HISTORY_ENTRIES = 6
# Max total entities in context
_MAX_ENTITIES = 80


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


def _is_interesting(state: dict) -> bool:
    """Return True if this entity is worth including in context."""
    s = state.get("state", "")
    if s in ("unavailable", "unknown", ""):
        return False
    domain = state.get("entity_id", "").split(".")[0]
    if domain in _SKIP_DOMAINS:
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


def build_context(states: dict, history: dict) -> dict:
    """Assemble the full context dict for prompt building."""
    now = datetime.now()
    ctx = {
        "current_time": now.strftime("%H:%M"),
        "current_date": now.strftime("%A, %B %d %Y"),
        "time_period": _time_period(now.hour),
        "entity_states": {},
        "available_actions": [],
        "history_summaries": {},
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
            ctx["available_actions"].append({
                "entity_id": eid,
                "name": attrs.get("friendly_name", eid),
                "current_state": state.get("state"),
                "domain": domain,
                "type": "automation" if domain == "automation"
                        else "script" if domain == "script"
                        else "entity",
            })

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

    return f"""You are a smart home assistant. Based on the current context and recent history, suggest the most relevant actions a user might want to take RIGHT NOW.

CONTEXT:
- Time: {ctx['current_time']} ({ctx['time_period']} on {ctx['current_date']})
- Entity States: {entity_states_json}
- Recent History (state transitions): {history_json}

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

Only suggest actions that make contextual sense RIGHT NOW. Use history to understand patterns. Do not suggest turning something off that is already off."""
