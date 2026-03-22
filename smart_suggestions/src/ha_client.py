"""Home Assistant REST client — polls states via Supervisor proxy."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Callable
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
            # Build URL with literal commas — urlencode would encode them as %2C,
            # causing HA to treat the entire list as one unknown entity ID.
            entity_ids_str = ",".join(chunk)
            url = yarl.URL(
                f"{HA_REST_BASE}/history/period/{start}"
                f"?filter_entity_id={entity_ids_str}&significant_changes_only=1&no_attributes=1",
                encoded=True,
            )
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

    async def create_automation(self, config_dict: dict) -> dict:
        """Create a new HA automation via REST. Returns {success, automation_id} or {success, error}."""
        if not self._session:
            return {"success": False, "error": "No active session"}
        try:
            async with self._session.post(
                f"{HA_REST_BASE}/config/automation/config",
                json={"config": config_dict},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return {"success": True, "automation_id": data.get("automation_id", "")}
                else:
                    text = await resp.text()
                    _LOGGER.warning("create_automation HTTP %s: %s", resp.status, text)
                    return {"success": False, "error": f"HTTP {resp.status}: {text}"}
        except Exception as e:
            _LOGGER.error("create_automation failed: %s", e)
            return {"success": False, "error": str(e)}
