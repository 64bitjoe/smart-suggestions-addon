# smart_suggestions/src/main.py
"""Smart Suggestions Add-on — main event loop (redesigned)."""
from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timedelta, timezone

from pattern_store import PatternStore
from statistical_engine import StatisticalEngine
from anthropic_analyzer import AnthropicAnalyzer
from scene_engine import SceneEngine
from narrator import OllamaNarrator, AINarrator
from automation_builder import AutomationBuilder
from ha_client import HAClient
from usage_log import UsageLog
from ws_server import WSServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("smart_suggestions")

_OPTIONS_FILE = "/data/options.json"
_FEEDBACK_FILE = "/data/feedback.json"


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
            allowed_domains=opts.get("domains") or None,
            max_entities=int(opts.get("max_entities", 150)),
        )
        self._scene_engine = SceneEngine(
            max_suggestions=int(opts.get("max_suggestions", 7)),
            confidence_threshold=float(opts.get("pattern_confidence_threshold", 0.6)),
        )
        _key = opts.get("ai_api_key", "")
        narrator_provider = opts.get("narrator_provider", "ai" if _key else "ollama")
        if narrator_provider == "ai" and _key:
            self._narrator = AINarrator(
                ai_provider=opts.get("ai_provider", "anthropic"),
                ai_api_key=_key,
                ai_model=opts.get("ai_model", "claude-opus-4-6"),
                ai_base_url=opts.get("ai_base_url", ""),
            )
            _LOGGER.info("Narrator: using %s/%s", opts.get("ai_provider", "anthropic"), opts.get("ai_model", "claude-opus-4-6"))
        else:
            self._narrator = OllamaNarrator(
                ollama_url=opts.get("ollama_url", "http://localhost:11434"),
                model=opts.get("ollama_model", "qwen3:8b"),
            )
            _LOGGER.info("Narrator: using Ollama at %s", opts.get("ollama_url", "http://localhost:11434"))
        _LOGGER.info("AI key loaded: %s (len=%d)", (_key[:8] + "...") if _key else "EMPTY", len(_key))
        self._analyzer = AnthropicAnalyzer(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=_key,
            ai_model=opts.get("ai_model", "claude-opus-4-6"),
            analysis_depth_days=int(opts.get("analysis_depth_days", 7)),
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._automation_builder = AutomationBuilder(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=_key,
            ai_model=opts.get("ai_model", "claude-opus-4-6"),
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._usage_log = UsageLog("/data/usage.db")
        self._ha: HAClient | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_suggestions: list = []
        self._last_states: dict = {}
        self._running = True
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
            "feedback_count": 0,
            "pattern_anomalies": len(self._pattern_store.get_active_anomalies()),
        }
        self._ws_server.set_system_status(status)

    def _push_stored_patterns(self) -> None:
        patterns = {
            "routines": self._pattern_store.get_routines(),
            "correlations": self._pattern_store.get_correlations(),
            "anomalies": self._pattern_store.get_active_anomalies(),
        }
        if any(patterns.values()):
            self._ws_server.set_patterns(patterns)

    async def _build_narrator_context(self, states: dict) -> dict:
        """Assemble rich context dict for the Ollama narrator."""
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        current_time = now.strftime("%H:%M on %A")

        # Entities changed in last 60 minutes
        recent_changes = []
        cutoff = now - timedelta(minutes=60)
        for eid, state in states.items():
            lc = state.get("last_changed", "")
            try:
                changed = datetime.fromisoformat(lc.replace("Z", "+00:00"))
                if changed >= cutoff:
                    mins_ago = int((now - changed).total_seconds() / 60)
                    recent_changes.append({
                        "entity_id": eid,
                        "state": state.get("state"),
                        "changed_ago_minutes": mins_ago,
                    })
            except (ValueError, TypeError):
                pass

        # Motion / occupancy sensors
        motion_sensors = []
        for eid, state in states.items():
            if eid.startswith("binary_sensor.") and any(
                kw in eid for kw in ("motion", "occupancy", "presence")
            ):
                lc = state.get("last_changed", "")
                mins_since = None
                try:
                    changed = datetime.fromisoformat(lc.replace("Z", "+00:00"))
                    mins_since = int((now - changed).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass
                motion_sensors.append({
                    "entity_id": eid,
                    "state": state.get("state"),
                    "minutes_since_triggered": mins_since,
                })

        # Presence
        presence = [
            eid for eid, s in states.items()
            if eid.startswith("person.") and s.get("state") == "home"
        ]

        # Weather — check sensor first, then weather entity for condition
        temp_val = None
        condition_val = None
        outdoor_temp = states.get("sensor.outdoor_temperature")
        if outdoor_temp:
            temp_val = outdoor_temp.get("state")
        for eid, state in states.items():
            if eid.startswith("weather."):
                condition_val = state.get("state")
                if temp_val is None:
                    temp_val = state.get("attributes", {}).get("temperature")
                break
        weather = {"temperature": temp_val, "condition": condition_val} if (temp_val or condition_val) else None

        # Avoided pairs from usage log
        avoided = await self._usage_log.get_avoided_pairs(hours=24, limit=10)

        # Existing automations
        existing_automations: list[str] = []
        if self._ha:
            existing_automations = await self._ha.get_automations()

        return {
            "current_time": current_time,
            "recent_changes": recent_changes[:10],
            "motion_sensors": motion_sensors[:10],
            "presence": presence,
            "weather": weather,
            "avoided_pairs": avoided,
            "existing_automations": existing_automations[:30],
        }

    async def _on_states_ready(self, states: dict) -> None:
        self._last_states = states
        if not self._ha_connected:
            self._ha_connected = True
            self._push_system_status()
            self._push_stored_patterns()
        await self._run_refresh_cycle(states)

    async def _run_refresh_cycle(self, states: dict) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            await self._ws_server.broadcast_status("updating")
            try:
                # Score candidates deterministically
                candidates = self._stat_engine.score_realtime(states)
                # Rank scenes first, apply feedback from UsageLog
                entity_ids = [c["entity_id"] for c in candidates]
                feedback_scores = await self._usage_log.get_feedback_scores(entity_ids)
                ranked = self._scene_engine.rank(candidates, states, feedback_scores)
                # Build rich context for narrator
                context = await self._build_narrator_context(states)
                # Narrate reasons (non-blocking: if Ollama fails, falls back to raw reasons)
                try:
                    ranked = await asyncio.wait_for(
                        self._narrator.narrate(ranked, context=context), timeout=20.0
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
        if not self._last_states or not self._ha:
            return
        try:
            history = await self._ha.fetch_history(self._opts.get("analysis_depth_days", 7) * 24)
            patterns = await self._analyzer.analyze(history, self._last_states)
            if any(patterns.values()):
                self._pattern_store.merge(patterns)
                self._push_stored_patterns()
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
            if self._last_states and self._ha:
                try:
                    history = await self._ha.fetch_history(
                        int(self._opts.get("analysis_depth_days", 7)) * 24
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
        # Wait until states are loaded before first-run analysis
        while self._running and not self._last_states:
            await asyncio.sleep(1)
        # First-run: trigger immediately if store needs fresh analysis
        if self._pattern_store.needs_fresh_analysis(int(self._opts.get("analysis_depth_days", 7))):
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
        outcome = "run" if vote == "up" else "dismissed"
        await self._usage_log.log(entity_id, "", outcome, 0.0)
        _LOGGER.info("Feedback logged: %s %s", entity_id, outcome)
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

    async def _on_trigger_refresh_all(self) -> None:
        """Full pipeline: re-fetch states, run analysis + correlations, then refresh suggestions."""
        _LOGGER.info("Refresh All triggered")
        if not self._ha:
            return
        try:
            # 1. Fetch fresh states
            await self._ha._fetch_states()
            states = self._last_states
            if not states:
                return
            # 2. Run pattern analysis
            await self._run_analysis()
            # 3. Run correlation scan
            history = await self._ha.fetch_history(
                int(self._opts.get("analysis_depth_days", 7)) * 24
            )
            correlations = await self._stat_engine.analyze_correlations(
                history, states,
                window_minutes=int(self._opts.get("correlation_window_minutes", 15)),
            )
            if correlations:
                self._pattern_store.merge({"routines": [], "correlations": correlations, "anomalies": []})
            # 4. Refresh suggestions with new patterns
            await self._run_refresh_cycle(states)
            _LOGGER.info("Refresh All complete")
        except Exception as e:
            _LOGGER.error("Refresh All error: %s", e)

    async def run(self) -> None:
        _LOGGER.info("Smart Suggestions starting")
        self._ws_server.register_feedback_handler(self._on_feedback)
        self._ws_server.register_refresh_handler(self._on_trigger_refresh)
        self._ws_server.register_analyze_handler(self._on_trigger_analysis)
        self._ws_server.register_refresh_all_handler(self._on_trigger_refresh_all)
        self._ws_server.register_automation_handler(self._on_save_automation)
        self._ws_server.set_usage_log(self._usage_log)
        self._ws_server.set_automation_builder(self._automation_builder)
        self._push_system_status()
        await self._ws_server.start()
        await self._usage_log.start()
        await self._usage_log.migrate_from_json(_FEEDBACK_FILE)

        log_handler = _WSLogHandler(self._ws_server)
        log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(log_handler)

        self._ha = HAClient(
            on_states_ready=self._on_states_ready,
            refresh_interval_seconds=int(self._opts.get("refresh_interval", 10)),
            ha_url=self._opts.get("ha_url", ""),
            ha_token=self._opts.get("ha_token", ""),
        )
        self._ws_server.set_ha_client(self._ha)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.create_task(self._shutdown()))

        loop.create_task(self._correlation_loop())
        loop.create_task(self._nightly_analysis_scheduler())
        await self._ha.start()  # blocks forever — keeps event loop alive

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
