"""Smart Suggestions Add-on — main event loop."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from context_builder import build_context, build_prompt, _ACTION_DOMAINS
from ha_client import HAClient, POLL_INTERVAL, REFRESH_INTERVAL as DEFAULT_REFRESH_INTERVAL
from ollama_client import OllamaClient
from pattern_analyzer import PatternAnalyzer
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


def _remove_noops(suggestions: list, states: dict) -> list:
    """Remove suggestions whose action already matches the entity's current state."""
    out = []
    for s in suggestions:
        eid = s.get("entity_id")
        action = s.get("action", "")
        current = states.get(eid, {}).get("state", "") if eid else ""
        if action == "turn_off" and current == "off":
            continue
        if action == "turn_on" and current == "on":
            continue
        out.append(s)
    return out


_opts = _load_options()
REFRESH_INTERVAL = int(_opts.get("refresh_interval", 10))
MAX_SUGGESTIONS = int(_opts.get("max_suggestions", 7))
HISTORY_HOURS = int(_opts.get("history_hours", 4))
ANALYSIS_INTERVAL = 7200  # 2 hours


class _WSLogHandler(logging.Handler):
    """Forwards log records to the WebSocket UI log panel."""

    def __init__(self, ws_server: WSServer) -> None:
        super().__init__()
        self._ws = ws_server

    def emit(self, record: logging.LogRecord) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ws.broadcast_log(record.levelname, self.format(record)))
        except Exception:
            pass  # Never raise from a log handler


