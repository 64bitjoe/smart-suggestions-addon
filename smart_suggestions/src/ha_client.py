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

POLL_INTERVAL = 30  # seconds between state polls


class HAClient:
    """Polls HA states via REST and fetches history."""

    def __init__(self, on_states_ready: Callable) -> None:
        self._on_states_ready = on_states_ready
        self._session: aiohttp.ClientSession | None = None
        self._states: dict = {}
        self._running = False

    async def start(self) -> None:
        if not SUPERVISOR_TOKEN:
            _LOGGER.error("SUPERVISOR_TOKEN not set — check homeassistant_api permission")
        else:
            _LOGGER.info("SUPERVISOR_TOKEN present (%d chars)", len(SUPERVISOR_TOKEN))

        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        )
        self._running = True

        # Initial fetch then poll loop
        while self._running:
            await self._fetch_states()
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def _fetch_states(self) -> None:
        """Fetch all current entity states from HA REST API."""
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
                _LOGGER.info("Fetched %d entity states", len(self._states))
                await self._on_states_ready(self._states)
        except Exception as e:
            _LOGGER.error("Failed to fetch states: %s", e)

    async def fetch_history(self, hours: int) -> dict[str, list]:
        """Fetch entity history for the last N hours."""
        if not self._session:
            return {}
        start = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
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
