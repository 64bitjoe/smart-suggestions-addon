"""Deep behavioral pattern analysis via Ollama (Phase A — runs every 2h)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

_PATTERNS_FILE = "/data/patterns.json"

_EMPTY_PATTERNS: dict = {
    "routines": [],
    "correlations": [],
    "right_now": [],
    "anomalies": [],
}


def _compact_history(history: dict, states: dict) -> dict:
    """Return a compact summary of 48h history — only actionable entities with changes."""
    from context_builder import _ACTION_DOMAINS, _summarise_history  # noqa: PLC0415

    out = {}
    for eid, entries in history.items():
        domain = eid.split(".")[0]
        if domain not in _ACTION_DOMAINS:
            continue
        if not entries:
            continue
        # Only include entities that had at least one state change
        states_seen = {e.get("state") for e in entries}
        if len(states_seen) < 2:
            continue
        summary = _summarise_history(entries)
        if summary:
            name = states.get(eid, {}).get("attributes", {}).get("friendly_name", eid)
            out[eid] = {"name": name, "history": summary}
    return out


class PatternAnalyzer:
    """Calls Ollama to extract behavioral patterns from 48h entity history."""

    def __init__(self, ollama_client) -> None:
        self._ollama = ollama_client

    async def analyze(self, history_48h: dict, states: dict) -> dict:
        """Run deep analysis. Returns structured pattern dict or empty on failure."""
        compact = _compact_history(history_48h, states)
        if not compact:
            _LOGGER.info("Pattern analysis: no actionable history to analyze")
            return {}

        now = datetime.now()
        day_of_week = now.strftime("%A")
        current_time = now.strftime("%H:%M")

        history_json = json.dumps(compact, indent=2)

        prompt = f"""You are analyzing a smart home's 48-hour entity history to extract behavioral patterns.

CURRENT TIME: {current_time} on {day_of_week}

ENTITY HISTORY (compact format, state transitions over past 48 hours):
{history_json}

Analyze this history and return ONLY a valid JSON object (no markdown, no explanation) with exactly these keys:

{{
  "routines": [
    {{
      "name": "Morning routine",
      "typical_time": "07:30",
      "days": ["Mon","Tue","Wed","Thu","Fri"],
      "sequence": [{{"entity_id": "switch.coffee_maker", "action": "turn_on", "offset_min": 0}}],
      "confidence": 0.85
    }}
  ],
  "correlations": [
    {{
      "entity_a": "switch.coffee_maker",
      "entity_b": "light.kitchen",
      "pattern": "kitchen light turns on within 5 minutes of coffee maker",
      "confidence": 0.9
    }}
  ],
  "right_now": [
    {{
      "insight": "User usually turns on living room TV at this time on Fridays",
      "entity_id": "media_player.tv",
      "urgency": "high"
    }}
  ],
  "anomalies": [
    {{
      "description": "Kitchen light on 8h, usually off by now",
      "entity_id": "light.kitchen"
    }}
  ]
}}

Rules:
- Only include patterns with strong evidence (3+ occurrences)
- "right_now" should reflect what typically happens at {current_time} on {day_of_week}
- "anomalies" are entities in unusual states compared to historical norm
- Use exact entity_ids from the history data
- Return empty arrays if no confident patterns found
- Be concise — max 5 items per category"""

        _LOGGER.info("Pattern analysis: analyzing %d entities over 48h", len(compact))
        try:
            raw = await self._ollama.generate_analysis(prompt)
            if not raw:
                return {}
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            parsed = json.loads(clean.strip())
            if not isinstance(parsed, dict):
                return {}
            # Normalise — ensure all keys exist
            return {
                "routines": parsed.get("routines", []),
                "correlations": parsed.get("correlations", []),
                "right_now": parsed.get("right_now", []),
                "anomalies": parsed.get("anomalies", []),
            }
        except json.JSONDecodeError as e:
            _LOGGER.warning("Pattern analysis: JSON parse error: %s | raw: %.300s", e, raw)
            return {}
        except Exception as e:
            _LOGGER.warning("Pattern analysis: unexpected error: %s", e)
            return {}

    @staticmethod
    def load_patterns() -> dict:
        try:
            with open(_PATTERNS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            _LOGGER.warning("Could not load patterns file: %s", e)
        return {}

    @staticmethod
    def save_patterns(patterns: dict) -> None:
        try:
            with open(_PATTERNS_FILE, "w") as f:
                json.dump(patterns, f, indent=2)
        except Exception as e:
            _LOGGER.error("Could not save patterns: %s", e)
