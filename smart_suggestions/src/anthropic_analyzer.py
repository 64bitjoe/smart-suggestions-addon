"""Nightly deep pattern analysis via Anthropic (or OpenAI-compatible) API."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from const import _ACTION_DOMAINS

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
    """Return compact summary — only actionable entities with state changes."""
    out = {}
    for eid, entries in history.items():
        domain = eid.split(".")[0]
        if domain not in _ACTION_DOMAINS:
            continue
        if not entries:
            continue
        # Scenes/scripts/automations are always "scening"/"idle" — treat any entries as changes
        if domain not in {"scene", "script", "automation"}:
            states_seen = {e.get("state") for e in entries}
            if len(states_seen) < 2:
                continue
        summary = _summarise_history(entries)
        if summary:
            name = states.get(eid, {}).get("attributes", {}).get("friendly_name", eid)
            out[eid] = {"name": name, "history": summary}
    return out


def _build_prompt(compact: dict, now: datetime) -> str:
    history_json = json.dumps(compact, indent=2)
    day_of_week = now.strftime("%A")
    current_time = now.strftime("%H:%M")
    return f"""Analyze this smart home entity history and extract behavioral patterns.

CURRENT TIME: {current_time} on {day_of_week}

ENTITY HISTORY (state transitions):
{history_json}

Return ONLY a valid JSON object (no markdown, no explanation):

{{
  "routines": [
    {{
      "name": "string",
      "entity_id": "exact_entity_id_from_history",
      "typical_time": "HH:MM",
      "days": ["Mon","Tue","Wed","Thu","Fri"],
      "confidence": 0.0
    }}
  ],
  "correlations": [
    {{
      "entity_a": "exact_entity_id",
      "entity_b": "exact_entity_id",
      "pattern": "one sentence description",
      "confidence": 0.0,
      "window_minutes": 5
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
- Only patterns with 3+ occurrences
- Use exact entity_ids from the history data
- Return empty arrays if no confident patterns found
- Max 20 items per category
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
        prompt = _build_prompt(compact, now)
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
            return message.content[0].text
        else:
            # OpenAI-compatible
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
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
