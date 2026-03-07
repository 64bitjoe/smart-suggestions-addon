"""Home Assistant REST client — polls states via Supervisor proxy."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlencode

import aiohttp
import yarl

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
        """Fetch entity history via HA REST API, batched to avoid URL length limits."""
        if not self._session or not self._states:
            return {}

        start = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_ids = list(self._states.keys())
        # Batch into chunks of 100 to stay well under URL length limits
        chunk_size = 100
        chunks = [all_ids[i:i + chunk_size] for i in range(0, len(all_ids), chunk_size)]

        result: dict[str, list] = {}
        for chunk in chunks:
            qs = urlencode({
                "filter_entity_id": ",".join(chunk),
                "significant_changes_only": "1",
                "minimal_response": "1",
                "no_attributes": "1",
            })
            url = yarl.URL(f"{HA_REST_BASE}/history/period/{start}?{qs}", encoded=True)
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("History API returned HTTP %s — body: %s",
                                        resp.status, await resp.text())
                        continue
                    data = await resp.json()
                    for entity_history in data:
                        if entity_history:
                            eid = entity_history[0].get("entity_id")
                            if eid:
                                result[eid] = entity_history
            except Exception as e:
                _LOGGER.warning("Failed to fetch history chunk: %s", e)

        _LOGGER.info("Fetched history for %d entities", len(result))
        return result

    async def fetch_dow_history(self, hour_window: int = 2, weeks_back: int = 4) -> dict[str, list]:
        """Fetch history for the same day-of-week at ±hour_window of current time, past weeks_back weeks."""
        if not self._session or not self._states:
            return {}
        from context_builder import _ACTION_DOMAINS
        action_ids = [eid for eid in self._states if eid.split(".")[0] in _ACTION_DOMAINS]
        if not action_ids:
            return {}
        now = datetime.utcnow()
        result: dict[str, list] = {}
        for week in range(1, weeks_back + 1):
            target = now - timedelta(weeks=week)
            start_dt = target - timedelta(hours=hour_window)
            end_dt = target + timedelta(hours=hour_window)
            start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            for i in range(0, len(action_ids), 100):
                chunk = action_ids[i:i + 100]
                qs = urlencode({
                    "filter_entity_id": ",".join(chunk),
                    "end_time": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "minimal_response": "1",
                    "no_attributes": "1",
                })
                url = yarl.URL(f"{HA_REST_BASE}/history/period/{start}?{qs}", encoded=True)
                try:
                    async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for entity_history in data:
                            if entity_history:
                                eid = entity_history[0].get("entity_id")
                                if eid:
                                    result.setdefault(eid, []).extend(entity_history)
                except Exception as e:
                    _LOGGER.warning("DOW history fetch failed (week -%d): %s", week, e)
        _LOGGER.info("Fetched DOW history for %d entities", len(result))
        return result

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
