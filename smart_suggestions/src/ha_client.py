"""Home Assistant REST client — direct REST connection."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Callable
import aiohttp

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

    def get_states(self) -> dict:
        """Current entity states as {entity_id: state_dict}. Kept fresh by the poller."""
        return self._states

    async def push_sensor(self, entity_id: str, state: str, attributes: dict) -> None:
        if not self._session:
            return
        try:
            async with self._session.post(
                f"{self._base}/states/{entity_id}",
                json={"state": state, "attributes": attributes},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("push_sensor %s: HTTP %s", entity_id, resp.status)
        except Exception as e:
            _LOGGER.warning("push_sensor %s failed: %s", entity_id, e)

    async def call_service(self, domain: str, service: str, entity_id: str) -> bool:
        if not self._session:
            return False
        try:
            async with self._session.post(
                f"{self._base}/services/{domain}/{service}",
                json={"entity_id": entity_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            _LOGGER.warning("call_service %s.%s failed: %s", domain, service, e)
            return False

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

    async def get_current_on_states(self) -> dict[str, tuple[str, datetime]]:
        """Return {entity_id: (state, last_changed_dt)} for entities currently in an active state.

        Active states: on, heat, cool, heat_cool, fan_only, auto.
        Reads from the cached self._states dict maintained by the 30-second poller.
        """
        active = {"on", "heat", "cool", "heat_cool", "fan_only", "auto"}
        out: dict[str, tuple[str, datetime]] = {}
        for entity_id, s in self._states.items():
            if s.get("state") in active:
                last = s.get("last_changed")
                if last:
                    out[entity_id] = (
                        s["state"],
                        datetime.fromisoformat(last.replace("Z", "+00:00")),
                    )
        return out

    async def get_automated_entities(self) -> set[str]:
        """Return set of entity_ids that appear as targets in any active automation."""
        out: set[str] = set()
        try:
            config_resp = await self._api_get("/config/automation/config")
        except Exception:
            return out  # endpoint may not be available; be tolerant
        if isinstance(config_resp, list):
            for auto in config_resp:
                for action in auto.get("action") or []:
                    target = action.get("target") or {}
                    eid = target.get("entity_id")
                    if isinstance(eid, str):
                        out.add(eid)
                    elif isinstance(eid, list):
                        out.update(eid)
        return out

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
