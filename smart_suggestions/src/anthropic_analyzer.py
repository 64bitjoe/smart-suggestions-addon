"""Nightly deep pattern analysis via Anthropic (or OpenAI-compatible) API."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from const import _ACTION_DOMAINS, _INACTIVE_STATES

_LOGGER = logging.getLogger(__name__)

_MAX_HISTORY_ENTRIES = 8


def _summarise_history(entries: list) -> str:
    """Compact state transition string."""
    if not entries:
        return ""
    deduped = [entries[0]]
    for e in entries[1:]:
        if e.get("state") != deduped[-1].get("state"):
            deduped.append(e)
    deduped = deduped[-_MAX_HISTORY_ENTRIES:]
    parts = []
    for e in deduped:
        ts = e.get("last_changed", "")[:16].replace("T", " ")
        parts.append(f"{e.get('state', '?')} at {ts}")
    return " → ".join(parts)


def _compact_history(history: dict, states: dict) -> dict:
    """Return compact summary — actionable entities with state changes or long-duration anomalies."""
    out = {}
    for eid, entries in history.items():
        domain = eid.split(".")[0]
        if domain not in _ACTION_DOMAINS:
            continue
        if not entries:
            continue
        name = states.get(eid, {}).get("attributes", {}).get("friendly_name", eid)
        # Scenes/scripts/automations are always "scening"/"idle" — treat any entries as changes
        if domain not in {"scene", "script", "automation"}:
            states_seen = {e.get("state") for e in entries}
            if len(states_seen) < 2:
                # Check for long-duration anomaly: entity stuck in active state
                current_state = states.get(eid, {}).get("state", "")
                if current_state in _INACTIVE_STATES or current_state in ("unavailable", "unknown", ""):
                    continue
                # Calculate how long entity has been in current state
                last_changed_str = states.get(eid, {}).get("last_changed", "")
                if not last_changed_str:
                    continue
                try:
                    from datetime import datetime, timezone
                    last_changed = datetime.fromisoformat(last_changed_str.replace("Z", "+00:00"))
                    hours_active = (datetime.now(timezone.utc) - last_changed).total_seconds() / 3600
                except (ValueError, TypeError):
                    continue
                # Include if active for unusually long
                threshold = 6 if domain in ("light", "switch", "fan") else 4 if domain == "lock" else 8
                if hours_active < threshold:
                    continue
                out[eid] = {"name": name, "history": f"{current_state} continuously for {int(hours_active)}h [ANOMALY: unusually long duration]"}
                continue
        summary = _summarise_history(entries)
        if summary:
            out[eid] = {"name": name, "history": summary}
    return out


def _build_prompt(compact: dict, states: dict, now: datetime) -> str:
    # Group entities by area
    by_area: dict[str, list[str]] = {}
    for eid, info in compact.items():
        area = states.get(eid, {}).get("attributes", {}).get("area_id", "")
        if not area:
            # Try to infer area from friendly name
            name = info.get("name", "")
            area = name.split(" ")[0] if " " in name else "Other"
        by_area.setdefault(area, []).append(f"- {eid} ({info['name']}): {info['history']}")

    history_text = ""
    for area, lines in sorted(by_area.items()):
        history_text += f"\n## {area}\n" + "\n".join(lines) + "\n"

    # Current state snapshot
    state_lines = []
    for eid, s in states.items():
        domain = eid.split(".")[0]
        if domain not in _ACTION_DOMAINS:
            continue
        name = s.get("attributes", {}).get("friendly_name", eid)
        state_val = s.get("state", "?")
        if state_val not in ("unavailable", "unknown"):
            state_lines.append(f"  {eid}: {state_val}")
    state_snapshot = "\n".join(state_lines[:150])

    day_of_week = now.strftime("%A")
    current_time = now.strftime("%H:%M")
    return f"""Analyze this smart home entity history and extract behavioral patterns.

CURRENT TIME: {current_time} on {day_of_week}

ENTITY HISTORY (state transitions, grouped by area):
{history_text}

CURRENT STATE SNAPSHOT:
{state_snapshot}

Return ONLY a valid JSON object (no markdown, no explanation):

