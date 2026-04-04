"""Home Assistant REST client — direct REST connection."""
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
_DEFAULT_BASE = "http://supervisor/core/api"

POLL_INTERVAL = 30          # seconds between state polls (keeps _states fresh)
REFRESH_INTERVAL = 600      # seconds between Ollama refresh cycles (default 10 min)


class HAClient:
    """Polls HA states via REST and fetches history."""

    def __init__(
        self,
        on_states_ready: Callable,
        refresh_interval_seconds: int = REFRESH_INTERVAL,
        ha_url: str = "",
        ha_token: str = "",
    ) -> None:
        self._on_states_ready = on_states_ready
        self._refresh_interval = refresh_interval_seconds
        # Prefer explicit ha_url/ha_token; fall back to Supervisor proxy
        if ha_url and ha_token:
            self._base = ha_url.rstrip("/") + "/api"
            self._token = ha_token
            _LOGGER.info("HAClient: using direct HA URL %s", self._base)
        else:
            self._base = _DEFAULT_BASE
            self._token = SUPERVISOR_TOKEN
            _LOGGER.info("HAClient: using Supervisor proxy")
        self._session: aiohttp.ClientSession | None = None
        self._states: dict = {}
        self._running = False
        self._last_refresh: float = 0.0

    async def start(self) -> None:
        if not self._token:
            _LOGGER.error("No auth token available — set ha_token option or check homeassistant_api permission")
        else:
            _LOGGER.info("Auth token present (%d chars)", len(self._token))

        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"}
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
                f"{self._base}/states",
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

        from const import _ACTION_DOMAINS, _CONTEXT_ONLY_DOMAINS
        start = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Only fetch history for actionable + context domains (not all 3000+ entities)
        relevant_domains = _ACTION_DOMAINS | _CONTEXT_ONLY_DOMAINS
        all_ids = [eid for eid in self._states if eid.split(".")[0] in relevant_domains]
        # Batch into chunks of 50 to stay safely under URL length limits
        chunk_size = 50
        chunks = [all_ids[i:i + chunk_size] for i in range(0, len(all_ids), chunk_size)]

        _LOGGER.info(
            "fetch_history: %d entities, %d chunks, start=%s",
            len(all_ids), len(chunks), start,
        )
        result: dict[str, list] = {}
        for chunk_idx, chunk in enumerate(chunks):
            # Build URL with literal commas — urlencode would encode them as %2C,
            # causing HA to treat the entire list as one unknown entity ID.
            entity_ids_str = ",".join(chunk)
            # Use bare flags (no =1) for HA compatibility; use minimal_response
            # instead of no_attributes for broader version support
            url = yarl.URL(
                f"{self._base}/history/period/{start}"
                f"?filter_entity_id={entity_ids_str}"
                f"&minimal_response&significant_changes_only",
                encoded=True,
            )
            if chunk_idx == 0:
                _LOGGER.info("History URL (chunk 0): %s", str(url)[:500])
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        _LOGGER.warning("History API returned HTTP %s — body: %.500s",
                                        resp.status, body)
                        continue
                    data = await resp.json()
                    if chunk_idx < 3:
                        _LOGGER.info(
                            "History chunk %d: got %d groups; first group sample: %s",
                            chunk_idx, len(data),
                            (str(data[0][0])[:200] if data and data[0] else "EMPTY"),
                        )
                    for entity_history in data:
                        if entity_history:
                            eid = entity_history[0].get("entity_id")
                            if eid:
                                result[eid] = entity_history
            except Exception as e:
                _LOGGER.warning("Failed to fetch history chunk %d: %s", chunk_idx, e)

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
                f"{self._base}/states/smart_suggestions.suggestions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("Failed to write HA state: HTTP %s", resp.status)
        except Exception as e:
            _LOGGER.warning("Error writing HA state: %s", e)

    async def send_notification(self, title: str, message: str, notification_id: str = "smart_suggestions") -> None:
        """Create a HA persistent notification (shows in the bell icon)."""
        if not self._session:
            return
        try:
            async with self._session.post(
                f"{self._base}/services/persistent_notification/create",
                json={"title": title, "message": message, "notification_id": notification_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("Failed to send notification: HTTP %s", resp.status)
        except Exception as e:
            _LOGGER.warning("Error sending notification: %s", e)

    async def _api_get(self, path: str) -> list:
        """Thin REST GET helper returning parsed JSON list."""
        if not self._session:
            raise RuntimeError("HAClient: no active session — call start() first")
        url = self._base.rstrip("/") + path
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_automations(self) -> list[str]:
        """Return list of existing automation friendly names from HA states."""
        try:
            states = await self._api_get("/states")
            return [
                s.get("attributes", {}).get("friendly_name", s["entity_id"])
                for s in states
                if s.get("entity_id", "").startswith("automation.")
            ]
        except Exception as e:
            _LOGGER.warning("get_automations failed: %s", e)
            return []

    async def create_automation(self, config_dict: dict) -> dict:
        """Create a new HA automation via REST. Returns {success, automation_id} or {success, error}."""
        if not self._session:
            return {"success": False, "error": "No active session"}
        try:
            async with self._session.post(
                f"{self._base}/config/automation/config",
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
