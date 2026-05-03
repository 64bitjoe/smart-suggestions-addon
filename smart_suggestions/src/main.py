# smart_suggestions/src/main.py
"""Smart Suggestions Add-on — main event loop (redesigned)."""
from __future__ import annotations

import anthropic as _anthropic
import asyncio
import json
import logging
import signal
from datetime import datetime, timedelta, timezone

from automation_builder import AutomationBuilder
from ha_client import HAClient
from usage_log import UsageLog
from ws_server import WSServer
from db_reader import DbReader
from signal_store import SignalStore, SignalType
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

# Cost-control caps. Keep in sync with config.yaml mining options.
MAX_SURVIVORS_PER_CYCLE = 10  # Top-N candidates by conditional_prob to describe


async def mine_and_emit_suggestions(
    db_reader: DbReader,
    signal_store: SignalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
    state: dict,
    history_window_days: int = 30,
    user_pattern_store=None,
    outcome_tracker=None,
) -> None:
    """Run all four miners, filter, describe, and write suggestions."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=history_window_days)
    all_changes = await db_reader.get_all_state_changes(since)

    temporal = await TemporalMiner().run(all_changes, now=now)
    sequence = await SequenceMiner().run(all_changes)
    cross = await CrossAreaMiner().run(all_changes)
    candidates = list(temporal) + list(sequence) + list(cross)

    automated_entities = await ha_client.get_automated_entities()
    filt = CandidateFilter(
        automated_entities=automated_entities,
        signal_store=signal_store,
        outcome_tracker=outcome_tracker,
    )
    survivors = await filt.filter(candidates)

    # Cap cost: only describe top-N survivors per cycle, ranked by conditional_prob.
    survivors.sort(key=lambda c: c.conditional_prob, reverse=True)
    survivors = survivors[:MAX_SURVIVORS_PER_CYCLE]

    # Append user-confirmed patterns (always surface, bypass filter and top-N cap)
    if user_pattern_store is not None:
        try:
            user_patterns = await user_pattern_store.list_all()
            for p in user_patterns:
                survivors.append(_user_pattern_to_candidate(p))
        except Exception:
            _LOGGER.exception("Failed to load user patterns")

    suggestions = []
    for c in survivors:
        user_confirmed = c.details.get("user_confirmed", False) if c.details else False
        desc = await llm_describer.describe(c, user_confirmed=user_confirmed)
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
            **({"user_confirmed": True, "user_label": c.details.get("user_label", "")} if user_confirmed else {}),
        })

    # Record pending outcomes for suggestion-zone items
    if outcome_tracker is not None:
        now = datetime.now(timezone.utc)
        for s in suggestions:
            try:
                target = s.get("details", {})
                target_entity = s.get("entity_id", "")
                target_action = s.get("action", "")
                await outcome_tracker.record_pending(
                    signature=s["signature"],
                    miner_type=s["miner_type"],
                    target_entity=target_entity,
                    target_action=target_action,
                    suggested_at=now,
                )
            except Exception:
                pass  # outcome tracking must never block suggestion emission

    state["last_suggestion_zone"] = suggestions

    combined = state["last_suggestion_zone"] + state.get("last_noticed_zone", [])
    await ha_client.write_suggestions(combined)
    state["hourly_completed"] = True
    state["last_hourly_at"] = datetime.now(timezone.utc).isoformat()


async def mine_and_emit_waste_only(
    db_reader: DbReader,
    signal_store: SignalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
    state: dict,
    outcome_tracker=None,
) -> None:
    """5-minute waste-only check; refreshes only the noticed zone."""
    if not state.get("hourly_completed", False):
        _LOGGER.info("waste check: skipping write, hourly miner has not completed yet")
        return
    since = datetime.now(timezone.utc) - timedelta(days=30)
    history = await db_reader.get_all_state_changes(since)
    current = await ha_client.get_current_on_states()
    waste = await WasteDetector().run(history, current, datetime.now(timezone.utc))

    automated = await ha_client.get_automated_entities()
    filt = CandidateFilter(
        automated_entities=automated,
        signal_store=signal_store,
        outcome_tracker=outcome_tracker,
    )
    survivors = await filt.filter(waste)

    noticed = []
    for c in survivors:
        desc = await llm_describer.describe(c, user_confirmed=False)
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
    state["last_waste_at"] = datetime.now(timezone.utc).isoformat()


async def hourly_mining_loop(
    db_reader: DbReader,
    signal_store: SignalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
    state: dict,
    user_pattern_store=None,
    outcome_tracker=None,
) -> None:
    while True:
        try:
            await mine_and_emit_suggestions(
                db_reader, signal_store, llm_describer, ha_client, state,
                user_pattern_store=user_pattern_store,
                outcome_tracker=outcome_tracker,
            )
        except Exception:
            _LOGGER.exception("hourly mining failed")
        await asyncio.sleep(3600)


async def waste_check_loop(
    db_reader: DbReader,
    signal_store: SignalStore,
    llm_describer: LlmDescriber,
    ha_client: HAClient,
    state: dict,
    outcome_tracker=None,
) -> None:
    while True:
        try:
            await mine_and_emit_waste_only(
                db_reader, signal_store, llm_describer, ha_client, state,
                outcome_tracker=outcome_tracker,
            )
        except Exception:
            _LOGGER.exception("waste check failed")
        await asyncio.sleep(300)


async def outcome_check_loop(outcome_tracker, ha_client) -> None:
    """Every 5 min, check pending outcomes against current HA state."""
    while True:
        try:
            await check_pending_outcomes(outcome_tracker, ha_client)
        except Exception:
            _LOGGER.exception("outcome check failed")
        await asyncio.sleep(300)


async def check_pending_outcomes(outcome_tracker, ha_client) -> None:
    pending = await outcome_tracker.list_pending(max_age=outcome_tracker.action_window)
    if not pending:
        return
    states = getattr(ha_client, '_states', {}) or {}
    now = datetime.now(timezone.utc)
    for entry in pending:
        eid = entry["target_entity"]
        suggested_at = datetime.fromtimestamp(entry["suggested_at"], tz=timezone.utc)
        age = (now - suggested_at).total_seconds()

        s = states.get(eid)
        if s and s.get("last_changed"):
            try:
                last = datetime.fromisoformat(s["last_changed"].replace("Z", "+00:00"))
                if last >= suggested_at:
                    action = entry["target_action"]
                    if _action_matches(s, action):
                        await outcome_tracker.resolve(entry["id"], "acted", last)
                        continue
            except Exception:
                pass

        # If past action window, mark expired
        if age > outcome_tracker.action_window.total_seconds():
            await outcome_tracker.resolve(entry["id"], "expired", now)


def _action_matches(state_dict: dict, action: str) -> bool:
    s = state_dict.get("state", "")
    if action == "turn_on" and s in {"on", "heat", "cool", "playing", "open"}:
        return True
    if action == "turn_off" and s in {"off", "idle", "closed", "unlocked"}:
        return True
    if action.startswith("set_state_"):
        return s == action[len("set_state_"):]
    return False


def _user_pattern_to_candidate(p) -> "Candidate":
    from candidate import Candidate, MinerType
    return Candidate(
        miner_type=MinerType.SEQUENCE,
        entity_id=p.trigger_entity,
        action=p.trigger_action,
        details={
            "target_entity": p.target_entity,
            "target_action": p.target_action,
            "delta_seconds": p.latency_seconds,
            "user_confirmed": True,
            "user_label": p.label,
        },
        occurrences=999,
        conditional_prob=1.0,
    )


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
        self._signal_store = SignalStore(db_path="/data/signals.db")
        from user_pattern_store import UserPatternStore
        from outcome_tracker import OutcomeTracker
        self._user_pattern_store = UserPatternStore(db_path="/data/user_patterns.db")
        self._outcome_tracker = OutcomeTracker(db_path="/data/outcomes.db")
        self._ws_server = WSServer(
            signal_store=self._signal_store,
            user_pattern_store=self._user_pattern_store,
            outcome_tracker=self._outcome_tracker,
        )
        _key = opts.get("ai_api_key", "")
        _ai_model = opts.get("ai_model", "claude-haiku-4-5-20251001")
        _LOGGER.info("AI key loaded: %s (len=%d)", (_key[:8] + "...") if _key else "EMPTY", len(_key))
        _LOGGER.info("Model: %s", _ai_model)
        self._automation_builder = AutomationBuilder(
            ai_provider=opts.get("ai_provider", "anthropic"),
            ai_api_key=_key,
            ai_model=_ai_model,
            ai_base_url=opts.get("ai_base_url", ""),
        )
        self._usage_log = UsageLog("/data/usage.db")
        self._deny_set: set[str] = self._load_deny_list()
        self._ha: HAClient | None = None
        self._last_states: dict = {}
        self._running = True
        self._ha_connected: bool = False
        self._last_refresh_str: str = "Never"
        self._anthropic_client = None
        self._db_reader: DbReader | None = None
        self._llm_describer: LlmDescriber | None = None
        self._pipeline_state: dict = {
            "last_suggestion_zone": [],
            "last_noticed_zone": [],
            "hourly_completed": False,
            "last_hourly_at": None,
            "last_waste_at": None,
        }
        # Lock to prevent concurrent Mine Now invocations (e.g. double-click).
        self._mine_now_lock = asyncio.Lock()

    def _push_system_status(self) -> None:
        status = {
            "ha_connected": self._ha_connected,
            "entity_count": len(self._last_states),
            "last_refresh": self._last_refresh_str,
        }
        self._ws_server.set_system_status(status)

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
        _LOGGER.info("Entity denied: %s (%d total) — will take effect on next mining cycle", entity_id, len(self._deny_set))

    async def _on_undeny_entity(self, entity_id: str) -> None:
        self._deny_set.discard(entity_id)
        self._save_deny_list()
        _LOGGER.info("Entity un-denied: %s (%d total)", entity_id, len(self._deny_set))

    async def _on_states_ready(self, states: dict) -> None:
        self._last_states = states
        if not self._ha_connected:
            self._ha_connected = True
            self._push_system_status()

    async def _on_feedback(self, entity_id: str, vote: str) -> None:
        outcome = "run" if vote == "up" else "dismissed"
        await self._usage_log.log(entity_id, "", outcome, 0.0)
        _LOGGER.info("Feedback logged: %s %s", entity_id, outcome)

    async def _on_save_automation(self, suggestion: dict) -> None:
        _LOGGER.info("Save as automation requested: %s", suggestion.get("entity_id"))
        ctx = suggestion.get("automation_context") or {}
        ctx["entity_id"] = suggestion.get("entity_id", "")
        ctx["name"] = suggestion.get("name", "")
        result = await self._automation_builder.build(ctx, self._ha)
        await self._ws_server.broadcast_automation_result(result)

    async def _on_trigger_refresh(self) -> None:
        """Re-broadcast last cached suggestions. Does NOT re-run mining."""
        combined = (
            self._pipeline_state.get("last_suggestion_zone", []) +
            self._pipeline_state.get("last_noticed_zone", [])
        )
        await self._ws_server.broadcast_suggestions(combined)

    async def _on_trigger_refresh_all(self) -> None:
        """User-initiated trigger — runs the full mining pipeline immediately.

        Guarded by an asyncio.Lock so accidental double-clicks (or rapid retries)
        don't start parallel mining cycles, which would multiply Claude API costs
        with no benefit.
        """
        if self._mine_now_lock.locked():
            _LOGGER.info("Mine Now ignored — a cycle is already running")
            return
        async with self._mine_now_lock:
            _LOGGER.info("Mine Now triggered")
            if not self._db_reader or not self._llm_describer or not self._ha:
                _LOGGER.warning("Mine Now: pipeline not ready yet (db_reader=%s, llm_describer=%s, ha=%s)",
                                self._db_reader, self._llm_describer, self._ha)
                return
            try:
                await mine_and_emit_suggestions(
                    self._db_reader,
                    self._signal_store,
                    self._llm_describer,
                    self._ha,
                    self._pipeline_state,
                    user_pattern_store=self._user_pattern_store,
                    outcome_tracker=self._outcome_tracker,
                )
                # Also run a waste check immediately since hourly_completed is now True.
                await mine_and_emit_waste_only(
                    self._db_reader,
                    self._signal_store,
                    self._llm_describer,
                    self._ha,
                    self._pipeline_state,
                    outcome_tracker=self._outcome_tracker,
                )
                _LOGGER.info("Mine Now complete")
            except Exception:
                _LOGGER.exception("Mine Now failed")

    async def run(self) -> None:
        _LOGGER.info("Smart Suggestions starting")
        self._ws_server.register_feedback_handler(self._on_feedback)
        self._ws_server.register_refresh_handler(self._on_trigger_refresh)
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

        self._db_reader = DbReader(sqlite_path="/config/home-assistant_v2.db")
        self._anthropic_client = _anthropic.AsyncAnthropic(
            api_key=self._opts.get("ai_api_key", "")
        )
        self._llm_describer = LlmDescriber(
            client=self._anthropic_client,
            cache_path="/data/llm_cache.db",
            model=self._opts.get("ai_model", "claude-haiku-4-5-20251001"),
        )
        self._ws_server.set_pipeline_state(self._pipeline_state, self._opts)
        loop.create_task(hourly_mining_loop(
            self._db_reader, self._signal_store, self._llm_describer, self._ha,
            self._pipeline_state,
            user_pattern_store=self._user_pattern_store,
            outcome_tracker=self._outcome_tracker,
        ))
        loop.create_task(waste_check_loop(
            self._db_reader, self._signal_store, self._llm_describer, self._ha,
            self._pipeline_state,
            outcome_tracker=self._outcome_tracker,
        ))
        loop.create_task(outcome_check_loop(self._outcome_tracker, self._ha))

        await self._ha.start()  # blocks forever — keeps event loop alive

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down...")
        self._running = False
        if self._ha:
            await self._ha.stop()
        await self._ws_server.stop()
        if self._anthropic_client is not None:
            # SDK >= 0.20 exposes aclose(); older versions only have close().
            close_fn = getattr(self._anthropic_client, "aclose", None) or getattr(
                self._anthropic_client, "close", None
            )
            if close_fn is not None:
                try:
                    await close_fn()
                except Exception:
                    _LOGGER.exception("Anthropic client close failed (non-fatal)")


if __name__ == "__main__":
    opts = _load_options()
    addon = SmartSuggestionsAddon(opts)
    asyncio.run(addon.run())
