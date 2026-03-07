"""Home Assistant WebSocket + REST client."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_WS_URL = "ws://supervisor/core/websocket"
HA_REST_BASE = "http://supervisor/core/api"

DEBOUNCE_SECONDS = 0.5


class HAClient:
    """Manages the HA WebSocket subscription and REST history calls."""

    def __init__(self, on_states_ready: Callable) -> None:
        self._on_states_ready = on_states_ready
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._msg_id = 1
        self._debounce_task: asyncio.Task | None = None
        self._states: dict = {}
        self._running = False

    def _next_id(self) -> int:
        mid = self._msg_id
        self._msg_id += 1
        return mid

    async def start(self) -> None:
        if not SUPERVISOR_TOKEN:
            _LOGGER.error("SUPERVISOR_TOKEN is not set — add-on may lack homeassistant_api permission")
        else:
            _LOGGER.info("SUPERVISOR_TOKEN present (%d chars)", len(SUPERVISOR_TOKEN))
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        )
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                _LOGGER.error("HA WebSocket error: %s — reconnecting in 10s", e)
                await asyncio.sleep(10)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _connect(self) -> None:
        _LOGGER.info("Connecting to HA WebSocket at %s", HA_WS_URL)
        async with self._session.ws_connect(HA_WS_URL) as ws:
            self._ws = ws
            # Auth handshake
            msg = await ws.receive_json()
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got: {msg}")
            await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"Auth failed: {msg}")
            _LOGGER.info("HA WebSocket authenticated")

            # Subscribe to all state changes
            sub_id = self._next_id()
            await ws.send_json({"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"})

            # Get initial states
            states_id = self._next_id()
            await ws.send_json({"id": states_id, "type": "get_states"})

            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    msg = json.loads(raw.data)
                    await self._handle_message(msg, states_id)
                elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _handle_message(self, msg: dict, states_id: int) -> None:
        mtype = msg.get("type")

        if mtype == "result" and msg.get("id") == states_id:
            # Initial states snapshot
            for state in msg.get("result", []):
                self._states[state["entity_id"]] = state
            _LOGGER.info("Loaded %d initial entity states", len(self._states))
            await self._schedule_refresh()

        elif mtype == "event":
            event = msg.get("event", {})
            if event.get("event_type") == "state_changed":
                data = event.get("data", {})
                entity_id = data.get("entity_id", "")
                new_state = data.get("new_state")
                if new_state:
                    self._states[entity_id] = new_state
                await self._schedule_refresh()

    async def _schedule_refresh(self) -> None:
        """Debounce rapid state changes into a single refresh."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_refresh())

    async def _debounced_refresh(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await self._on_states_ready(self._states)

    async def fetch_history(self, hours: int) -> dict[str, list]:
        """Fetch entity history for the last N hours via REST."""
        if not self._session:
            return {}
        start = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        url = f"{HA_REST_BASE}/history/period/{start}"
        try:
            async with self._session.get(
                url,
                params={"significant_changes_only": "true"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("History API returned %s", resp.status)
                    return {}
                data = await resp.json()
                # data is a list of lists; each inner list is history for one entity
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
        """Write final suggestions to HA state via REST API."""
        if not self._session:
            return
        url = f"{HA_REST_BASE}/states/smart_suggestions.suggestions"
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
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("Failed to write HA state: HTTP %s", resp.status)
        except Exception as e:
            _LOGGER.warning("Error writing HA state: %s", e)
