# smart_suggestions/src/main.py
"""Smart Suggestions Add-on — main event loop (redesigned)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from pattern_store import PatternStore
from statistical_engine import StatisticalEngine
from anthropic_analyzer import AnthropicAnalyzer
from scene_engine import SceneEngine
from ollama_narrator import OllamaNarrator
from automation_builder import AutomationBuilder
from ha_client import HAClient
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


class _WSLogHandler(logging.Handler):
    def __init__(self, ws_server: WSServer) -> None:
        super().__init__()
        self._ws = ws_server

    def emit(self, record: logging.LogRecord) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ws.broadcast_log(record.levelname, self.format(record)))
        except Exception:
            pass


class SmartSuggestionsAddon:
    def __init__(self, opts: dict) -> None:
        self._opts = opts
        self._ws_server = WSServer()
        self._pattern_store = PatternStore()
        self._stat_engine = StatisticalEngine(
            self._pattern_store,
            confidence_threshold=float(opts.get("pattern_confidence_threshold", 0.6)),
        )
        self._scene_engine = SceneEngine(
            max_suggestions=int(opts.get("max_suggestions", 7)),
            confidence_threshold=float(opts.get("pattern_confidence_threshold", 0.6)),
        )
        self._narrator = OllamaNarrator(
            ollama_url=opts.get("ollama_url", "http://localhost:11434"),
            model=opts.get("ollama_model", "llama3.2"),
        )
        self._analyzer = AnthropicAnalyzer(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=opts.get("ai_api_key", ""),
            ai_model=opts.get("ai_model", "claude-opus-4-5"),
            analysis_depth_days=int(opts.get("analysis_depth_days", 14)),
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._automation_builder = AutomationBuilder(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=opts.get("ai_api_key", ""),
            ai_model=opts.get("ai_model", "claude-opus-4-5"),
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._ha: HAClient | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_suggestions: list = []
        self._last_states: dict = {}
        self._running = True
        self._feedback: dict = _load_feedback()
        self._ha_connected: bool = False
        self._ollama_connected: bool = False
        self._last_refresh_str: str = "Never"
        self._last_analysis_str: str = "Never"

    def _push_system_status(self) -> None:
        status = {
            "ha_connected": self._ha_connected,
            "ollama_connected": self._ollama_connected,
            "ollama_url": self._opts.get("ollama_url", ""),
            "ollama_model": self._opts.get("ollama_model", ""),
            "entity_count": len(self._last_states),
            "last_refresh": self._last_refresh_str,
            "last_analysis": self._last_analysis_str,
            "patterns_loaded": bool(self._pattern_store.get_routines()),
            "pattern_routines": len(self._pattern_store.get_routines()),
            "feedback_count": len(self._feedback),
            "pattern_anomalies": len(self._pattern_store.get_active_anomalies()),
        }
        self._ws_server.set_system_status(status)

    async def _on_states_ready(self, states: dict) -> None:
        self._last_states = states
        if not self._ha_connected:
            self._ha_connected = True
            self._push_system_status()
        await self._run_refresh_cycle(states)

    async def _run_refresh_cycle(self, states: dict) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            await self._ws_server.broadcast_status("updating")
            try:
                # Score candidates deterministically
                candidates = self._stat_engine.score_realtime(states)
                # Rank scenes first, apply feedback
                ranked = self._scene_engine.rank(candidates, states, self._feedback)
                # Narrate reasons (non-blocking: if Ollama fails, falls back to raw reasons)
                try:
                    ranked = await asyncio.wait_for(
                        self._narrator.narrate(ranked), timeout=15.0
                    )
                    self._ollama_connected = True
                except Exception as e:
                    self._ollama_connected = False
                    _LOGGER.warning("Narration skipped: %s", e)

                suggestions = ranked
                if suggestions:
                    self._last_suggestions = suggestions
                else:
                    suggestions = self._last_suggestions

                await self._ws_server.broadcast_suggestions(suggestions)
                if self._ha:
                    await self._ha.write_suggestions_state(suggestions)
                self._last_refresh_str = datetime.now().strftime("%H:%M:%S")
                self._push_system_status()
                _LOGGER.info("Refresh complete: %d suggestions", len(suggestions))
            except Exception as e:
                _LOGGER.error("Refresh cycle error: %s", e)
                await self._ws_server.broadcast_status("error")
                self._push_system_status()

    async def _run_analysis(self) -> None:
        if not self._last_states:
            return
        try:
            history = await self._ha.fetch_history(self._opts.get("analysis_depth_days", 14) * 24)
            patterns = await self._analyzer.analyze(history, self._last_states)
            if any(patterns.values()):
                self._pattern_store.merge(patterns)
                self._last_analysis_str = datetime.now().strftime("%H:%M:%S")
                self._push_system_status()
                _LOGGER.info(
                    "Analysis complete: %d routines, %d correlations, %d anomalies",
                    len(patterns.get("routines", [])),
                    len(patterns.get("correlations", [])),
                    len(patterns.get("anomalies", [])),
                )
        except Exception as e:
            _LOGGER.warning("Analysis failed: %s", e)

    async def _correlation_loop(self) -> None:
        interval = int(self._opts.get("analysis_interval_hours", 6)) * 3600
        while self._running:
            await asyncio.sleep(interval)
            if self._last_states:
                try:
                    history = await self._ha.fetch_history(
                        int(self._opts.get("analysis_depth_days", 14)) * 24
                    )
                    correlations = await self._stat_engine.analyze_correlations(
                        history,
                        self._last_states,
                        window_minutes=int(self._opts.get("correlation_window_minutes", 5)),
                    )
                    if correlations:
                        self._pattern_store.merge({"routines": [], "correlations": correlations, "anomalies": []})
                        _LOGGER.info("Correlation scan: %d correlations stored", len(correlations))
                except Exception as e:
                    _LOGGER.warning("Correlation loop error: %s", e)

    async def _nightly_analysis_scheduler(self) -> None:
        # First-run: trigger immediately if store needs fresh analysis
        if self._pattern_store.needs_fresh_analysis(int(self._opts.get("analysis_depth_days", 14))):
            _LOGGER.info("First-run analysis triggered")
            await self._run_analysis()
        # Then schedule nightly
        schedule_str = self._opts.get("analysis_schedule", "03:00")
        while self._running:
            try:
                h, m = (int(x) for x in schedule_str.split(":"))
            except ValueError:
                h, m = 3, 0
            now = datetime.now()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            _LOGGER.info("Nightly analysis scheduled in %.0f seconds (at %s)", sleep_seconds, schedule_str)
            await asyncio.sleep(sleep_seconds)
            await self._run_analysis()
            # Loop back to top to recompute next occurrence

    async def _on_feedback(self, entity_id: str, vote: str) -> None:
        entry = self._feedback.setdefault(entity_id, {"up": 0, "down": 0})
        entry[vote] = entry.get(vote, 0) + 1
        _save_feedback(self._feedback)
        self._ws_server.set_feedback(self._feedback)
        _LOGGER.info("Feedback: %s %s (net %d)", entity_id, vote, entry["up"] - entry["down"])
        if self._last_states:
            asyncio.get_running_loop().create_task(self._run_refresh_cycle(self._last_states))

    async def _on_save_automation(self, suggestion: dict) -> None:
        _LOGGER.info("Save as automation requested: %s", suggestion.get("entity_id"))
        ctx = suggestion.get("automation_context") or {}
        ctx["entity_id"] = suggestion.get("entity_id", "")
        ctx["name"] = suggestion.get("name", "")
        result = await self._automation_builder.build(ctx, self._ha)
        await self._ws_server.broadcast_automation_result(result)

    async def _on_trigger_analysis(self) -> None:
        asyncio.get_running_loop().create_task(self._run_analysis())

    async def _on_trigger_refresh(self) -> None:
        if self._last_states:
            asyncio.get_running_loop().create_task(self._run_refresh_cycle(self._last_states))

    async def run(self) -> None:
        _LOGGER.info("Smart Suggestions starting")
        self._ws_server.set_feedback(self._feedback)
        self._ws_server.register_feedback_handler(self._on_feedback)
        self._ws_server.register_refresh_handler(self._on_trigger_refresh)
        self._ws_server.register_analyze_handler(self._on_trigger_analysis)
        self._ws_server.register_automation_handler(self._on_save_automation)
        self._push_system_status()
        await self._ws_server.start()

        log_handler = _WSLogHandler(self._ws_server)
        log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(log_handler)

        self._ha = HAClient(
            on_states_ready=self._on_states_ready,
            refresh_interval_seconds=int(self._opts.get("refresh_interval", 10)),
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.create_task(self._shutdown()))

        await self._ha.start()
        loop.create_task(self._correlation_loop())
        loop.create_task(self._nightly_analysis_scheduler())

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down...")
        self._running = False
        if self._ha:
            await self._ha.stop()
        await self._ws_server.stop()


if __name__ == "__main__":
    opts = _load_options()
    addon = SmartSuggestionsAddon(opts)
    asyncio.run(addon.run())
