"""Home Assistant REST client — polls states via Supervisor proxy."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_REST_BASE = "http://supervisor/core/api"

POLL_INTERVAL = 30          # seconds between state polls (keeps _states fresh)
REFRESH_INTERVAL = 600      # seconds between Ollama refresh cycles (default 10 min)


class HAClient:
    """Polls HA states via REST and fetches history."""

    def __init__(self, on_states_ready: Callable, refresh_interval_seconds: int = REFRESH_INTERVAL) -> None:
        self._on_states_ready = on_states_ready
        self._refresh_interval = refresh_interval_seconds
        self._session: aiohttp.ClientSession | None = None
        self._states: dict = {}
        self._running = False
        self._last_refresh: float = 0.0

    async def start(self) -> None:
        if not SUPERVISOR_TOKEN:
            _LOGGER.error("SUPERVISOR_TOKEN not set — check homeassistant_api permission")
        else:
            _LOGGER.info("SUPERVISOR_TOKEN present (%d chars)", len(SUPERVISOR_TOKEN))

        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        )
        self._running = True

        # Poll states frequently to keep _states fresh; only trigger Ollama
        # refresh every refresh_interval seconds.
        while self._running:
            await self._fetch_states()
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def _fetch_states(self) -> None:
        """Fetch all current entity states and trigger Ollama refresh if due."""
        import time
        try:
            async with self._session.get(
                f"{HA_REST_BASE}/states",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("GET /states returned HTTP %s", resp.status)
                    return
                states_list = await resp.json()
                self._states = {s["entity_id"]: s for s in states_list}
                _LOGGER.debug("Fetched %d entity states", len(self._states))
        except Exception as e:
            _LOGGER.error("Failed to fetch states: %s", e)
            return

        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_interval:
            self._last_refresh = now
            _LOGGER.info("Triggering refresh cycle (%d states)", len(self._states))
            await self._on_states_ready(self._states)

    async def fetch_history(self, hours: int) -> dict[str, list]:
        """Fetch entity history for the last N hours."""
        if not self._session:
            return {}
        start = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        try:
            async with self._session.get(
                f"{HA_REST_BASE}/history/period/{start}",
                params={"significant_changes_only": "true"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("History API returned HTTP %s", resp.status)
                    return {}
                data = await resp.json()
                result = {}
                for entity_history in data:
                    if entity_history:
                        eid = entity_history[0].get("entity_id")
                        if eid:
                            result[eid] = entity_history
                return result
        except Exception as e:
            _LOGGER.warning("Failed to fetch history: %s", e)
            return {}

    async def write_suggestions_state(self, suggestions: list) -> None:
        """Write final suggestions to HA state via REST."""
        if not self._session:
            return
        payload = {
            "state": "ready",
            "attributes": {
                "suggestions": suggestions,
                "last_updated": datetime.now().isoformat(),
                "friendly_name": "Smart Suggestions",
                "count": len(suggestions),
            },
        }
        try:
            async with self._session.post(
                f"{HA_REST_BASE}/states/smart_suggestions.suggestions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("Failed to write HA state: HTTP %s", resp.status)
        except Exception as e:
            _LOGGER.warning("Error writing HA state: %s", e)
