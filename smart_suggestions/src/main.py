"""Smart Suggestions Add-on — main event loop."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from context_builder import build_context, build_prompt
from ha_client import HAClient, POLL_INTERVAL, REFRESH_INTERVAL as DEFAULT_REFRESH_INTERVAL
from ollama_client import OllamaClient
from ws_server import WSServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("smart_suggestions")

_OPTIONS_FILE = "/data/options.json"
_FEEDBACK_FILE = "/data/feedback.json"


def _load_feedback() -> dict:
    try:
        with open(_FEEDBACK_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        _LOGGER.warning("Could not read feedback file: %s", e)
        return {}


def _save_feedback(fb: dict) -> None:
    try:
        with open(_FEEDBACK_FILE, "w") as f:
            json.dump(fb, f)
    except Exception as e:
        _LOGGER.error("Could not save feedback: %s", e)


def _load_options() -> dict:
    try:
        with open(_OPTIONS_FILE) as f:
            return json.load(f)
    except Exception as e:
        _LOGGER.warning("Could not read %s: %s — using defaults", _OPTIONS_FILE, e)
        return {}


_opts = _load_options()
REFRESH_INTERVAL = int(_opts.get("refresh_interval", 10))
MAX_SUGGESTIONS = int(_opts.get("max_suggestions", 7))
HISTORY_HOURS = int(_opts.get("history_hours", 4))


class SmartSuggestionsAddon:
    def __init__(self) -> None:
        self._ws_server = WSServer()
        self._ollama = OllamaClient()
        self._ha: HAClient | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_suggestions: list = []
        self._running = True
        self._feedback: dict = _load_feedback()

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
                ctx = build_context(states, history, self._feedback)
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

    async def _on_feedback(self, entity_id: str, vote: str) -> None:
        entry = self._feedback.setdefault(entity_id, {"up": 0, "down": 0})
        entry[vote] = entry.get(vote, 0) + 1
        _save_feedback(self._feedback)
        self._ws_server.set_feedback(self._feedback)
        _LOGGER.info("Feedback recorded: %s %s (net %d)", entity_id, vote, entry["up"] - entry["down"])

    async def run(self) -> None:
        from ollama_client import OLLAMA_URL, OLLAMA_MODEL
        _LOGGER.info(
            "Smart Suggestions starting (refresh every %ds, max=%d, history=%dh, ollama=%s, model=%s)",
            POLL_INTERVAL, MAX_SUGGESTIONS, HISTORY_HOURS, OLLAMA_URL, OLLAMA_MODEL,
        )

        self._ws_server.set_feedback(self._feedback)
        self._ws_server.register_feedback_handler(self._on_feedback)
        await self._ws_server.start()
        await self._ollama.start()

        self._ha = HAClient(
            on_states_ready=self._on_states_ready,
            refresh_interval_seconds=REFRESH_INTERVAL * 60,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.create_task(self._shutdown()))

        await self._ha.start()

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down...")
        self._running = False
        await self._ha.stop()
        await self._ollama.stop()
        await self._ws_server.stop()


if __name__ == "__main__":
    addon = SmartSuggestionsAddon()
    asyncio.run(addon.run())
