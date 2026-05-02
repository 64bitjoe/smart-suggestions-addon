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
from db_reader import DbReader
from dismissal_store import DismissalStore
from candidate_filter import CandidateFilter
from llm_describer import LlmDescriber
from miners.temporal import TemporalMiner
from miners.sequence import SequenceMiner
from miners.cross_area import CrossAreaMiner
from miners.waste import WasteDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("smart_suggestions")

_OPTIONS_FILE = "/data/options.json"
_FEEDBACK_FILE = "/data/feedback.json"
_DENY_LIST_FILE = "/data/deny_list.json"

# Shared state between hourly mining loop and 5-minute waste loop.
_pipeline_state: dict = {"last_suggestion_zone": [], "last_noticed_zone": []}


async def mine_and_emit_suggestions(
    db_reader: DbReader,
    dismissal_store: DismissalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
    state: dict,
    history_window_days: int = 30,
) -> None:
    """Run all four miners, filter, describe, and write suggestions."""
    since = datetime.now(timezone.utc) - timedelta(days=history_window_days)
    all_changes = await db_reader.get_all_state_changes(since)

    temporal = await TemporalMiner().run(all_changes, now=datetime.now(timezone.utc))
    sequence = await SequenceMiner().run(all_changes)
    cross = await CrossAreaMiner().run(all_changes)
    candidates = list(temporal) + list(sequence) + list(cross)

    automated_entities = await ha_client.get_automated_entities()
    filt = CandidateFilter(automated_entities=automated_entities, dismissal_store=dismissal_store)
    survivors = await filt.filter(candidates)

    suggestions = []
    for c in survivors:
        desc = await llm_describer.describe(c)
        suggestions.append({
            "miner_type": c.miner_type.value,
            "entity_id": c.entity_id,
            "action": c.action,
            "title": desc.title,
            "description": desc.description,
            "automation_yaml": desc.automation_yaml,
            "confidence": c.conditional_prob,
            "signature": c.signature(),
            "zone": "suggestion",
        })
    state["last_suggestion_zone"] = suggestions

    combined = state["last_suggestion_zone"] + state.get("last_noticed_zone", [])
    await ha_client.write_suggestions(combined)


