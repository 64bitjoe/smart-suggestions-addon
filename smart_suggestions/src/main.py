# smart_suggestions/src/main.py
"""Smart Suggestions v4 — pattern ledger pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
import shutil
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic as _anthropic

from action_handler import ActionHandler
from context_matcher import ContextMatcher
from db_reader import DbReader, StateChange
from ha_client import HAClient
from ha_ws import HAEventListener
from lifecycle import passes_emerging
from llm_describer import Describer, template_description
from miners.cross_area import CrossAreaMiner
from miners.sequence import SequenceMiner
from miners.temporal import TemporalMiner
from miners.waste import WasteDetector
from model_router import ModelRouter
from pattern_ledger import PatternLedger
from publisher import Publisher, build_payload
from ws_server import WSServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("smart_suggestions")

_OPTIONS_FILE = "/data/options.json"
_CARD_SRC = "/app/card/smart-suggestions-card.js"
_CARD_DST = "/config/www/smart-suggestions-card.js"
_RECORDER_DB = "/config/home-assistant_v2.db"

# Miners emit permissively; the ledger's adaptive lifecycle decides.
MINE_MIN_OCCURRENCES = 2
MINE_MIN_PROB = 0.3


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
            loop.create_task(
                self._ws.broadcast_log(record.levelname, self.format(record))
            )
        except Exception:
            pass


class SmartSuggestionsAddon:
    def __init__(self, opts: dict) -> None:
        self._opts = opts
        self._domains: set[str] = set(opts.get("domains") or [])
        self._history_days = int(opts.get("history_days", 30))
        self._mining_interval = int(opts.get("mining_interval_hours", 1)) * 3600
        self._waste_interval = int(opts.get("waste_check_interval_minutes", 5)) * 60

        self._ledger = PatternLedger("/data/patterns.db")
        self._matcher = ContextMatcher(max_now=int(opts.get("max_now_suggestions", 5)))
        self._ws_server = WSServer()
        self._db_reader = DbReader(sqlite_path=_RECORDER_DB)

        key = opts.get("ai_api_key", "")
        self._anthropic = _anthropic.AsyncAnthropic(api_key=key) if key else None
        self._router = ModelRouter(
            anthropic_client=self._anthropic,
            primary_model=opts.get("ai_model", "claude-haiku-4-5-20251001"),
            secondary_base_url=opts.get("secondary_base_url", ""),
            secondary_model=opts.get("secondary_model", ""),
            describe_backend=opts.get("describe_backend", "primary"),
        )
        self._describer = Describer(self._router)

        self._ha = HAClient(
            on_states_ready=self._noop_states_ready,
            ha_url=opts.get("ha_url", ""),
            ha_token=opts.get("ha_token", ""),
        )
        self._publisher = Publisher(self._ha)
        self._action_handler = ActionHandler(
            self._ledger, self._ha, self._matcher, self.refresh_publish
        )
        self._listener = HAEventListener(
            on_event=self._action_handler.handle,
            ha_url=opts.get("ha_url", ""),
            ha_token=opts.get("ha_token", ""),
        )
        self._now_items: list[dict] = []
        self._tz: ZoneInfo | None = None
        self._waste_baselines: dict | None = None

    async def _noop_states_ready(self, states: dict) -> None:
        return

    async def _resolve_tz(self):
        """HA's local timezone; cached once known, system-local fallback."""
        if self._tz is None:
            name = await self._ha.get_timezone()
            if name:
                try:
                    self._tz = ZoneInfo(name)
                except Exception:
                    _LOGGER.warning("Unknown HA timezone %s", name)
        return self._tz or datetime.now(timezone.utc).astimezone().tzinfo

    # ── mining ──────────────────────────────────────────────────────

    def _domain_ok(self, entity_id: str) -> bool:
        return not self._domains or entity_id.split(".")[0] in self._domains

    # Presence domains the CrossAreaMiner needs as triggers, on top of the
    # user's actionable-domain allow-list. binary_sensor is deliberately NOT
    # a whole domain here: only motion-named ones are miner triggers, and the
    # rest (door/connectivity/battery) dominate recorder volume on busy homes.
    _TRIGGER_DOMAINS = {"person", "device_tracker"}
    _TRIGGER_LIKE = ["binary_sensor.%motion%"]

    def _mining_domains(self) -> list[str] | None:
        """SQL-level domain filter — without it a busy recorder yields
        millions of irrelevant sensor rows (gigabytes of RAM, minutes of CPU)."""
        if not self._domains:
            return None
        return sorted((self._domains - {"binary_sensor"}) | self._TRIGGER_DOMAINS)

    async def mine_once(self) -> None:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=self._history_days)
        changes = await self._db_reader.get_all_state_changes(
            since,
            domains=self._mining_domains(),
            extra_like=self._TRIGGER_LIKE,
            dedup_consecutive=True,
        )
        _LOGGER.info("mining: loaded %d state changes (deduped)", len(changes))
        top = Counter(c.entity_id for c in changes).most_common(5)
        _LOGGER.info("mining: busiest entities: %s", top)
        if not changes:
            _LOGGER.info("mining: no history rows — skipping run")
            return
        history_days = min(
            self._history_days, max(1.0, (now - changes[0].ts).total_seconds() / 86400)
        )

        self._waste_baselines = WasteDetector()._compute_baseline_durations(changes)

        tz = await self._resolve_tz()
        local_changes = [
            StateChange(c.entity_id, c.state, c.ts.astimezone(tz)) for c in changes
        ]

        automated = await self._ha.get_automated_entities()
        candidates = []
        for miner_coro in (
            TemporalMiner(MINE_MIN_OCCURRENCES, MINE_MIN_PROB).run(local_changes, now=now.astimezone(tz)),
            SequenceMiner(MINE_MIN_OCCURRENCES, MINE_MIN_PROB).run(changes),
            CrossAreaMiner(MINE_MIN_OCCURRENCES, MINE_MIN_PROB).run(changes),
        ):
            try:
                candidates.extend(await miner_coro)
            except Exception:
                _LOGGER.exception("miner failed — continuing")

        ingested = 0
        for c in candidates:
            if not self._domain_ok(c.entity_id) or c.entity_id in automated:
                continue
            if passes_emerging(c.occurrences, c.conditional_prob):
                await self._ledger.upsert_evidence(c)
                ingested += 1

        newly = await self._ledger.run_lifecycle(history_days, now)
        _LOGGER.info(
            "mining: %d candidates, %d ingested, %d newly confirmed",
            len(candidates), ingested, len(newly),
        )
        await self._describe_pending()
        await self.refresh_publish()

    async def _describe_pending(self) -> None:
        for row in await self._ledger.needing_description():
            await self._ledger.bump_describe_attempts(row["signature"])
            title, desc, by = await self._describer.describe(row)
            await self._ledger.save_description(
                row["signature"], title, desc, "", by
            )

    async def waste_once(self) -> None:
        now = datetime.now(timezone.utc)
        current = await self._ha.get_current_on_states()
        waste = await WasteDetector().run([], current, now, baselines=self._waste_baselines)
        for c in waste:
            if not self._domain_ok(c.entity_id):
                continue
            # Waste is urgent: enters the ledger as confirmed, template-described.
            await self._ledger.upsert_evidence(c, initial_lifecycle="confirmed")
            row = await self._ledger.get(c.signature())
            if row and not row["title"]:
                title, desc = template_description(row)
                await self._ledger.save_description(
                    c.signature(), title, desc, "", "template"
                )
        await self.refresh_publish()

    # ── publishing ──────────────────────────────────────────────────

    async def refresh_publish(self) -> None:
        now = datetime.now(timezone.utc)
        confirmed = await self._ledger.get_rows(("confirmed",))
        waste_rows, discovery_rows = [], []
        stale_cutoff = now.timestamp() - 2 * self._waste_interval
        for r in confirmed:
            if r["miner_type"] == "waste":
                snoozed = r["snoozed_until"] and r["snoozed_until"] > now.timestamp()
                if r["last_seen"] >= stale_cutoff and not snoozed:
                    waste_rows.append(r)
            else:
                discovery_rows.append(r)

        zones = await self._publisher.publish(
            self._now_items, discovery_rows, waste_rows
        )
        emerging = await self._ledger.get_rows(("emerging",))
        zones["emerging"] = [build_payload(r, "emerging") for r in emerging]
        self._ws_server.set_zones(zones)
        await self._ws_server.broadcast_zones()

    async def match_once(self) -> None:
        confirmed = await self._ledger.get_rows(("confirmed",))
        rows = [r for r in confirmed if r["miner_type"] != "waste"]
        now = datetime.now(timezone.utc).astimezone(await self._resolve_tz())
        items = self._matcher.match(
            rows, self._ha.get_states(), now
        )
        if [r["signature"] for r in items] != [r["signature"] for r in self._now_items]:
            self._now_items = items
            await self.refresh_publish()
        else:
            self._now_items = items

    # ── loops ───────────────────────────────────────────────────────

    async def _loop(self, fn, interval: int, name: str) -> None:
        while True:
            try:
                await fn()
            except Exception:
                _LOGGER.exception("%s failed", name)
            await asyncio.sleep(interval)

    @staticmethod
    def _install_card() -> None:
        try:
            Path(_CARD_DST).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_CARD_SRC, _CARD_DST)
            _LOGGER.info("Card installed to %s", _CARD_DST)
        except Exception as e:
            _LOGGER.warning("Card install failed (%s) — see panel for manual setup", e)

    async def run(self) -> None:
        _LOGGER.info("Smart Suggestions v4 starting")
        await self._ledger.init()
        self._install_card()

        self._ws_server.register_action_handler(self._action_handler.handle)
        await self._ws_server.start()

        log_handler = _WSLogHandler(self._ws_server)
        log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logging.getLogger().addHandler(log_handler)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: loop.create_task(self._shutdown()))

        loop.create_task(self._loop(self.mine_once, self._mining_interval, "mining"))
        loop.create_task(self._loop(self.waste_once, self._waste_interval, "waste check"))
        loop.create_task(self._loop(self.match_once, 60, "context match"))
        loop.create_task(self._listener.run())

        await self._ha.start()  # blocks forever — keeps event loop alive

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down...")
        self._listener.stop()
        await self._ha.stop()
        await self._ws_server.stop()
        await self._router.close()
        if self._anthropic is not None:
            await self._anthropic.close()


if __name__ == "__main__":
    addon = SmartSuggestionsAddon(_load_options())
    asyncio.run(addon.run())