class SmartSuggestionsAddon:
    def __init__(self) -> None:
        self._ws_server = WSServer()
        self._ollama = OllamaClient()
        self._ha: HAClient | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_suggestions: list = []
        self._last_states: dict = {}
        self._running = True
        self._feedback: dict = _load_feedback()
        self._patterns: dict = PatternAnalyzer.load_patterns()
        self._pattern_analyzer = PatternAnalyzer(self._ollama)
        self._last_analysis: float = 0.0
        self._ha_connected: bool = False
        self._ollama_connected: bool = False
        self._last_refresh_str: str = "Never"
        self._last_analysis_str: str = "Never"

    def _push_system_status(self) -> None:
        from ollama_client import OLLAMA_URL, OLLAMA_MODEL  # noqa: PLC0415
        status = {
            "ha_connected": self._ha_connected,
            "ollama_connected": self._ollama_connected,
            "ollama_url": OLLAMA_URL,
            "ollama_model": OLLAMA_MODEL,
            "entity_count": len(self._last_states),
            "last_refresh": self._last_refresh_str,
            "last_analysis": self._last_analysis_str,
            "patterns_loaded": bool(self._patterns),
            "pattern_routines": len(self._patterns.get("routines", [])) if self._patterns else 0,
            "pattern_anomalies": len(self._patterns.get("anomalies", [])) if self._patterns else 0,
            "feedback_count": len(self._feedback),
        }
        self._ws_server.set_system_status(status)

    async def _on_states_ready(self, states: dict) -> None:
        """Called by HAClient when state changes are debounced and ready."""
        self._last_states = states
        if not self._ha_connected:
            self._ha_connected = True
            self._push_system_status()
        # Populate inject panel with ALL actionable entities (no dormancy filter)
        all_entities = [
            {
                "entity_id": eid,
                "name": s.get("attributes", {}).get("friendly_name", eid),
                "domain": eid.split(".")[0],
                "current_state": s.get("state"),
            }
            for eid, s in states.items()
            if eid.split(".")[0] in _ACTION_DOMAINS
            and s.get("state") not in ("unavailable", "unknown", "")
        ]
        all_entities.sort(key=lambda e: e["name"].lower())
        self._ws_server.set_known_entities(all_entities)
        await self._run_refresh_cycle(states)

    async def _trigger_analysis(self) -> None:
        """Manually trigger a pattern analysis cycle."""
        if not self._last_states:
            _LOGGER.warning("Pattern analysis requested but no states loaded yet")
            return
        _LOGGER.info("Manual pattern analysis triggered")
        asyncio.get_running_loop().create_task(self._run_analysis())

    async def _run_analysis(self) -> None:
        """Run a single pattern analysis cycle (shared by loop and manual trigger)."""
        try:
            history_48h = await self._ha.fetch_history(48)
            patterns = await self._pattern_analyzer.analyze(history_48h, self._last_states)
            if patterns:
                self._patterns = patterns
                PatternAnalyzer.save_patterns(patterns)
                from datetime import datetime as _dt  # noqa: PLC0415
                self._last_analysis_str = _dt.now().strftime("%H:%M:%S")
                self._push_system_status()
                _LOGGER.info(
                    "Pattern analysis complete: %d routines, %d anomalies",
                    len(patterns.get("routines", [])),
                    len(patterns.get("anomalies", [])),
                )
            else:
                _LOGGER.info("Pattern analysis returned no patterns")
        except Exception as e:
            _LOGGER.warning("Pattern analysis failed: %s", e)

    async def _trigger_refresh(self) -> None:
        """Trigger a refresh using the last known states (called from web UI or feedback)."""
        if self._last_states:
            asyncio.get_running_loop().create_task(
                self._run_refresh_cycle(self._last_states)
            )

    async def _run_refresh_cycle(self, states: dict) -> None:
        """Build context, stream Ollama, broadcast tokens, write HA state."""
        if self._refresh_lock.locked():
            return  # already refreshing

        async with self._refresh_lock:
            await self._ws_server.broadcast_status("updating")
            try:
                history = await self._ha.fetch_history(HISTORY_HOURS)
                dow_history = await self._ha.fetch_dow_history()
                ctx = build_context(states, history, self._feedback, dow_history=dow_history)
                prompt = build_prompt(ctx, MAX_SUGGESTIONS, patterns=self._patterns or None)

                loop = asyncio.get_running_loop()

                def on_token(token: str) -> None:
                    loop.create_task(self._ws_server.broadcast_token(token))

                raw = await self._ollama.stream_generate(prompt, on_token)
                self._ollama_connected = bool(raw)
                suggestions = self._ollama.parse_suggestions(raw, MAX_SUGGESTIONS)
                suggestions = _remove_noops(suggestions, states)

                # Post-LLM validation — drop hallucinated entity_ids and hard-downvoted
                valid_eids = {a["entity_id"] for a in ctx["available_actions"]}
                score_map = {a["entity_id"]: a.get("score", 50) for a in ctx["available_actions"]}
                validated = []
                for s in suggestions:
                    eid = s.get("entity_id")
                    if eid not in valid_eids:
                        _LOGGER.warning("Dropping hallucinated entity_id: %s", eid)
                        continue
                    fb = self._feedback.get(eid, {})
                    if isinstance(fb, dict) and fb.get("up", 0) - fb.get("down", 0) <= -3:
                        _LOGGER.info("Excluding hard-downvoted entity: %s", eid)
                        continue
                    s["score"] = score_map.get(eid, 50)
                    validated.append(s)
                suggestions = validated

                # Impression tracking
                shown_eids = {s["entity_id"] for s in suggestions if s.get("entity_id")}
                for eid in shown_eids:
                    entry = self._feedback.setdefault(eid, {"up": 0, "down": 0, "shown": 0})
                    entry["shown"] = min(entry.get("shown", 0) + 1, 8)
                _save_feedback(self._feedback)

                if suggestions:
                    self._last_suggestions = suggestions
                else:
                    # Keep last known good suggestions on parse failure
                    suggestions = self._last_suggestions

                await self._ws_server.broadcast_suggestions(suggestions)
                await self._ha.write_suggestions_state(suggestions)
                from datetime import datetime as _dt  # noqa: PLC0415
                self._last_refresh_str = _dt.now().strftime("%H:%M:%S")
                self._push_system_status()
                _LOGGER.info("Refresh complete: %d suggestions (candidates scored, top-%d sent to Ollama)",
                             len(suggestions), len(ctx["available_actions"]))

            except Exception as e:
                self._ollama_connected = False
                _LOGGER.error("Refresh cycle error: %s", e)
                self._push_system_status()
                await self._ws_server.broadcast_status("error")

    async def _on_feedback(self, entity_id: str, vote: str) -> None:
        entry = self._feedback.setdefault(entity_id, {"up": 0, "down": 0, "shown": 0})
        entry[vote] = entry.get(vote, 0) + 1
        entry["shown"] = 0  # reset impression penalty on explicit feedback
        _save_feedback(self._feedback)
        self._ws_server.set_feedback(self._feedback)
        _LOGGER.info("Feedback recorded: %s %s (net %d)", entity_id, vote, entry["up"] - entry["down"])
        # Immediately re-run so new suggestions reflect the vote
        await self._trigger_refresh()

    async def _ollama_health_check(self) -> None:
        """Ping Ollama on startup to set connectivity state immediately."""
        from ollama_client import OLLAMA_URL  # noqa: PLC0415
        import aiohttp  # noqa: PLC0415
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{OLLAMA_URL}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    self._ollama_connected = r.status == 200
        except Exception:
            self._ollama_connected = False
        _LOGGER.info("Ollama health check: %s", "reachable" if self._ollama_connected else "unreachable")
        self._push_system_status()

    async def run(self) -> None:
        from ollama_client import OLLAMA_URL, OLLAMA_MODEL
        _LOGGER.info(
            "Smart Suggestions starting (refresh every %ds, max=%d, history=%dh, ollama=%s, model=%s)",
            POLL_INTERVAL, MAX_SUGGESTIONS, HISTORY_HOURS, OLLAMA_URL, OLLAMA_MODEL,
        )

        self._ws_server.set_feedback(self._feedback)
        self._ws_server.register_feedback_handler(self._on_feedback)
        self._ws_server.register_refresh_handler(self._trigger_refresh)
        self._ws_server.register_analyze_handler(self._trigger_analysis)
        self._push_system_status()
        await self._ws_server.start()

        # Stream all log output to the web UI log panel
        log_handler = _WSLogHandler(self._ws_server)
        log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(log_handler)
        await self._ollama.start()
        asyncio.get_running_loop().create_task(self._ollama_health_check())

        self._ha = HAClient(
            on_states_ready=self._on_states_ready,
            refresh_interval_seconds=REFRESH_INTERVAL * 60,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.create_task(self._shutdown()))

        await self._ha.start()
        asyncio.get_running_loop().create_task(self._analysis_loop())

    async def _analysis_loop(self) -> None:
        """Background task: run deep pattern analysis every 2 hours."""
        while self._running:
            await asyncio.sleep(ANALYSIS_INTERVAL)
            if self._last_states:
                await self._run_analysis()

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down...")
        self._running = False
        await self._ha.stop()
        await self._ollama.stop()
        await self._ws_server.stop()


if __name__ == "__main__":
    addon = SmartSuggestionsAddon()
    asyncio.run(addon.run())
