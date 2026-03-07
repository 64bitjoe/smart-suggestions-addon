"""Smart Suggestions Add-on — main event loop."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from context_builder import build_context, build_prompt
from ha_client import HAClient
from ollama_client import OllamaClient
from ws_server import WSServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("smart_suggestions")

REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "10"))
MAX_SUGGESTIONS = int(os.environ.get("MAX_SUGGESTIONS", "7"))
HISTORY_HOURS = int(os.environ.get("HISTORY_HOURS", "4"))


class SmartSuggestionsAddon:
    def __init__(self) -> None:
        self._ws_server = WSServer()
        self._ollama = OllamaClient()
        self._ha: HAClient | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_suggestions: list = []
        self._running = True

    async def _on_states_ready(self, states: dict) -> None:
        """Called by HAClient when state changes are debounced and ready."""
        await self._run_refresh_cycle(states)

    async def _run_refresh_cycle(self, states: dict) -> None:
        """Build context, stream Ollama, broadcast tokens, write HA state."""
        if self._refresh_lock.locked():
            return  # already refreshing

        async with self._refresh_lock:
            await self._ws_server.broadcast_status("updating")
            try:
                history = await self._ha.fetch_history(HISTORY_HOURS)
                ctx = build_context(states, history)
                prompt = build_prompt(ctx, MAX_SUGGESTIONS)

                loop = asyncio.get_running_loop()

                def on_token(token: str) -> None:
                    loop.create_task(self._ws_server.broadcast_token(token))

                raw = await self._ollama.stream_generate(prompt, on_token)
                suggestions = self._ollama.parse_suggestions(raw, MAX_SUGGESTIONS)

                if suggestions:
                    self._last_suggestions = suggestions
                else:
                    # Keep last known good suggestions on parse failure
                    suggestions = self._last_suggestions

                await self._ws_server.broadcast_suggestions(suggestions)
                await self._ha.write_suggestions_state(suggestions)
                _LOGGER.info("Refresh complete: %d suggestions", len(suggestions))

            except Exception as e:
                _LOGGER.error("Refresh cycle error: %s", e)
                await self._ws_server.broadcast_status("error")

    async def _timer_loop(self) -> None:
        """Periodic refresh independent of state changes."""
        while self._running:
            await asyncio.sleep(REFRESH_INTERVAL * 60)
            if self._ha and self._ha._states:
                await self._run_refresh_cycle(self._ha._states)

    async def run(self) -> None:
        _LOGGER.info(
            "Smart Suggestions starting (refresh=%dm, max=%d, history=%dh)",
            REFRESH_INTERVAL, MAX_SUGGESTIONS, HISTORY_HOURS,
        )

        await self._ws_server.start()
        await self._ollama.start()

        self._ha = HAClient(on_states_ready=self._on_states_ready)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.create_task(self._shutdown()))

        await asyncio.gather(
            self._ha.start(),
            self._timer_loop(),
        )

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down...")
        self._running = False
        await self._ha.stop()
        await self._ollama.stop()
        await self._ws_server.stop()


if __name__ == "__main__":
    addon = SmartSuggestionsAddon()
    asyncio.run(addon.run())
