"""Constrained Ollama wrapper — rewrites 'reason' fields only. No ranking."""
from __future__ import annotations

import json
import logging
from datetime import datetime

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

    async def narrate(self, candidates: list[dict], **kwargs) -> list[dict]:
        """Rewrite 'reason' fields for all candidates. Returns candidates unchanged on any failure."""
        if not candidates:
            return []
        try:
            raw = await self._call_ollama(candidates)
            return self._apply_reasons(candidates, raw)
        except Exception as e:
            _LOGGER.warning("OllamaNarrator: failed, using original reasons: %s", e)
            return candidates

    async def _call_ollama(self, candidates: list[dict]) -> str:
        now_str = datetime.now().strftime("%H:%M on %A")
        input_json = json.dumps([
            {"entity_id": c["entity_id"], "name": c["name"], "type": c.get("type"), "reason": c.get("reason", "")}
            for c in candidates
        ], indent=2)
        prompt = f"""It is {now_str}. Rewrite the 'reason' field for each smart home suggestion to be natural and specific. Keep it to one sentence.

SUGGESTIONS:
{input_json}

Return ONLY a valid JSON array (no markdown):
[{{"entity_id": "...", "reason": "..."}}]

One object per input item, in the same order. Do not add or remove items."""

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
        """Apply narrated reasons. Falls back to original for any missing/failed items."""
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:].strip()
            narrated = json.loads(clean.strip())
            if not isinstance(narrated, list):
                return candidates
            narrated_by_eid = {item["entity_id"]: item["reason"] for item in narrated if "entity_id" in item and "reason" in item}
            result = []
            for c in candidates:
                updated = dict(c)
                if c["entity_id"] in narrated_by_eid:
                    updated["reason"] = narrated_by_eid[c["entity_id"]]
                result.append(updated)
            return result
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            _LOGGER.warning("OllamaNarrator: could not parse response: %s", e)
            return candidates
