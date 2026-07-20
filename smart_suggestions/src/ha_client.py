"""Home Assistant REST client — direct REST connection."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Callable
import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _collect_entity_ids(node) -> set[str]:
    """Deep-walk any automation-config structure collecting entity_id values.

    Registry UUIDs from device actions have no '.' and are skipped."""
    out: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                values = value if isinstance(value, list) else [value]
                out.update(v for v in values if isinstance(v, str) and "." in v)
            else:
                out |= _collect_entity_ids(value)
    elif isinstance(node, list):
        for item in node:
            out |= _collect_entity_ids(item)
    return out


def _collect_device_ids(node) -> set[str]:
    """Deep-walk a config structure collecting device_id values (device
    actions reference devices, not entities — resolved separately)."""
    out: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "device_id" and isinstance(value, str) and value:
                out.add(value)
            else:
                out |= _collect_device_ids(value)
    elif isinstance(node, list):
        for item in node:
            out |= _collect_device_ids(item)
    return out
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
        """Return set of entity_ids referenced anywhere in any automation.

        HA has no "list all automation configs" endpoint, so we: GET /states to
        find automation.* entities and their config ids (attributes.id), then GET
        the config for each id individually and deep-walk it for entity_ids —
        both the legacy "action" and the 2024.10+ "actions"/"triggers" formats,
        including nested if/choose/parallel blocks. Registry UUIDs from device
        actions (no '.') are skipped. Over-collection is fine: this feeds a
        "don't re-suggest what's already automated" exclusion. Tolerant of
        partial failure — returns whatever was collected, never raises.
        """
        out: set[str] = set()
        try:
            states = await self._api_get("/states")
        except Exception as e:
            _LOGGER.warning("get_automated_entities: failed to list states: %s", e)
            return out

        automation_ids = [
            s.get("attributes", {}).get("id")
            for s in states
            if s.get("entity_id", "").startswith("automation.")
        ]

        device_ids: set[str] = set()
        for auto_id in automation_ids:
            if not auto_id:
                continue
            try:
                async with self._session.get(
                    f"{self._base}/config/automation/config/{auto_id}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue
                    config = await resp.json()
            except Exception:
                continue
            out |= _collect_entity_ids(config)
            device_ids |= _collect_device_ids(config)

        # Device actions reference devices; resolve to that device's entities
        # via the template API (registry lookups aren't REST-exposed).
        for device_id in device_ids:
            try:
                async with self._session.post(
                    f"{self._base}/template",
                    json={"template": "{{ device_entities('%s') | to_json }}" % device_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue
                    entities = json.loads(await resp.text())
                    if isinstance(entities, list):
                        out.update(e for e in entities if isinstance(e, str))
            except Exception:
                continue

        return out

    async def create_automation(self, config_dict: dict) -> dict:
        """Create a new HA automation via REST. Returns {success, automation_id} or {success, error}."""
        if not self._session:
            return {"success": False, "error": "No active session"}
        new_id = str(int(time.time() * 1000))
        try:
            async with self._session.post(
                f"{self._base}/config/automation/config/{new_id}",
                json=config_dict,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    return {"success": True, "automation_id": new_id}
                else:
                    text = await resp.text()
                    _LOGGER.warning("create_automation HTTP %s: %s", resp.status, text)
                    return {"success": False, "error": f"HTTP {resp.status}: {text}"}
        except Exception as e:
            _LOGGER.error("create_automation failed: %s", e)
            return {"success": False, "error": str(e)}

    async def get_timezone(self) -> str | None:
        """HA-configured timezone name from GET /config, or None."""
        if not self._session:
            return None
        try:
            async with self._session.get(
                f"{self._base}/config", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("time_zone")
        except Exception:
            return None
