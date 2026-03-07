"""Streaming Ollama client."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

_OPTIONS_FILE = "/data/options.json"


def _load_options() -> dict:
    try:
        with open(_OPTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


_opts = _load_options()
_raw_url = _opts.get("ollama_url", "http://homeassistant.local:11434").strip()
OLLAMA_URL = _raw_url if _raw_url.startswith(("http://", "https://")) else f"http://{_raw_url}"
OLLAMA_MODEL = _opts.get("ollama_model", "llama3.2")


class OllamaClient:
    """Calls Ollama with streaming and yields tokens."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    async def stream_generate(
        self, prompt: str, on_token: Callable[[str], None]
    ) -> str:
        """Stream tokens from Ollama. Calls on_token for each chunk.
        Returns the full assembled response string."""
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": True,
            "format": "json",
            "options": {"temperature": 0.3, "num_predict": 2048},
        }
        full_response = ""
        last_error: Exception | None = None

        for attempt in range(2):
            full_response = ""
            try:
                async with self._session.post(
                    f"{OLLAMA_URL}/api/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 404:
                        raise RuntimeError(
                            f"Ollama model '{OLLAMA_MODEL}' not found (HTTP 404). "
                            f"Run: ollama pull {OLLAMA_MODEL}"
                        )
                    if resp.status != 200:
                        raise RuntimeError(f"Ollama HTTP {resp.status}")
                    async for line in resp.content:
                        line = line.strip()
                        if not line:
                            continue
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        if token:
                            full_response += token
                            on_token(token)
                        if chunk.get("done"):
                            break
                return full_response
            except Exception as e:
                last_error = e
                if attempt == 0:
                    _LOGGER.warning("Ollama stream attempt 1 failed: %s — retrying", e)
                    await asyncio.sleep(2)

        _LOGGER.error("Ollama stream failed after 2 attempts: %s", last_error)
        return full_response

    def parse_suggestions(self, raw: str, max_suggestions: int) -> list:
        """Parse the JSON suggestions array from Ollama output."""
        clean = raw.strip()
        # Strip markdown code fences if present
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, list):
                return parsed[:max_suggestions]
            if isinstance(parsed, dict):
                candidates = parsed.get("suggestions") or parsed.get("actions") or []
                return candidates[:max_suggestions]
        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse Ollama JSON: %s\nRaw: %.500s", e, raw)
        return []
