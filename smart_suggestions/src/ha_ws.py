"""Listen for smart_suggestions_action events on the HA WebSocket API."""
from __future__ import annotations
import asyncio
import logging
import os

import aiohttp

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_WS = "ws://supervisor/core/websocket"


class HAEventListener:
    def __init__(
        self,
        on_event,
        ha_url: str = "",
        ha_token: str = "",
        event_type: str = "smart_suggestions_action",
    ):
        self._on_event = on_event
        self._event_type = event_type
        if ha_url and ha_token:
            base = ha_url.rstrip("/")
            base = base.replace("https://", "wss://").replace("http://", "ws://")
            self._url = f"{base}/api/websocket"
            self._token = ha_token
        else:
            self._url = _SUPERVISOR_WS
            self._token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        backoff = 1
        while self._running:
            try:
                await self._connect_once()
                backoff = 1
            except Exception as e:
                _LOGGER.warning("HA WS listener disconnected: %s", e)
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self._url, heartbeat=30) as ws:
                msg = await ws.receive_json()  # auth_required
                if msg.get("type") == "auth_required":
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"HA WS auth failed: {msg}")
                await ws.send_json({
                    "id": 1, "type": "subscribe_events",
                    "event_type": self._event_type,
                })
                _LOGGER.info("Listening for %s events", self._event_type)
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = raw.json()
                    if data.get("type") == "event":
                        event_data = data["event"].get("data", {})
                        try:
                            await self._on_event(event_data)
                        except Exception:
                            _LOGGER.exception("action event handler failed")
