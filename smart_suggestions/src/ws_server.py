"""WebSocket server for the Lovelace card to connect to via HA Ingress."""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

PORT = 8099


class WSServer:
    """Manages connected card WebSocket clients and broadcasts events."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/", self._health_handler)
        self._runner: web.AppRunner | None = None
        self._last_suggestions: list = []
        self._last_status: str = "idle"

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", PORT)
        await site.start()
        _LOGGER.info("WebSocket server listening on port %d", PORT)

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "clients": len(self._clients)})

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        _LOGGER.info("Card connected (%d total)", len(self._clients))

        # Send current state immediately on connect
        await self._send(ws, {"type": "status", "state": self._last_status})
        if self._last_suggestions:
            await self._send(ws, {"type": "suggestions", "data": self._last_suggestions})

        try:
            async for msg in ws:
                pass  # card sends no messages; just keep alive
        except Exception:
            pass
        finally:
            self._clients.discard(ws)
            _LOGGER.info("Card disconnected (%d remaining)", len(self._clients))

        return ws

    async def _send(self, ws: web.WebSocketResponse, payload: dict) -> None:
        try:
            await ws.send_str(json.dumps(payload))
        except Exception as e:
            _LOGGER.debug("Send failed to client: %s", e)

    async def broadcast(self, payload: dict) -> None:
        """Broadcast a message to all connected clients."""
        dead = set()
        for ws in list(self._clients):
            try:
                await ws.send_str(json.dumps(payload))
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def broadcast_token(self, token: str) -> None:
        await self.broadcast({"type": "streaming", "token": token})

    async def broadcast_suggestions(self, suggestions: list) -> None:
        self._last_suggestions = suggestions
        self._last_status = "ready"
        await self.broadcast({"type": "suggestions", "data": suggestions})
        await self.broadcast({"type": "status", "state": "ready"})

    async def broadcast_status(self, state: str) -> None:
        self._last_status = state
        await self.broadcast({"type": "status", "state": state})