{{
  "routines": [
    {{
      "name": "string",
      "entity_id": "exact_entity_id_from_history",
      "typical_time": "HH:MM",
      "days": ["Mon","Tue","Wed","Thu","Fri"],
      "confidence": 0.0,
      "instances": 3
    }}
  ],
  "correlations": [
    {{
      "entity_a": "exact_entity_id",
      "entity_b": "exact_entity_id",
      "pattern": "one sentence description",
      "confidence": 0.0,
      "window_minutes": 15,
      "instances": 3
    }}
  ],
  "anomalies": [
    {{
      "entity_id": "exact_entity_id",
      "description": "one sentence",
      "severity": "low|medium|high"
    }}
  ]
}}

Rules:
- Look for patterns across ALL entity domains (lights, switches, climate, locks, media, covers, fans, automations, scripts, scenes)
- Include time-based routines for ANY entity type, not just scenes
- Flag "energy waste" anomalies: devices left on when area appears unoccupied
- Flag "forgotten device" anomalies: entities active much longer than their historical average
- Patterns with 2+ occurrences at high confidence (0.7+) or 3+ at medium confidence (0.5+)
- Use exact entity_ids from the history data
- Return empty arrays if no confident patterns found
- Max 30 items per category
- Days use 3-letter abbreviations: Mon,Tue,Wed,Thu,Fri,Sat,Sun"""


class AnthropicAnalyzer:
    def __init__(
        self,
        ai_provider: str,
        ai_api_key: str,
        ai_model: str,
        analysis_depth_days: int = 14,
        ai_base_url: str = "",
    ) -> None:
        self._provider = ai_provider
        self._model = ai_model
        self._depth = analysis_depth_days
        self._client: Any = None
        if ai_api_key:
            self._init_client(ai_provider, ai_api_key, ai_base_url)

    def _init_client(self, provider: str, api_key: str, base_url: str = "") -> None:
        try:
            if provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=api_key)
            elif provider == "openai_compatible":
                import openai
                self._client = openai.OpenAI(api_key=api_key, base_url=base_url or None)
            else:
                _LOGGER.warning("Unknown AI provider: %s", provider)
        except ImportError as e:
            _LOGGER.error("Could not import AI SDK for provider %s: %s", provider, e)

    async def analyze(self, history: dict, states: dict) -> dict:
        """Run deep analysis. Returns structured pattern dict or empty on failure."""
        compact = _compact_history(history, states)
        if not compact:
            _LOGGER.info("AnthropicAnalyzer: no actionable history to analyze")
            return {"routines": [], "correlations": [], "anomalies": []}

        if not self._client:
            _LOGGER.warning("AnthropicAnalyzer: no AI client configured — skipping")
            return {"routines": [], "correlations": [], "anomalies": []}

        now = datetime.now(timezone.utc)
        prompt = _build_prompt(compact, states, now)
        _LOGGER.info("AnthropicAnalyzer: analyzing %d entities with %s/%s", len(compact), self._provider, self._model)

        try:
            raw = await asyncio.get_running_loop().run_in_executor(
                None, self._call_api, prompt
            )
            return self._parse(raw)
        except Exception as e:
            _LOGGER.warning("AnthropicAnalyzer: unexpected error (%s): %s", type(e).__name__, e)
            return {"routines": [], "correlations": [], "anomalies": []}

    def _call_api(self, prompt: str) -> str:
        if self._provider == "anthropic":
            message = self._client.messages.create(
                model=self._model,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text
            _LOGGER.info("Anthropic response: stop_reason=%s, len=%d", message.stop_reason, len(text))
            return text
        else:
            # OpenAI-compatible
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            return response.choices[0].message.content

    def _parse(self, raw: str) -> dict:
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.lower().startswith("json"):
                    clean = clean[4:].strip()
            parsed = json.loads(clean.strip())
            if not isinstance(parsed, dict):
                return {"routines": [], "correlations": [], "anomalies": []}
            return {
                "routines": parsed.get("routines", []),
                "correlations": parsed.get("correlations", []),
                "anomalies": parsed.get("anomalies", []),
            }
        except json.JSONDecodeError as e:
            _LOGGER.warning("AnthropicAnalyzer: JSON parse error: %s | raw: %.300s", e, raw)
            return {"routines": [], "correlations": [], "anomalies": []}
