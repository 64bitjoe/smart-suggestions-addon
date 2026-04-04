"""Narrator — contextually reranks candidates and rewrites reason fields.

Supports two backends:
  - OllamaNarrator: local Ollama instance
  - AINarrator: Anthropic Claude or OpenAI-compatible API
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _build_prompt(candidates: list[dict], context: dict | None = None) -> str:
    """Build the shared narration prompt used by both backends."""
    from datetime import datetime
    now_str = context.get("current_time") if context else datetime.now().strftime("%H:%M on %A")

    context_lines = []
    if context:
        if context.get("motion_sensors"):
            no_motion = [s for s in context["motion_sensors"] if s.get("state") == "off"]
            if no_motion:
                context_lines.append("No motion in: " + ", ".join(
                    f"{s['entity_id']} ({s.get('minutes_since_triggered', '?')}m)"
                    for s in no_motion[:5]
                ))
        if context.get("presence"):
            context_lines.append("Home: " + ", ".join(context["presence"]))
        if context.get("weather"):
            w = context["weather"]
            context_lines.append(f"Weather: {w.get('condition', '')} {w.get('temperature', '')}°")
        if context.get("existing_automations"):
            context_lines.append("Already automated: " + "; ".join(context["existing_automations"][:10]))
        if context.get("avoided_pairs"):
            context_lines.append("User has dismissed: " + "; ".join(
                f"{p['entity_id']} {p['action']}" for p in context["avoided_pairs"][:5]
            ))

    context_block = "\n".join(context_lines) if context_lines else "No additional context."

    input_json = json.dumps([
        {
            "entity_id": c["entity_id"],
            "name": c["name"],
            "type": c.get("type"),
            "current_state": c.get("current_state", ""),
            "action": c.get("action", ""),
            "reason": c.get("reason", ""),
        }
        for c in candidates
    ], indent=2)

    entity_context_lines = []
    if context and context.get("recent_changes"):
        entity_context_lines.append("RECENTLY CHANGED:")
        for rc in context["recent_changes"][:15]:
            entity_context_lines.append(f"  {rc['entity_id']}: {rc.get('state', '?')} ({rc.get('changed_ago_minutes', '?')}m ago)")

    entity_context = "\n".join(entity_context_lines) if entity_context_lines else ""

    return f"""You are a smart home advisor. Your job is to identify actionable, non-obvious suggestions — things that are connected but NOT currently automated.

TIME: {now_str}

{context_block}

{entity_context}

CANDIDATES (each has current_state and proposed action):
{input_json}

Instructions:
1. RANK by how actionable and non-obvious each suggestion is RIGHT NOW. Prioritize:
   - Devices that seem forgotten or wasteful (lights left on, locks unlocked, covers open in bad weather)
   - Things that work together but aren't automated (e.g. "living room lights are on but TV is off — if you're done watching, turn off the lights too")
   - Anomalies: something is in an unusual state for this time of day
   - Correlations: entity A changed recently, entity B usually follows but hasn't yet
2. DE-PRIORITIZE generic time-of-day suggestions ("it's evening, adjust lights"). Only mention time if it reveals something specific and actionable.
3. Rewrite each 'reason' as a SHORT, specific sentence explaining the concrete situation:
   - BAD: "It's evening — good time to adjust lighting"
   - GOOD: "Kitchen lights have been on for 6 hours with no motion since 2pm"
   - BAD: "Consider adjusting climate"
   - GOOD: "AC is cooling to 68° but it's only 52° outside — open windows instead?"
4. Do NOT suggest anything in "Already automated" or "User has dismissed".
5. Return ONLY a valid JSON array (no markdown):
[{{"entity_id": "...", "reason": "..."}}]"""


def _apply_reasons(candidates: list[dict], raw: str) -> list[dict]:
    """Apply narrated reasons and reranking. Falls back to original on any failure."""
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return candidates

        by_eid = {c["entity_id"]: c for c in candidates}
        seen = set()
        result = []

        for item in parsed:
            eid = item.get("entity_id")
            if not eid or eid not in by_eid or eid in seen:
                continue
            candidate = dict(by_eid[eid])
            if item.get("reason"):
                candidate["reason"] = item["reason"]
            result.append(candidate)
            seen.add(eid)

        for c in candidates:
            if c["entity_id"] not in seen:
                result.append(c)

        return result if result else candidates
    except (json.JSONDecodeError, TypeError):
        return candidates


class OllamaNarrator:
    """Narrate suggestions via a local Ollama instance."""

    def __init__(self, ollama_url: str, model: str) -> None:
        self._url = ollama_url.rstrip("/")
        self._model = model
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def narrate(self, candidates: list[dict], context: dict | None = None) -> list[dict]:
        if not candidates:
            return []
        try:
            prompt = _build_prompt(candidates, context)
            raw = await self._post(prompt)
            return _apply_reasons(candidates, raw)
        except Exception as e:
            _LOGGER.warning("OllamaNarrator: failed, using original reasons: %s", e)
            return candidates

    async def _post(self, prompt: str) -> str:
        session = self._session
        if not session:
            session = aiohttp.ClientSession()
        try:
            async with session.post(
                f"{self._url}/api/generate",
                json={"model": self._model, "prompt": prompt, "stream": False},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Ollama returned HTTP {resp.status}")
                data = await resp.json()
                return data.get("response", "")
        finally:
            if not self._session:
                await session.close()


class AINarrator:
    """Narrate suggestions via Anthropic Claude or OpenAI-compatible API."""

    def __init__(self, ai_provider: str, ai_api_key: str, ai_model: str, ai_base_url: str = "") -> None:
        self._provider = ai_provider
        self._model = ai_model
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
                _LOGGER.warning("AINarrator: unknown AI provider: %s", provider)
        except ImportError as e:
            _LOGGER.error("AINarrator: could not import SDK for %s: %s", provider, e)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def narrate(self, candidates: list[dict], context: dict | None = None) -> list[dict]:
        if not candidates:
            return []
        if not self._client:
            _LOGGER.warning("AINarrator: no AI client configured — using original reasons")
            return candidates
        try:
            prompt = _build_prompt(candidates, context)
            raw = await asyncio.get_running_loop().run_in_executor(None, self._call_api, prompt)
            return _apply_reasons(candidates, raw)
        except Exception as e:
            _LOGGER.warning("AINarrator: failed, using original reasons: %s", e)
            return candidates

    def _call_api(self, prompt: str) -> str:
        if self._provider == "anthropic":
            message = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        else:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
            )
            return response.choices[0].message.content