async def mine_and_emit_waste_only(
    db_reader: DbReader,
    dismissal_store: DismissalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
    state: dict,
) -> None:
    """5-minute waste-only check; refreshes only the noticed zone."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    history = await db_reader.get_all_state_changes(since)
    current = await ha_client.get_current_on_states()
    waste = await WasteDetector().run(history, current, datetime.now(timezone.utc))

    automated = await ha_client.get_automated_entities()
    filt = CandidateFilter(automated_entities=automated, dismissal_store=dismissal_store)
    survivors = await filt.filter(waste)

    noticed = []
    for c in survivors:
        desc = await llm_describer.describe(c)
        noticed.append({
            "miner_type": c.miner_type.value,
            "entity_id": c.entity_id,
            "action": c.action,
            "title": desc.title,
            "description": desc.description,
            "automation_yaml": desc.automation_yaml,
            "confidence": c.conditional_prob,
            "signature": c.signature(),
            "zone": "noticed",
        })
    state["last_noticed_zone"] = noticed

    combined = state.get("last_suggestion_zone", []) + state["last_noticed_zone"]
    await ha_client.write_suggestions(combined)


async def hourly_mining_loop(
    db_reader: DbReader,
    dismissal_store: DismissalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
) -> None:
    while True:
        try:
            await mine_and_emit_suggestions(
                db_reader, dismissal_store, llm_describer, ha_client, _pipeline_state
            )
        except Exception:
            _LOGGER.exception("hourly mining failed")
        await asyncio.sleep(3600)


async def waste_check_loop(
    db_reader: DbReader,
    dismissal_store: DismissalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
) -> None:
    while True:
        try:
            await mine_and_emit_waste_only(
                db_reader, dismissal_store, llm_describer, ha_client, _pipeline_state
            )
        except Exception:
            _LOGGER.exception("waste check failed")
        await asyncio.sleep(300)


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
        _ai_model = opts.get("ai_model", "claude-haiku-4-5-20251001")
        _deep_model = opts.get("deep_analysis_model", "") or _ai_model
        narrator_provider = opts.get("narrator_provider", "ai" if _key else "ollama")
        if narrator_provider == "ai" and _key:
            self._narrator = AINarrator(
                ai_provider=opts.get("ai_provider", "anthropic"),
                ai_api_key=_key,
                ai_model=_ai_model,
                ai_base_url=opts.get("ai_base_url", ""),
            )
            _LOGGER.info("Narrator: using %s/%s", opts.get("ai_provider", "anthropic"), _ai_model)
        else:
            self._narrator = OllamaNarrator(
                ollama_url=opts.get("ollama_url", "http://localhost:11434"),
                model=opts.get("ollama_model", "qwen3:8b"),
            )
            _LOGGER.info("Narrator: using Ollama at %s", opts.get("ollama_url", "http://localhost:11434"))
        _LOGGER.info("AI key loaded: %s (len=%d)", (_key[:8] + "...") if _key else "EMPTY", len(_key))
        _LOGGER.info("Models: narration=%s, deep_analysis=%s", _ai_model, _deep_model)
        self._analyzer = AnthropicAnalyzer(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=_key,
            ai_model=_deep_model,
            analysis_depth_days=int(opts.get("analysis_depth_days", 7)),
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._automation_builder = AutomationBuilder(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=_key,
            ai_model=_ai_model,
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._usage_log = UsageLog("/data/usage.db")
        self._deny_set: set[str] = self._load_deny_list()
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

    @staticmethod
    def _load_deny_list() -> set[str]:
        try:
            with open(_DENY_LIST_FILE) as f:
                data = json.load(f)
            return set(data) if isinstance(data, list) else set()
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_deny_list(self) -> None:
        with open(_DENY_LIST_FILE, "w") as f:
            json.dump(sorted(self._deny_set), f)

    async def _on_deny_entity(self, entity_id: str) -> None:
        self._deny_set.add(entity_id)
        self._save_deny_list()
        _LOGGER.info("Entity denied: %s (%d total)", entity_id, len(self._deny_set))
        if self._last_states:
            await self._run_refresh_cycle(self._last_states)

    async def _on_undeny_entity(self, entity_id: str) -> None:
        self._deny_set.discard(entity_id)
        self._save_deny_list()
        _LOGGER.info("Entity un-denied: %s (%d total)", entity_id, len(self._deny_set))

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
                candidates = self._stat_engine.score_realtime(states, deny_set=self._deny_set)
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
            existing_automations = await self._ha.get_automations() if self._ha else []
            patterns = await self._analyzer.analyze(history, self._last_states, existing_automations)
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
        self._ws_server.register_deny_handler(self._on_deny_entity)
        self._ws_server.register_undeny_handler(self._on_undeny_entity)
        self._ws_server.set_deny_list(self._deny_set)
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

        # ── New pattern-mining pipeline (runs alongside old paths until Task 12 removes them) ──
        import anthropic as _anthropic
        _db_reader = DbReader(sqlite_path="/config/home-assistant_v2.db")
        _dismissal_store = DismissalStore(db_path="/data/dismissals.db")
        _anthropic_client = _anthropic.AsyncAnthropic(
            api_key=self._opts.get("ai_api_key", "")
        )
        _llm_describer = LlmDescriber(
            client=_anthropic_client,
            cache_path="/data/llm_cache.db",
            model=self._opts.get("ai_model", "claude-haiku-4-5-20251001"),
        )
        loop.create_task(hourly_mining_loop(_db_reader, _dismissal_store, _llm_describer, self._ha))
        loop.create_task(waste_check_loop(_db_reader, _dismissal_store, _llm_describer, self._ha))

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
