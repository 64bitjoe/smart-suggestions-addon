"""Ollama wrapper — contextually reranks candidates and rewrites reason fields."""
from __future__ import annotations

import json
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class OllamaNarrator:
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
        """Rewrite 'reason' fields for all candidates. Returns candidates unchanged on any failure."""
        if not candidates:
            return []
        try:
            raw = await self._call_ollama(candidates, context=context)
            return self._apply_reasons(candidates, raw)
        except Exception as e:
            _LOGGER.warning("OllamaNarrator: failed, using original reasons: %s", e)
            return candidates

    async def _call_ollama(self, candidates: list[dict], context: dict | None = None) -> str:
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
            {"entity_id": c["entity_id"], "name": c["name"], "type": c.get("type"), "reason": c.get("reason", "")}
            for c in candidates
        ], indent=2)

        # Build compact entity state context grouped by area
        entity_context_lines = []
        if context and context.get("recent_changes"):
            entity_context_lines.append("RECENTLY CHANGED:")
            for rc in context["recent_changes"][:15]:
                entity_context_lines.append(f"  {rc['entity_id']}: {rc.get('state', '?')} ({rc.get('changed_ago_minutes', '?')}m ago)")

        entity_context = "\n".join(entity_context_lines) if entity_context_lines else ""

        prompt = f"""You are a smart home assistant. Given the current context, rank these suggestions by how useful they are RIGHT NOW.

TIME: {now_str}

{context_block}

{entity_context}

CANDIDATE SUGGESTIONS (reorder by relevance, rewrite reasons to be specific and helpful):
{input_json}

Instructions:
1. Put the most useful-right-now suggestions first. Consider time of day, what's currently on/off, weather, and occupancy.
2. Rewrite each 'reason' to be conversational and specific — explain WHY this action makes sense right now (one sentence).
3. Prioritize diversity: mix different entity types (lights, climate, locks, media, scenes) rather than clustering one type.
4. Do NOT suggest anything in "Already automated" or "User has dismissed".
5. Return ONLY a valid JSON array (no markdown):
[{{"entity_id": "...", "reason": "..."}}]"""

        session = self._session
        if session:
            return await self._post(session, prompt)
        async with aiohttp.ClientSession() as tmp_session:
            return await self._post(tmp_session, prompt)

    async def _post(self, session: aiohttp.ClientSession, prompt: str) -> str:
        async with session.post(
            f"{self._url}/api/generate",
            json={"model": self._model, "prompt": prompt, "stream": False},
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Ollama returned HTTP {resp.status}")
            data = await resp.json()
            return data.get("response", "")

    def _apply_reasons(self, candidates: list[dict], raw: str) -> list[dict]:
        """Apply narrated reasons and reranking. Falls back to original on any failure."""
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return candidates

            # Build lookup from original candidates
            by_eid = {c["entity_id"]: c for c in candidates}
            seen = set()
            result = []

            # Follow LLM's order
            for item in parsed:
                eid = item.get("entity_id")
                if not eid or eid not in by_eid or eid in seen:
                    continue
                candidate = dict(by_eid[eid])
                if item.get("reason"):
                    candidate["reason"] = item["reason"]
                result.append(candidate)
                seen.add(eid)

            # Append any candidates the LLM dropped
            for c in candidates:
                if c["entity_id"] not in seen:
                    result.append(c)

            return result if result else candidates
        except (json.JSONDecodeError, TypeError):
            return candidates
