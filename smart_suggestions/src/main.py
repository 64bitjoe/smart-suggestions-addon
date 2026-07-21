# smart_suggestions/src/main.py
"""Smart Suggestions v4 — pattern ledger pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
import shutil
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic as _anthropic

from action_handler import ActionHandler
from autopilot import partition_matches, within_throttle
from candidate import MinerType
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
ADDON_VERSION = "4.1.1"  # keep in sync with config.yaml

# Miners emit permissively; the ledger's adaptive lifecycle decides.
MINE_MIN_OCCURRENCES = 2
MINE_MIN_PROB = 0.3

# Domains that can never be acted on — stripped from the user's allow-list
# (v3 configs listed 'sensor' as *context*; in v4 domains are actionable).
NON_ACTIONABLE_DOMAINS = {
    "sensor", "binary_sensor", "weather", "person", "device_tracker",
    "sun", "zone",
}
# Actions the executor/automation builder can map to a service. Anything
# else (e.g. cross-area 'set_state_playing' or numeric sensor states) is junk.
KNOWN_ACTIONS = {"turn_on", "turn_off", "set_state_on", "set_state_off", "currently_on"}
# Hard cost guardrail: max LLM describe calls per mining pass.
DESCRIBE_CAP_PER_RUN = 15
# Sequence pairs firing within this many seconds are one group/scene command
# fanning out across member lights, not human behavior.
SEQUENCE_MIN_DELTA_SECONDS = 6


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
        raw_domains = set(opts.get("domains") or [])
        self._domains: set[str] = raw_domains - NON_ACTIONABLE_DOMAINS
        if raw_domains - self._domains:
            _LOGGER.warning(
                "Ignoring non-actionable domains in config: %s",
                sorted(raw_domains - self._domains),
            )
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
        self._last_mining_summary: str = "not yet run"

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

    _SUFFIX_RE = re.compile(r"_\d+$")

    async def _dedup_mirrors(self, candidates: list) -> list:
        """A switch_as_x wrapper mirrors every state change of its source, so
        light.X and switch.X mine identical patterns — and the registry often
        renames one side (switch.X_2 vs light.X). Group by suffix-normalized
        object_id + action + pattern identity; collapse a light/switch pair to
        the light when the object ids match exactly OR the two entities share
        a registry device (template API lookup, only for suffix-renamed
        pairs)."""
        groups: dict = {}
        for c in candidates:
            _, _, object_id = c.entity_id.partition(".")
            norm = self._SUFFIX_RE.sub("", object_id)
            identity = json.dumps(
                {k: sorted(v) if isinstance(v, list) else v
                 for k, v in c.details.items()},
                sort_keys=True, default=str,
            )
            groups.setdefault(
                (norm, c.action, c.miner_type.value, identity), []
            ).append(c)

        out: list = []
        for group in groups.values():
            if len(group) == 1:
                out.extend(group)
                continue
            lights = [c for c in group if c.entity_id.startswith("light.")]
            switches = [c for c in group if c.entity_id.startswith("switch.")]
            out.extend(
                c for c in group if c not in lights and c not in switches
            )
            if not (lights and switches):
                out.extend(lights + switches)
                continue
            out.extend(lights)
            for sw in switches:
                sw_obj = sw.entity_id.split(".", 1)[1]
                absorbed = False
                for li in lights:
                    li_obj = li.entity_id.split(".", 1)[1]
                    if sw_obj == li_obj:
                        absorbed = True
                        break
                    dev_sw = await self._ha.get_device_id(sw.entity_id)
                    dev_li = await self._ha.get_device_id(li.entity_id)
                    if dev_sw and dev_li and dev_sw == dev_li:
                        absorbed = True
                        break
                if not absorbed:
                    out.append(sw)
        return out

    def _expand_exclusions(self, automated: set[str]) -> set[str]:
        """Grow the automated-entity set with group members and the
        switch_as_x light/switch mirror of each entity — an automation
        targeting a group or wrapper otherwise leaks its members back in
        as 'discovered habits'."""
        states = self._ha.get_states()
        expanded = set(automated)
        for eid in automated:
            members = states.get(eid, {}).get("attributes", {}).get("entity_id")
            if isinstance(members, list):
                expanded.update(m for m in members if isinstance(m, str))
        for eid in list(expanded):
            domain, _, object_id = eid.partition(".")
            if domain in ("light", "switch"):
                expanded.add(f"light.{object_id}")
                expanded.add(f"switch.{object_id}")
        return expanded

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

        automated = self._expand_exclusions(await self._ha.get_automated_entities())
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

        candidates = await self._dedup_mirrors(candidates)
        ingested = 0
        for c in candidates:
            if c.action not in KNOWN_ACTIONS:
                continue
            if not self._domain_ok(c.entity_id) or c.entity_id in automated:
                continue
            if c.miner_type == MinerType.SEQUENCE:
                target = c.details.get("target_entity", "")
                if c.details.get("delta_seconds", 0) < SEQUENCE_MIN_DELTA_SECONDS:
                    continue  # group-command fanout, not a habit
                if target in automated or not self._domain_ok(target):
                    continue
            if passes_emerging(c.occurrences, c.conditional_prob):
                await self._ledger.upsert_evidence(c)
                ingested += 1

        newly = await self._ledger.run_lifecycle(history_days, now)
        _LOGGER.info(
            "mining: %d candidates, %d ingested, %d newly confirmed",
            len(candidates), ingested, len(newly),
        )
        self._last_mining_summary = (
            f"{len(candidates)} candidates, {ingested} ingested, "
            f"{len(newly)} newly confirmed"
        )
        await self._describe_pending()
        await self.refresh_publish()

    async def _describe_pending(self) -> None:
        pending = await self._ledger.needing_description()
        if len(pending) > DESCRIBE_CAP_PER_RUN:
            _LOGGER.warning(
                "describe: %d pending, capping at %d this run (backlog drains hourly)",
                len(pending), DESCRIBE_CAP_PER_RUN,
            )
        for row in pending[:DESCRIBE_CAP_PER_RUN]:
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
        rows = await self._ledger.get_rows(("confirmed", "autopilot"))
        waste_rows, discovery_rows, autopilot_rows = [], [], []
        stale_cutoff = now.timestamp() - 2 * self._waste_interval
        for r in rows:
            if r["miner_type"] == "waste":
                snoozed = r["snoozed_until"] and r["snoozed_until"] > now.timestamp()
                if r["last_seen"] >= stale_cutoff and not snoozed:
                    waste_rows.append(r)
            elif r["lifecycle"] == "autopilot":
                autopilot_rows.append(r)
            else:
                discovery_rows.append(r)

        activity = await self._ledger.recent_activity(
            since_ts=(now - timedelta(hours=24)).timestamp()
        )
        zones = await self._publisher.publish(
            self._now_items, discovery_rows + autopilot_rows, waste_rows, activity
        )
        zones["autopilot"] = [build_payload(r, "autopilot") for r in autopilot_rows]
        emerging = await self._ledger.get_rows(("emerging",))
        zones["emerging"] = [build_payload(r, "emerging") for r in emerging]
        self._ws_server.set_zones(zones)
        counts = await self._ledger.lifecycle_counts()
        if hasattr(self._ws_server, "set_stats"):
            self._ws_server.set_stats({
                "counts": counts,
                "last_mining": self._last_mining_summary,
                "version": ADDON_VERSION,
            })
        await self._ws_server.broadcast_zones()

    async def match_once(self) -> None:
        rows = await self._ledger.get_rows(("confirmed", "autopilot"))
        rows = [r for r in rows if r["miner_type"] != "waste"]
        now = datetime.now(timezone.utc).astimezone(await self._resolve_tz())
        items = self._matcher.match(rows, self._ha.get_states(), now)
        to_execute, to_suggest = partition_matches(items)

        executed_any = False
        for row in to_execute:
            hour_ago = (now - timedelta(hours=1)).timestamp()
            if not within_throttle(await self._ledger.autoruns_since(hour_ago)):
                _LOGGER.warning(
                    "autorun throttle hit — falling back to suggestion: %s",
                    row["signature"],
                )
                to_suggest.append(row)
                continue
            if await self._action_handler.execute_autorun(row, now):
                executed_any = True
            else:
                to_suggest.append(row)

        changed = [r["signature"] for r in to_suggest] != [
            r["signature"] for r in self._now_items
        ]
        self._now_items = to_suggest
        if changed or executed_any:
            await self.refresh_publish()

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
        wiped = await self._ledger.ensure_data_version(4)
        if wiped:
            _LOGGER.info(
                "Ledger data-version reset: wiped %d rows mined before "
                "deduped evidence semantics", wiped,
            )
        purged = await self._ledger.purge_junk(
            allowed_domains=self._domains or None,
            allowed_actions=KNOWN_ACTIONS,
        )
        if purged:
            _LOGGER.info("Purged %d junk ledger rows", purged)
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
