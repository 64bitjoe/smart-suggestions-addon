# Smart Suggestions v4 Phase 1 — Pattern Ledger + Context Matcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the token-burning legacy suggestion pipeline with a pattern-ledger architecture: miners feed a SQLite ledger with lifecycle states, a zero-LLM context matcher surfaces "act now" one-taps, and the LLM is called at most once per newly-confirmed pattern.

**Architecture:** Three loops share one SQLite ledger (`/data/patterns.db`). Hourly: miners → ledger upsert → lifecycle transitions → describe newly-confirmed (LLM once, template fallback). Every 60s: ContextMatcher matches confirmed patterns against live states → `sensor.smart_suggestions_now`. Every 5min: WasteDetector → `noticed` zone (template text only, never LLM). User actions arrive as `smart_suggestions_action` HA events (from the Lovelace card) or `POST /action` (from the ingress panel), both routed to one ActionHandler.

**Tech Stack:** Python 3 (Alpine, asyncio), aiosqlite, aiohttp, anthropic SDK (primary LLM), raw aiohttp for OpenAI-compatible secondary (Ollama/MLX), vanilla-JS custom element for the Lovelace card.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-18-phase1-pattern-ledger-design.md`.
- Flat imports: `src/` is on `sys.path` (see `tests/conftest.py`); import as `from pattern_ledger import PatternLedger`, never `from src.…`.
- Run tests from repo root: `python3 -m pytest smart_suggestions/tests/... -v` (pytest.ini configures rootdir; a `.venv` exists — use `.venv/bin/python -m pytest` if bare `python3` lacks deps).
- No new pip dependencies. Allowed: `anthropic`, `aiosqlite`, `aiohttp`, `yaml` (PyYAML), stdlib. The `openai` pip dep gets REMOVED from the Dockerfile in Task 11.
- Testing is deliberately minimal (owner's call): lifecycle rules, ledger transitions, context-matcher windows, automation-config builder, publisher payload contract. Everything else is verified live against the owner's HA instance.
- Existing miner tests must keep passing untouched.
- Every LLM call site must degrade to deterministic template text — LLM output is polish, never a dependency.
- Async SQLite via aiosqlite; timestamps stored as UTC Unix floats.
- Commit after every task with the `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.
- Version bumps to `4.0.0` in Task 11 (breaking config change).

## File Structure (end state)

```
smart_suggestions/src/
  candidate.py            (unchanged)
  db_reader.py            (unchanged)
  miners/                 (constructor params added, defaults unchanged)
  lifecycle.py            NEW — pure lifecycle/threshold rules
  pattern_ledger.py       NEW — SQLite ledger, absorbs dismissal_store + llm cache
  model_router.py         NEW — primary Anthropic / secondary OpenAI-compatible / RouterError
  llm_describer.py        REWRITTEN — Describer(router) + template_description(), no own cache
  automation_builder.py   REWRITTEN — deterministic pattern→automation config dicts (no LLM)
  context_matcher.py      NEW — 60s relevance matching, suppression
  publisher.py            NEW — ledger rows → sensor payloads (card contract)
  ha_client.py            MODIFIED — +push_sensor/call_service/get_states, −fetch_history legacy
  ha_ws.py                NEW — HA WebSocket event listener with reconnect
  action_handler.py       NEW — run/accept/dismiss/snooze
  ws_server.py            REWRITTEN — slim ingress panel (zones + logs + /action)
  main.py                 REWRITTEN — three loops + listener + card install
smart_suggestions/card/smart-suggestions-card.js   NEW
DELETED: statistical_engine.py, scene_engine.py, narrator.py, pattern_store.py,
         usage_log.py, dismissal_store.py, candidate_filter.py, const.py,
         seed_patterns.json (+ their tests, test_context_builder.py, test_ws_server.py)
```

---

### Task 1: Lifecycle rules (`lifecycle.py`)

**Files:**
- Create: `smart_suggestions/src/lifecycle.py`
- Test: `smart_suggestions/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: nothing (pure functions).
- Produces: `passes_emerging(occurrences: int, prob: float) -> bool`; `confirm_gate(history_days: float, dismissals_7d: int) -> tuple[int, float]`; `passes_confirmed(occurrences: int, prob: float, history_days: float, dismissals_7d: int) -> bool`; `should_resurface(prob: float, prob_at_dismissal: float | None, days_since_dismissal: float) -> bool`. Constants `EMERGING_MIN_OCCURRENCES=2`, `EMERGING_MIN_PROB=0.4`, `RESURFACE_AFTER_DAYS=30`.

- [ ] **Step 1: Write the failing tests**

```python
# smart_suggestions/tests/test_lifecycle.py
from lifecycle import (
    passes_emerging, confirm_gate, passes_confirmed, should_resurface,
)


def test_emerging_gate():
    assert passes_emerging(2, 0.4)
    assert not passes_emerging(1, 0.9)
    assert not passes_emerging(5, 0.39)


def test_confirm_gate_full_history():
    # 30 days of history → the mature bar: 5 occurrences, 0.6 prob
    assert confirm_gate(30.0, 0) == (5, 0.6)


def test_confirm_gate_young_install():
    # Day-5 install → floor of 3 occurrences, not 5
    min_occ, min_prob = confirm_gate(5.0, 0)
    assert min_occ == 3
    assert min_prob == 0.6


def test_confirm_gate_dismissal_bump_and_cap():
    assert confirm_gate(30.0, 3)[1] == 0.65
    assert confirm_gate(30.0, 5)[1] == 0.65      # bump per full group of 3
    assert confirm_gate(30.0, 100)[1] == 0.9     # capped


def test_passes_confirmed_day5_install_confirms_something():
    # The exact failure v3 had: young install, decent evidence → must confirm
    assert passes_confirmed(3, 0.7, history_days=5.0, dismissals_7d=0)
    assert not passes_confirmed(2, 0.7, history_days=5.0, dismissals_7d=0)


def test_should_resurface():
    assert should_resurface(0.75, 0.55, days_since_dismissal=1)   # prob rose ≥0.15
    assert not should_resurface(0.60, 0.55, days_since_dismissal=1)
    assert should_resurface(0.10, 0.55, days_since_dismissal=31)  # 30-day expiry
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lifecycle'`

- [ ] **Step 3: Implement**

```python
# smart_suggestions/src/lifecycle.py
"""Pure lifecycle/threshold rules for the pattern ledger.

Lifecycle states: emerging -> confirmed -> automated | dismissed.
Thresholds adapt to how much recorder history actually exists so a
young install still confirms patterns (the v3 filter never did).
"""
from __future__ import annotations

EMERGING_MIN_OCCURRENCES = 2
EMERGING_MIN_PROB = 0.4
CONFIRM_BASE_PROB = 0.6
MAX_CONFIRM_PROB = 0.9
DISMISSAL_BUMP = 0.05
RESURFACE_PROB_DELTA = 0.15
RESURFACE_AFTER_DAYS = 30

LIFECYCLE_EMERGING = "emerging"
LIFECYCLE_CONFIRMED = "confirmed"
LIFECYCLE_AUTOMATED = "automated"
LIFECYCLE_DISMISSED = "dismissed"


def passes_emerging(occurrences: int, prob: float) -> bool:
    return occurrences >= EMERGING_MIN_OCCURRENCES and prob >= EMERGING_MIN_PROB


def confirm_gate(history_days: float, dismissals_7d: int) -> tuple[int, float]:
    """Return (min_occurrences, min_conditional_prob) for confirmation."""
    h = max(1.0, min(history_days, 30.0))
    min_occ = max(3, round(h / 6))
    min_prob = min(
        MAX_CONFIRM_PROB, CONFIRM_BASE_PROB + (dismissals_7d // 3) * DISMISSAL_BUMP
    )
    return min_occ, min_prob


def passes_confirmed(
    occurrences: int, prob: float, history_days: float, dismissals_7d: int
) -> bool:
    min_occ, min_prob = confirm_gate(history_days, dismissals_7d)
    return occurrences >= min_occ and prob >= min_prob


def should_resurface(
    prob: float, prob_at_dismissal: float | None, days_since_dismissal: float
) -> bool:
    if days_since_dismissal >= RESURFACE_AFTER_DAYS:
        return True
    return prob >= (prob_at_dismissal or 0.0) + RESURFACE_PROB_DELTA
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_lifecycle.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add smart_suggestions/src/lifecycle.py smart_suggestions/tests/test_lifecycle.py
git commit -m "feat: lifecycle rules with history-adaptive confirmation thresholds"
```

---

### Task 2: Pattern ledger (`pattern_ledger.py`)

**Files:**
- Create: `smart_suggestions/src/pattern_ledger.py`
- Test: `smart_suggestions/tests/test_pattern_ledger.py`

**Interfaces:**
- Consumes: `Candidate` (candidate.py), lifecycle functions (Task 1).
- Produces (all async unless noted, rows are plain dicts of the columns):
  - `PatternLedger(db_path: str | Path)`; `await init()`
  - `await upsert_evidence(c: Candidate, initial_lifecycle: str = "emerging") -> None`
  - `await run_lifecycle(history_days: float, now: datetime) -> list[dict]` — applies transitions, returns rows newly moved to `confirmed`
  - `await get_rows(lifecycles: tuple[str, ...]) -> list[dict]`
  - `await get(signature: str) -> dict | None`
  - `await needing_description() -> list[dict]` — confirmed, `title` NULL, `describe_attempts < 3`, `miner_type != 'waste'`
  - `await save_description(sig, title, description, automation_yaml, described_by) -> None`
  - `await bump_describe_attempts(sig) -> None`
  - `await dismiss(sig, now: datetime) -> None`
  - `await snooze(sig, until_ts: float) -> None`
  - `await record_run(sig) -> None`
  - `await mark_automated(sig, automation_id: str) -> bool` — False if already automated (idempotency guard)
- Columns: `signature` PK, `miner_type`, `entity_id`, `action`, `details_json`, `occurrences` INT, `conditional_prob` REAL, `first_seen` REAL, `last_seen` REAL, `lifecycle` TEXT, `title`, `description`, `automation_yaml`, `described_by`, `describe_attempts` INT, `dismiss_count` INT, `dismissed_at` REAL, `prob_at_dismissal` REAL, `snoozed_until` REAL, `accepted_runs` INT, `automation_id` TEXT.

- [ ] **Step 1: Write the failing tests**

```python
# smart_suggestions/tests/test_pattern_ledger.py
import pytest
from datetime import datetime, timedelta, timezone
from candidate import Candidate, MinerType
from pattern_ledger import PatternLedger

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _cand(occ=6, prob=0.8):
    return Candidate(
        miner_type=MinerType.TEMPORAL,
        entity_id="light.porch",
        action="turn_on",
        details={"hour": 20, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
        occurrences=occ,
        conditional_prob=prob,
    )


@pytest.fixture
async def ledger(tmp_path):
    led = PatternLedger(tmp_path / "patterns.db")
    await led.init()
    return led


async def test_upsert_then_confirm(ledger):
    await ledger.upsert_evidence(_cand())
    newly = await ledger.run_lifecycle(history_days=30.0, now=NOW)
    assert [r["signature"] for r in newly] == [_cand().signature()]
    rows = await ledger.get_rows(("confirmed",))
    assert rows[0]["entity_id"] == "light.porch"
    # second run: no re-notification
    assert await ledger.run_lifecycle(30.0, NOW) == []


async def test_upsert_updates_evidence_not_lifecycle(ledger):
    await ledger.upsert_evidence(_cand())
    await ledger.run_lifecycle(30.0, NOW)
    await ledger.upsert_evidence(_cand(occ=7, prob=0.85))
    row = await ledger.get(_cand().signature())
    assert row["occurrences"] == 7
    assert row["lifecycle"] == "confirmed"


async def test_dismiss_blocks_and_resurfaces(ledger):
    await ledger.upsert_evidence(_cand(prob=0.7))
    await ledger.run_lifecycle(30.0, NOW)
    sig = _cand().signature()
    await ledger.dismiss(sig, NOW)
    assert (await ledger.get(sig))["lifecycle"] == "dismissed"
    # stronger evidence within window → resurfaces to emerging
    await ledger.upsert_evidence(_cand(prob=0.9))
    await ledger.run_lifecycle(30.0, NOW + timedelta(days=1))
    assert (await ledger.get(sig))["lifecycle"] in ("emerging", "confirmed")


async def test_dismissals_bump_threshold(ledger):
    # 3 dismissed temporal patterns in window → bar rises to 0.65; a 0.62 stays emerging
    for i in range(3):
        c = _cand()
        c.entity_id = f"light.l{i}"
        await ledger.upsert_evidence(c)
        await ledger.run_lifecycle(30.0, NOW)
        await ledger.dismiss(c.signature(), NOW)
    borderline = _cand(prob=0.62)
    borderline.entity_id = "light.borderline"
    await ledger.upsert_evidence(borderline)
    newly = await ledger.run_lifecycle(30.0, NOW)
    assert newly == []
    assert (await ledger.get(borderline.signature()))["lifecycle"] == "emerging"


async def test_mark_automated_is_idempotent(ledger):
    await ledger.upsert_evidence(_cand())
    await ledger.run_lifecycle(30.0, NOW)
    sig = _cand().signature()
    assert await ledger.mark_automated(sig, "auto_123") is True
    assert await ledger.mark_automated(sig, "auto_456") is False
    assert (await ledger.get(sig))["automation_id"] == "auto_123"
```

Note: async tests need `pytest-asyncio` in auto mode or the repo's existing convention — check `pytest.ini`; the existing suite already has async tests (e.g. test_pattern_store), follow whatever marker/mode they use.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_pattern_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pattern_ledger'`

- [ ] **Step 3: Implement**

```python
# smart_suggestions/src/pattern_ledger.py
"""SQLite pattern ledger — single source of truth for mined patterns.

Absorbs the roles of the old DismissalStore and the LlmDescriber cache.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from candidate import Candidate
from lifecycle import (
    LIFECYCLE_AUTOMATED, LIFECYCLE_CONFIRMED, LIFECYCLE_DISMISSED,
    LIFECYCLE_EMERGING, passes_confirmed, should_resurface,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    signature TEXT PRIMARY KEY,
    miner_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    conditional_prob REAL NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'emerging',
    title TEXT,
    description TEXT,
    automation_yaml TEXT,
    described_by TEXT,
    describe_attempts INTEGER NOT NULL DEFAULT 0,
    dismiss_count INTEGER NOT NULL DEFAULT 0,
    dismissed_at REAL,
    prob_at_dismissal REAL,
    snoozed_until REAL,
    accepted_runs INTEGER NOT NULL DEFAULT 0,
    automation_id TEXT
)
"""

DISMISSAL_BUMP_WINDOW = timedelta(days=7)


class PatternLedger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_SCHEMA)
            await db.commit()

    def _connect(self):
        conn = aiosqlite.connect(self.db_path)
        return conn

    async def upsert_evidence(
        self, c: Candidate, initial_lifecycle: str = LIFECYCLE_EMERGING
    ) -> None:
        now_ts = datetime.now(timezone.utc).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO patterns
                   (signature, miner_type, entity_id, action, details_json,
                    occurrences, conditional_prob, first_seen, last_seen, lifecycle)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(signature) DO UPDATE SET
                    occurrences=excluded.occurrences,
                    conditional_prob=excluded.conditional_prob,
                    details_json=excluded.details_json,
                    last_seen=excluded.last_seen""",
                (
                    c.signature(), c.miner_type.value, c.entity_id, c.action,
                    json.dumps(c.details, sort_keys=True),
                    c.occurrences, c.conditional_prob, now_ts, now_ts,
                    initial_lifecycle,
                ),
            )
            await db.commit()

    async def _dismissals_per_miner(self, db, now: datetime) -> dict[str, int]:
        cutoff = (now - DISMISSAL_BUMP_WINDOW).timestamp()
        cur = await db.execute(
            "SELECT miner_type, COUNT(*) AS n FROM patterns "
            "WHERE dismissed_at IS NOT NULL AND dismissed_at >= ? GROUP BY miner_type",
            (cutoff,),
        )
        return {row[0]: row[1] for row in await cur.fetchall()}

    async def run_lifecycle(self, history_days: float, now: datetime) -> list[dict]:
        newly_confirmed: list[dict] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            bumps = await self._dismissals_per_miner(db, now)
            cur = await db.execute(
                "SELECT * FROM patterns WHERE lifecycle IN (?, ?)",
                (LIFECYCLE_EMERGING, LIFECYCLE_DISMISSED),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            for row in rows:
                d7 = bumps.get(row["miner_type"], 0)
                if row["lifecycle"] == LIFECYCLE_DISMISSED:
                    days_since = (
                        (now.timestamp() - row["dismissed_at"]) / 86400.0
                        if row["dismissed_at"] else 999.0
                    )
                    if should_resurface(
                        row["conditional_prob"], row["prob_at_dismissal"], days_since
                    ):
                        await db.execute(
                            "UPDATE patterns SET lifecycle=? WHERE signature=?",
                            (LIFECYCLE_EMERGING, row["signature"]),
                        )
                        row["lifecycle"] = LIFECYCLE_EMERGING
                    else:
                        continue
                if row["lifecycle"] == LIFECYCLE_EMERGING and passes_confirmed(
                    row["occurrences"], row["conditional_prob"], history_days, d7
                ):
                    await db.execute(
                        "UPDATE patterns SET lifecycle=? WHERE signature=?",
                        (LIFECYCLE_CONFIRMED, row["signature"]),
                    )
                    row["lifecycle"] = LIFECYCLE_CONFIRMED
                    newly_confirmed.append(row)
            await db.commit()
        return newly_confirmed

    async def get_rows(self, lifecycles: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" for _ in lifecycles)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM patterns WHERE lifecycle IN ({marks}) "
                "ORDER BY conditional_prob DESC",
                lifecycles,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get(self, signature: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM patterns WHERE signature=?", (signature,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def needing_description(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM patterns WHERE lifecycle=? AND title IS NULL "
                "AND describe_attempts < 3 AND miner_type != 'waste'",
                (LIFECYCLE_CONFIRMED,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def save_description(
        self, sig: str, title: str, description: str,
        automation_yaml: str, described_by: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET title=?, description=?, automation_yaml=?, "
                "described_by=? WHERE signature=?",
                (title, description, automation_yaml, described_by, sig),
            )
            await db.commit()

    async def bump_describe_attempts(self, sig: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET describe_attempts=describe_attempts+1 "
                "WHERE signature=?", (sig,),
            )
            await db.commit()

    async def dismiss(self, sig: str, now: datetime) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET lifecycle=?, dismiss_count=dismiss_count+1, "
                "dismissed_at=?, prob_at_dismissal=conditional_prob WHERE signature=?",
                (LIFECYCLE_DISMISSED, now.timestamp(), sig),
            )
            await db.commit()

    async def snooze(self, sig: str, until_ts: float) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET snoozed_until=? WHERE signature=?",
                (until_ts, sig),
            )
            await db.commit()

    async def record_run(self, sig: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET accepted_runs=accepted_runs+1 WHERE signature=?",
                (sig,),
            )
            await db.commit()

    async def mark_automated(self, sig: str, automation_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE patterns SET lifecycle=?, automation_id=? "
                "WHERE signature=? AND lifecycle != ?",
                (LIFECYCLE_AUTOMATED, automation_id, sig, LIFECYCLE_AUTOMATED),
            )
            await db.commit()
            return cur.rowcount > 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_pattern_ledger.py smart_suggestions/tests/test_lifecycle.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add smart_suggestions/src/pattern_ledger.py smart_suggestions/tests/test_pattern_ledger.py
git commit -m "feat: PatternLedger — SQLite single source of truth with lifecycle transitions"
```

---

### Task 3: Parametrize miner gates (permissive emission)

The miners hard-code `MIN_OCCURRENCES = 5` / `MIN_CONDITIONAL_PROB = 0.7`, which would starve the ledger — adaptive thresholds can't help if candidates die inside the miner. Add constructor params with defaults equal to today's constants (existing tests stay green); the pipeline will pass permissive values (2 / 0.3).

**Files:**
- Modify: `smart_suggestions/src/miners/temporal.py`
- Modify: `smart_suggestions/src/miners/sequence.py`
- Modify: `smart_suggestions/src/miners/cross_area.py`

**Interfaces:**
- Produces: `TemporalMiner(min_occurrences: int = 5, min_conditional_prob: float = 0.7)`, same for `SequenceMiner` and `CrossAreaMiner`. `run()` signatures unchanged. `WasteDetector` unchanged.

- [ ] **Step 1: Add `__init__` to each miner and replace constant reads with instance attrs**

For each of the three miners, add:

```python
    def __init__(
        self,
        min_occurrences: int = MIN_OCCURRENCES,
        min_conditional_prob: float = MIN_CONDITIONAL_PROB,
    ):
        self.min_occurrences = min_occurrences
        self.min_conditional_prob = min_conditional_prob
```

Then replace uses inside the class:
- `temporal.py`: in `run()` change `if cond_prob < MIN_CONDITIONAL_PROB` → `self.min_conditional_prob`; in `_find_densest_cluster()` change both `len(timestamps) < MIN_OCCURRENCES` and `best_count < MIN_OCCURRENCES` → `self.min_occurrences`.
- `sequence.py`: `if occurrences < MIN_OCCURRENCES or cond_prob < MIN_CONDITIONAL_PROB` → `self.min_occurrences` / `self.min_conditional_prob`.
- `cross_area.py`: same substitution in its gate line.

Module-level constants stay as the defaults.

- [ ] **Step 2: Run existing miner tests untouched**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/miners/ -v`
Expected: all PASS with zero test-file changes

- [ ] **Step 3: Commit**

```bash
git add smart_suggestions/src/miners/
git commit -m "feat: parametrize miner gates so the ledger's adaptive thresholds decide"
```

---

### Task 4: ModelRouter + Describer rewrite

**Files:**
- Create: `smart_suggestions/src/model_router.py`
- Rewrite: `smart_suggestions/src/llm_describer.py`
- Delete: `smart_suggestions/tests/test_llm_describer.py` (tests the old cache design)
- Test: `smart_suggestions/tests/test_describer_template.py`

**Interfaces:**
- Consumes: ledger row dicts (Task 2 columns).
- Produces:
  - `RouterError(Exception)`
  - `ModelRouter(anthropic_client=None, primary_model="claude-haiku-4-5-20251001", secondary_base_url="", secondary_model="", describe_backend="primary")`; `await complete(prompt: str, max_tokens: int = 300) -> str` (raises `RouterError` when all backends fail); `await close()`
  - `Describer(router: ModelRouter)`; `await describe(row: dict) -> tuple[str, str, str]` → `(title, description, described_by)` where `described_by` is `"llm" | "template"` — never raises
  - `template_description(row: dict) -> tuple[str, str]` → deterministic `(title, description)` for all four miner types

- [ ] **Step 1: Write the failing template tests**

```python
# smart_suggestions/tests/test_describer_template.py
import json
from llm_describer import template_description


def _row(miner_type, entity_id, action, details):
    return {
        "miner_type": miner_type, "entity_id": entity_id, "action": action,
        "details_json": json.dumps(details),
        "occurrences": 12, "conditional_prob": 0.87,
    }


def test_temporal_template():
    title, desc = template_description(_row(
        "temporal", "light.porch", "turn_on",
        {"hour": 20, "minute": 5, "weekdays": [0, 1, 2, 3, 4]},
    ))
    assert "porch" in title.lower()
    assert "20:05" in desc and "87%" in desc


def test_waste_template():
    title, desc = template_description(_row(
        "waste", "switch.heater", "currently_on",
        {"condition": "on_duration_anomaly", "duration_seconds": 21600,
         "baseline_seconds": 5400, "since": "2026-07-18T06:00:00+00:00"},
    ))
    assert "heater" in title.lower()
    assert "6.0" in desc and "1.5" in desc  # hours on vs baseline hours


def test_sequence_and_cross_area_templates_do_not_crash():
    template_description(_row("sequence", "light.hall", "turn_on",
        {"target_entity": "light.stairs", "target_action": "turn_on", "delta_seconds": 30}))
    template_description(_row("cross_area", "light.kitchen", "set_state_on",
        {"trigger_entity": "person.joe", "latency_bucket": "0-2m", "latency_seconds": 45}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_describer_template.py -v`
Expected: FAIL — `ImportError: cannot import name 'template_description'`

- [ ] **Step 3: Implement `model_router.py`**

```python
# smart_suggestions/src/model_router.py
"""Two-backend LLM router: primary Anthropic, secondary OpenAI-compatible.

The secondary covers Ollama and MLX (mlx_lm.server / LM Studio) — all speak
POST {base}/v1/chat/completions.
"""
from __future__ import annotations
import asyncio
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class RouterError(Exception):
    """All configured backends failed."""


class ModelRouter:
    def __init__(
        self,
        anthropic_client=None,
        primary_model: str = "claude-haiku-4-5-20251001",
        secondary_base_url: str = "",
        secondary_model: str = "",
        describe_backend: str = "primary",
    ):
        self._anthropic = anthropic_client
        self._primary_model = primary_model
        self._secondary_base_url = secondary_base_url.rstrip("/")
        self._secondary_model = secondary_model
        self._describe_backend = describe_backend
        self._session: aiohttp.ClientSession | None = None

    def _order(self) -> list[str]:
        order = ["primary", "secondary"]
        if self._describe_backend == "secondary":
            order.reverse()
        return order

    async def complete(self, prompt: str, max_tokens: int = 300) -> str:
        errors: list[str] = []
        for backend in self._order():
            try:
                if backend == "primary" and self._anthropic is not None:
                    return await self._call_primary(prompt, max_tokens)
                if backend == "secondary" and self._secondary_base_url:
                    return await self._call_secondary(prompt, max_tokens)
            except Exception as e:
                errors.append(f"{backend}: {e}")
                _LOGGER.warning("LLM backend %s failed: %s", backend, e)
        raise RouterError("; ".join(errors) or "no backend configured")

    async def _call_primary(self, prompt: str, max_tokens: int) -> str:
        resp = await asyncio.wait_for(
            self._anthropic.messages.create(
                model=self._primary_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=30.0,
        )
        return resp.content[0].text

    async def _call_secondary(self, prompt: str, max_tokens: int) -> str:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.post(
            f"{self._secondary_base_url}/v1/chat/completions",
            json={
                "model": self._secondary_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        if self._session:
            await self._session.close()
```

- [ ] **Step 4: Rewrite `llm_describer.py`**

Replace the whole file:

```python
# smart_suggestions/src/llm_describer.py
"""Turn a confirmed ledger row into user-facing title + description.

LLM output is polish only — template_description() always works and is the
fallback (and the only path for waste alerts).
"""
from __future__ import annotations
import json
import logging
import re

from model_router import ModelRouter, RouterError

_LOGGER = logging.getLogger(__name__)

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _friendly(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def _weekdays_text(weekdays: list[int]) -> str:
    if sorted(weekdays) == [0, 1, 2, 3, 4]:
        return "weekdays"
    if sorted(weekdays) == [5, 6]:
        return "weekends"
    if len(weekdays) >= 7:
        return "every day"
    return "/".join(_WEEKDAY_NAMES[d] for d in sorted(weekdays))


def template_description(row: dict) -> tuple[str, str]:
    d = json.loads(row["details_json"])
    name = _friendly(row["entity_id"])
    verb = "Turn on" if "on" in row["action"] else "Turn off"
    pct = f"{row['conditional_prob']:.0%}"
    mt = row["miner_type"]
    if mt == "temporal":
        t = f"{d['hour']:02d}:{d['minute']:02d}"
        title = f"{verb} {name} at {t}"
        desc = (f"You do this around {t} on {_weekdays_text(d['weekdays'])} — "
                f"{pct} of the time ({row['occurrences']} times).")
    elif mt == "sequence":
        tgt = _friendly(d["target_entity"])
        title = f"{tgt} follows {name}"
        desc = (f"When {name} turns on, {tgt} usually turns on within "
                f"{d['delta_seconds']}s — {pct} of the time.")
    elif mt == "cross_area":
        trig = _friendly(d["trigger_entity"])
        title = f"{verb} {name} when {trig} arrives"
        desc = (f"After {trig} shows up, {name} usually changes within "
                f"{d['latency_bucket']} — {pct} of the time.")
    else:  # waste
        hours = d["duration_seconds"] / 3600.0
        base = d["baseline_seconds"] / 3600.0
        title = f"{name} left on?"
        desc = (f"{name} has been on for {hours:.1f} h — "
                f"it usually runs about {base:.1f} h.")
    return title, desc


def _build_prompt(row: dict) -> str:
    return f"""You write one Home Assistant suggestion for a statistically-validated pattern.
Pattern type: {row['miner_type']}
Entity: {row['entity_id']}
Action: {row['action']}
Details: {row['details_json']}
Occurrences: {row['occurrences']}, conditional probability: {row['conditional_prob']:.2f}

Respond with ONLY a JSON object: {{"title": "≤60 chars, friendly", "description": "one warm, concrete sentence"}}"""


def _strip_json_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


class Describer:
    def __init__(self, router: ModelRouter):
        self._router = router

    async def describe(self, row: dict) -> tuple[str, str, str]:
        """Return (title, description, described_by). Never raises."""
        try:
            text = await self._router.complete(_build_prompt(row), max_tokens=300)
            payload = json.loads(_strip_json_fences(text))
            return str(payload["title"]), str(payload["description"]), "llm"
        except (RouterError, KeyError, ValueError, TypeError) as e:
            _LOGGER.warning("describe fell back to template: %s", e)
            title, desc = template_description(row)
            return title, desc, "template"
```

- [ ] **Step 5: Delete the old describer tests, run new ones**

```bash
git rm smart_suggestions/tests/test_llm_describer.py
```

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_describer_template.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add -A smart_suggestions/src/model_router.py smart_suggestions/src/llm_describer.py smart_suggestions/tests/
git commit -m "feat: ModelRouter (Anthropic + OpenAI-compatible local) and template-first Describer"
```

---

### Task 5: Deterministic automation builder rewrite

The old builder asked the LLM to write YAML (fragile, token-costly). Automation configs are fully determined by pattern data — build the dict directly, zero LLM. The old `build()` API and its LLM client die here.

**Files:**
- Rewrite: `smart_suggestions/src/automation_builder.py`
- Rewrite: `smart_suggestions/tests/test_automation_builder.py`

**Interfaces:**
- Consumes: ledger row dicts.
- Produces:
  - `build_pattern_automation(row: dict) -> dict | None` — pure; returns an HA automation config dict, or `None` for non-automatable rows (waste)
  - `async create_pattern_automation(row: dict, ha_client) -> dict` — builds + POSTs via `ha_client.create_automation(config)`; returns that result dict (`{"success": bool, ...}`)

- [ ] **Step 1: Write the failing tests** (replace the whole existing test file)

```python
# smart_suggestions/tests/test_automation_builder.py
import json
from automation_builder import build_pattern_automation


def _row(miner_type, entity_id, action, details, title="My Pattern"):
    return {
        "miner_type": miner_type, "entity_id": entity_id, "action": action,
        "details_json": json.dumps(details), "title": title,
    }


def test_temporal_automation():
    cfg = build_pattern_automation(_row(
        "temporal", "light.porch", "turn_on",
        {"hour": 20, "minute": 5, "weekdays": [0, 1, 2, 3, 4]},
    ))
    assert cfg["trigger"] == [{"platform": "time", "at": "20:05:00"}]
    assert cfg["condition"] == [{"condition": "time",
        "weekday": ["mon", "tue", "wed", "thu", "fri"]}]
    assert cfg["action"] == [{"service": "homeassistant.turn_on",
        "target": {"entity_id": "light.porch"}}]
    assert cfg["mode"] == "single"


def test_temporal_every_day_has_no_weekday_condition():
    cfg = build_pattern_automation(_row(
        "temporal", "light.porch", "turn_on",
        {"hour": 7, "minute": 0, "weekdays": [0, 1, 2, 3, 4, 5, 6]},
    ))
    assert cfg["condition"] == []


def test_sequence_automation_targets_follower():
    cfg = build_pattern_automation(_row(
        "sequence", "light.hall", "turn_on",
        {"target_entity": "light.stairs", "target_action": "turn_on",
         "delta_seconds": 30},
    ))
    assert cfg["trigger"] == [{"platform": "state", "entity_id": "light.hall",
        "to": "on"}]
    assert cfg["action"][0]["target"]["entity_id"] == "light.stairs"


def test_cross_area_person_triggers_on_home():
    cfg = build_pattern_automation(_row(
        "cross_area", "light.kitchen", "set_state_on",
        {"trigger_entity": "person.joe", "latency_bucket": "0-2m",
         "latency_seconds": 45},
    ))
    assert cfg["trigger"] == [{"platform": "state", "entity_id": "person.joe",
        "to": "home"}]
    assert cfg["action"] == [{"service": "homeassistant.turn_on",
        "target": {"entity_id": "light.kitchen"}}]


def test_waste_not_automatable():
    assert build_pattern_automation(_row(
        "waste", "switch.heater", "currently_on",
        {"condition": "on_duration_anomaly"},
    )) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_automation_builder.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_pattern_automation'`

- [ ] **Step 3: Rewrite `automation_builder.py`** (replace the whole file)

```python
# smart_suggestions/src/automation_builder.py
"""Deterministic pattern → HA automation config. No LLM involved."""
from __future__ import annotations
import json
import logging

_LOGGER = logging.getLogger(__name__)

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _service_for(action: str) -> str | None:
    if action in ("turn_on", "set_state_on"):
        return "homeassistant.turn_on"
    if action in ("turn_off", "set_state_off"):
        return "homeassistant.turn_off"
    return None


def _action_step(action: str, entity_id: str) -> list[dict] | None:
    service = _service_for(action)
    if service is None:
        return None
    return [{"service": service, "target": {"entity_id": entity_id}}]


def build_pattern_automation(row: dict) -> dict | None:
    d = json.loads(row["details_json"])
    mt = row["miner_type"]
    alias = row.get("title") or f"Smart Suggestion: {row['entity_id']}"

    if mt == "temporal":
        steps = _action_step(row["action"], row["entity_id"])
        if steps is None:
            return None
        weekdays = sorted(d.get("weekdays", []))
        condition = (
            [] if len(weekdays) >= 7
            else [{"condition": "time", "weekday": [_WEEKDAYS[w] for w in weekdays]}]
        )
        return {
            "alias": alias,
            "trigger": [{"platform": "time", "at": f"{d['hour']:02d}:{d['minute']:02d}:00"}],
            "condition": condition,
            "action": steps,
            "mode": "single",
        }

    if mt == "sequence":
        steps = _action_step(d["target_action"], d["target_entity"])
        if steps is None:
            return None
        return {
            "alias": alias,
            "trigger": [{"platform": "state", "entity_id": row["entity_id"], "to": "on"}],
            "condition": [],
            "action": steps,
            "mode": "single",
        }

    if mt == "cross_area":
        steps = _action_step(row["action"], row["entity_id"])
        if steps is None:
            return None
        trig = d["trigger_entity"]
        to_state = (
            "home" if trig.startswith(("person.", "device_tracker.")) else "on"
        )
        return {
            "alias": alias,
            "trigger": [{"platform": "state", "entity_id": trig, "to": to_state}],
            "condition": [],
            "action": steps,
            "mode": "single",
        }

    return None  # waste and anything unknown: not automatable


async def create_pattern_automation(row: dict, ha_client) -> dict:
    config = build_pattern_automation(row)
    if config is None:
        return {"success": False, "error": "pattern is not automatable"}
    result = await ha_client.create_automation(config)
    if not result.get("success"):
        _LOGGER.warning("create_pattern_automation failed: %s", result.get("error"))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_automation_builder.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add smart_suggestions/src/automation_builder.py smart_suggestions/tests/test_automation_builder.py
git commit -m "feat: deterministic pattern→automation config builder (LLM removed)"
```

---

### Task 6: ContextMatcher (`context_matcher.py`)

**Files:**
- Create: `smart_suggestions/src/context_matcher.py`
- Test: `smart_suggestions/tests/test_context_matcher.py`

**Interfaces:**
- Consumes: confirmed ledger row dicts; HA states dict (`{entity_id: {"state": str, "last_changed": iso-str, ...}}` — the shape `HAClient._states` holds).
- Produces:
  - `ContextMatcher(max_now: int = 5)`
  - `match(rows: list[dict], states: dict, now: datetime) -> list[dict]` — synchronous, pure except suppression state; returns rows augmented with `"act_entity"` and `"act_action"`, best-first by `conditional_prob`, at most `max_now`
  - `suppress(signature: str, until: datetime) -> None` — hides a suggestion (after run/dismiss) until `until`
- Match rules: temporal = now within ±30 min of learned time, weekday matches, entity not already in target state. sequence = trigger entity currently "on" and changed within `delta_seconds + 120`s, follower not already on. cross_area = trigger entity in ("home","on") and changed within 300s, target not already in desired state.

- [ ] **Step 1: Write the failing tests**

```python
# smart_suggestions/tests/test_context_matcher.py
import json
from datetime import datetime, timedelta, timezone
from context_matcher import ContextMatcher

# Friday 2026-07-17 19:55 UTC (weekday()==4)
NOW = datetime(2026, 7, 17, 19, 55, tzinfo=timezone.utc)


def _state(state, changed_ago_s=0):
    return {
        "state": state,
        "last_changed": (NOW - timedelta(seconds=changed_ago_s)).isoformat(),
    }


def _temporal_row(sig="t1", hour=20, weekdays=(0, 1, 2, 3, 4), prob=0.9):
    return {
        "signature": sig, "miner_type": "temporal", "entity_id": "light.porch",
        "action": "turn_on", "conditional_prob": prob, "occurrences": 10,
        "title": "Porch", "description": "",
        "details_json": json.dumps(
            {"hour": hour, "minute": 0, "weekdays": list(weekdays)}),
    }


def test_temporal_matches_within_window_when_off():
    m = ContextMatcher()
    out = m.match([_temporal_row()], {"light.porch": _state("off")}, NOW)
    assert len(out) == 1
    assert out[0]["act_entity"] == "light.porch"
    assert out[0]["act_action"] == "turn_on"


def test_temporal_skips_wrong_weekday_wrong_time_or_already_on():
    m = ContextMatcher()
    assert m.match([_temporal_row(weekdays=(5, 6))],
                   {"light.porch": _state("off")}, NOW) == []
    assert m.match([_temporal_row(hour=9)],
                   {"light.porch": _state("off")}, NOW) == []
    assert m.match([_temporal_row()],
                   {"light.porch": _state("on")}, NOW) == []


def test_sequence_matches_recent_trigger():
    row = {
        "signature": "s1", "miner_type": "sequence", "entity_id": "light.hall",
        "action": "turn_on", "conditional_prob": 0.8, "occurrences": 8,
        "title": "", "description": "",
        "details_json": json.dumps({"target_entity": "light.stairs",
            "target_action": "turn_on", "delta_seconds": 30}),
    }
    states = {"light.hall": _state("on", changed_ago_s=20),
              "light.stairs": _state("off")}
    out = ContextMatcher().match([row], states, NOW)
    assert out[0]["act_entity"] == "light.stairs"
    # trigger too old → no match
    states["light.hall"] = _state("on", changed_ago_s=1000)
    assert ContextMatcher().match([row], states, NOW) == []


def test_suppression_hides_then_expires():
    m = ContextMatcher()
    states = {"light.porch": _state("off")}
    m.suppress("t1", NOW + timedelta(hours=1))
    assert m.match([_temporal_row()], states, NOW) == []
    assert len(m.match([_temporal_row()], states, NOW + timedelta(hours=2))) == 1


def test_max_now_caps_and_ranks():
    rows = [_temporal_row(sig=f"t{i}", prob=0.5 + i * 0.05) for i in range(8)]
    for i, r in enumerate(rows):
        r["entity_id"] = f"light.l{i}"
    states = {f"light.l{i}": _state("off") for i in range(8)}
    out = ContextMatcher(max_now=5).match(rows, states, NOW)
    assert len(out) == 5
    assert out[0]["conditional_prob"] >= out[-1]["conditional_prob"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_context_matcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'context_matcher'`

- [ ] **Step 3: Implement**

```python
# smart_suggestions/src/context_matcher.py
"""Match confirmed patterns against the current moment. Pure Python, no LLM."""
from __future__ import annotations
import json
import logging
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

TEMPORAL_WINDOW_MINUTES = 30
CROSS_AREA_WINDOW_SECONDS = 300
SEQUENCE_GRACE_SECONDS = 120

_DESIRED_STATE = {"turn_on": "on", "set_state_on": "on",
                  "turn_off": "off", "set_state_off": "off"}


def _entity_state(states: dict, eid: str) -> str | None:
    s = states.get(eid)
    return s.get("state") if s else None


def _changed_ago_seconds(states: dict, eid: str, now: datetime) -> float | None:
    s = states.get(eid)
    if not s or not s.get("last_changed"):
        return None
    try:
        changed = datetime.fromisoformat(s["last_changed"].replace("Z", "+00:00"))
        return (now - changed).total_seconds()
    except (ValueError, TypeError):
        return None


class ContextMatcher:
    def __init__(self, max_now: int = 5):
        self.max_now = max_now
        self._suppressed: dict[str, datetime] = {}

    def suppress(self, signature: str, until: datetime) -> None:
        self._suppressed[signature] = until

    def _is_suppressed(self, signature: str, now: datetime) -> bool:
        until = self._suppressed.get(signature)
        if until is None:
            return False
        if now >= until:
            del self._suppressed[signature]
            return False
        return True

    def match(self, rows: list[dict], states: dict, now: datetime) -> list[dict]:
        matched: list[dict] = []
        for row in rows:
            if self._is_suppressed(row["signature"], now):
                continue
            try:
                d = json.loads(row["details_json"])
                act = self._match_one(row, d, states, now)
            except Exception:
                _LOGGER.exception("matcher error on %s", row.get("signature"))
                continue
            if act is not None:
                enriched = dict(row)
                enriched["act_entity"], enriched["act_action"] = act
                matched.append(enriched)
        matched.sort(key=lambda r: r["conditional_prob"], reverse=True)
        return matched[: self.max_now]

    def _match_one(
        self, row: dict, d: dict, states: dict, now: datetime
    ) -> tuple[str, str] | None:
        mt = row["miner_type"]
        if mt == "temporal":
            minute_now = now.hour * 60 + now.minute
            minute_pat = d["hour"] * 60 + d["minute"]
            if abs(minute_now - minute_pat) > TEMPORAL_WINDOW_MINUTES:
                return None
            if now.weekday() not in d.get("weekdays", []):
                return None
            desired = _DESIRED_STATE.get(row["action"])
            if desired and _entity_state(states, row["entity_id"]) == desired:
                return None
            return row["entity_id"], row["action"]

        if mt == "sequence":
            ago = _changed_ago_seconds(states, row["entity_id"], now)
            if ago is None or _entity_state(states, row["entity_id"]) != "on":
                return None
            if ago > d["delta_seconds"] + SEQUENCE_GRACE_SECONDS:
                return None
            desired = _DESIRED_STATE.get(d["target_action"], "on")
            if _entity_state(states, d["target_entity"]) == desired:
                return None
            return d["target_entity"], d["target_action"]

        if mt == "cross_area":
            trig = d["trigger_entity"]
            if _entity_state(states, trig) not in ("home", "on"):
                return None
            ago = _changed_ago_seconds(states, trig, now)
            if ago is None or ago > CROSS_AREA_WINDOW_SECONDS:
                return None
            desired = _DESIRED_STATE.get(row["action"])
            if desired and _entity_state(states, row["entity_id"]) == desired:
                return None
            return row["entity_id"], row["action"]

        return None  # waste is never a "now" one-tap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_context_matcher.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add smart_suggestions/src/context_matcher.py smart_suggestions/tests/test_context_matcher.py
git commit -m "feat: ContextMatcher — zero-LLM 'act now' relevance matching"
```

---

### Task 7: HAClient additions + Publisher with payload contract

**Files:**
- Modify: `smart_suggestions/src/ha_client.py`
- Create: `smart_suggestions/src/publisher.py`
- Test: `smart_suggestions/tests/test_publisher_contract.py`
- Modify (possibly): `smart_suggestions/tests/test_ha_client.py`

**Interfaces:**
- Consumes: ledger rows / matcher output.
- Produces:
  - `HAClient.get_states() -> dict` (sync accessor for `self._states`)
  - `await HAClient.push_sensor(entity_id: str, state: str, attributes: dict) -> None`
  - `await HAClient.call_service(domain: str, service: str, entity_id: str) -> bool`
  - `publisher.build_payload(row: dict, zone: str) -> dict` — pure; **THE card contract**, keys: `signature, zone, title, description, entity_id, act_entity, act_action, miner_type, confidence, occurrences, can_automate`
  - `Publisher(ha_client)`; `await publish(now_items, discovery_rows, noticed_rows) -> dict` — pushes all three sensors and returns `{"now": [...], "discoveries": [...], "noticed": [...]}` (also handed to the ingress panel)
- Sensor names: `sensor.smart_suggestions_now`, `sensor.smart_suggestions_discoveries`, `sensor.smart_suggestions_noticed`; sensor `state` = item count, attributes `{"suggestions": [...], "friendly_name": ...}`.

- [ ] **Step 1: Write the failing contract test**

```python
# smart_suggestions/tests/test_publisher_contract.py
import json
from publisher import build_payload

# The card and panel render exactly these keys. If this contract breaks,
# suggestions silently vanish — which is precisely how v3 died.
CARD_CONTRACT = {
    "signature", "zone", "title", "description", "entity_id",
    "act_entity", "act_action", "miner_type", "confidence",
    "occurrences", "can_automate",
}


def _row(miner_type="temporal", title="Porch at 20:00", act=None):
    row = {
        "signature": "abc", "miner_type": miner_type,
        "entity_id": "light.porch", "action": "turn_on",
        "details_json": json.dumps(
            {"hour": 20, "minute": 0, "weekdays": [0, 1, 2, 3, 4]}),
        "occurrences": 9, "conditional_prob": 0.83,
        "title": title, "description": "desc",
    }
    if act:
        row["act_entity"], row["act_action"] = act
    return row


def test_payload_matches_card_contract_exactly():
    p = build_payload(_row(act=("light.porch", "turn_on")), zone="now")
    assert set(p.keys()) == CARD_CONTRACT
    assert p["zone"] == "now"
    assert p["confidence"] == 0.83
    assert p["can_automate"] is True


def test_payload_without_matcher_enrichment_defaults_act_to_entity():
    p = build_payload(_row(), zone="discoveries")
    assert p["act_entity"] == "light.porch"
    assert p["act_action"] == "turn_on"


def test_untitled_row_gets_template_title():
    p = build_payload(_row(title=None), zone="discoveries")
    assert p["title"]  # template fallback, never empty


def test_waste_rows_cannot_automate():
    row = _row(miner_type="waste")
    row["action"] = "currently_on"
    row["details_json"] = json.dumps(
        {"condition": "on_duration_anomaly", "duration_seconds": 7200,
         "baseline_seconds": 1800, "since": "2026-07-18T06:00:00+00:00"})
    p = build_payload(row, zone="noticed")
    assert p["can_automate"] is False
    assert p["act_action"] == "turn_off"  # the fix for a waste alert
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_publisher_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'publisher'`

- [ ] **Step 3: Implement `publisher.py`**

```python
# smart_suggestions/src/publisher.py
"""Ledger rows → HA sensor payloads. build_payload() is the card contract."""
from __future__ import annotations
import logging

from llm_describer import template_description

_LOGGER = logging.getLogger(__name__)

SENSOR_NOW = "sensor.smart_suggestions_now"
SENSOR_DISCOVERIES = "sensor.smart_suggestions_discoveries"
SENSOR_NOTICED = "sensor.smart_suggestions_noticed"


def build_payload(row: dict, zone: str) -> dict:
    title = row.get("title")
    description = row.get("description")
    if not title:
        title, tdesc = template_description(row)
        description = description or tdesc
    act_entity = row.get("act_entity", row["entity_id"])
    act_action = row.get("act_action", row["action"])
    if row["miner_type"] == "waste":
        act_action = "turn_off"
    return {
        "signature": row["signature"],
        "zone": zone,
        "title": title,
        "description": description or "",
        "entity_id": row["entity_id"],
        "act_entity": act_entity,
        "act_action": act_action,
        "miner_type": row["miner_type"],
        "confidence": row["conditional_prob"],
        "occurrences": row["occurrences"],
        "can_automate": row["miner_type"] != "waste",
    }


class Publisher:
    def __init__(self, ha_client):
        self._ha = ha_client

    async def publish(
        self, now_items: list[dict], discovery_rows: list[dict],
        noticed_rows: list[dict],
    ) -> dict:
        zones = {
            "now": [build_payload(r, "now") for r in now_items],
            "discoveries": [build_payload(r, "discoveries") for r in discovery_rows],
            "noticed": [build_payload(r, "noticed") for r in noticed_rows],
        }
        for sensor, key, name in (
            (SENSOR_NOW, "now", "Smart Suggestions: Now"),
            (SENSOR_DISCOVERIES, "discoveries", "Smart Suggestions: Discoveries"),
            (SENSOR_NOTICED, "noticed", "Smart Suggestions: Noticed"),
        ):
            await self._ha.push_sensor(
                sensor, str(len(zones[key])),
                {"suggestions": zones[key], "friendly_name": name},
            )
        return zones
```

- [ ] **Step 4: Add HAClient methods, remove legacy history fetchers**

In `smart_suggestions/src/ha_client.py`:

1. Delete methods `fetch_history`, `_fetch_history_from_db`, `_fetch_history_rest`, the `_DB_PATHS` list, and the `write_suggestions` alias (the `from const import …` line goes with them). Keep `write_suggestions_state` deleted too — the sensors replace it. Keep `send_notification`, `get_automations`, `get_current_on_states`, `get_automated_entities`, `create_automation`.
2. Add:

```python
    def get_states(self) -> dict:
        """Current entity states as {entity_id: state_dict}. Kept fresh by the poller."""
        return self._states

    async def push_sensor(self, entity_id: str, state: str, attributes: dict) -> None:
        if not self._session:
            return
        try:
            async with self._session.post(
                f"{self._base}/states/{entity_id}",
                json={"state": state, "attributes": attributes},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("push_sensor %s: HTTP %s", entity_id, resp.status)
        except Exception as e:
            _LOGGER.warning("push_sensor %s failed: %s", entity_id, e)

    async def call_service(self, domain: str, service: str, entity_id: str) -> bool:
        if not self._session:
            return False
        try:
            async with self._session.post(
                f"{self._base}/services/{domain}/{service}",
                json={"entity_id": entity_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            _LOGGER.warning("call_service %s.%s failed: %s", domain, service, e)
            return False
```

- [ ] **Step 5: Run tests; prune test_ha_client of deleted-method tests**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_publisher_contract.py smart_suggestions/tests/test_ha_client.py -v`
Expected: contract tests PASS. If any `test_ha_client.py` tests reference the deleted `fetch_history`/`write_suggestions*` methods, delete those test functions (only those) and re-run to green.

- [ ] **Step 6: Commit**

```bash
git add -A smart_suggestions/src/ha_client.py smart_suggestions/src/publisher.py smart_suggestions/tests/
git commit -m "feat: Publisher with card payload contract; HAClient sensor push + service call"
```

---

### Task 8: HA event listener + ActionHandler

No unit tests (owner's minimal-testing call — idempotency is already covered by the ledger test; this is I/O glue verified live).

**Files:**
- Create: `smart_suggestions/src/ha_ws.py`
- Create: `smart_suggestions/src/action_handler.py`

**Interfaces:**
- Consumes: `PatternLedger` (Task 2), `HAClient.call_service` (Task 7), `create_pattern_automation` (Task 5), `ContextMatcher.suppress` (Task 6).
- Produces:
  - `HAEventListener(on_event, ha_url: str = "", ha_token: str = "", event_type: str = "smart_suggestions_action")`; `await run()` — runs forever with reconnect backoff, calls `await on_event(event_data_dict)`
  - `ActionHandler(ledger, ha_client, matcher, refresh_cb)`; `await handle(data: dict)` where `data = {"action": "run|accept|dismiss|snooze", "signature": str}`; `refresh_cb` is an async no-arg callable that republishes all zones.

- [ ] **Step 1: Implement `ha_ws.py`**

```python
# smart_suggestions/src/ha_ws.py
"""Listen for smart_suggestions_action events on the HA WebSocket API."""
from __future__ import annotations
import asyncio
import logging
import os

import aiohttp

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_WS = "ws://supervisor/core/websocket"


class HAEventListener:
    def __init__(
        self,
        on_event,
        ha_url: str = "",
        ha_token: str = "",
        event_type: str = "smart_suggestions_action",
    ):
        self._on_event = on_event
        self._event_type = event_type
        if ha_url and ha_token:
            base = ha_url.rstrip("/")
            base = base.replace("https://", "wss://").replace("http://", "ws://")
            self._url = f"{base}/api/websocket"
            self._token = ha_token
        else:
            self._url = _SUPERVISOR_WS
            self._token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        backoff = 1
        while self._running:
            try:
                await self._connect_once()
                backoff = 1
            except Exception as e:
                _LOGGER.warning("HA WS listener disconnected: %s", e)
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self._url, heartbeat=30) as ws:
                msg = await ws.receive_json()  # auth_required
                if msg.get("type") == "auth_required":
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"HA WS auth failed: {msg}")
                await ws.send_json({
                    "id": 1, "type": "subscribe_events",
                    "event_type": self._event_type,
                })
                _LOGGER.info("Listening for %s events", self._event_type)
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = raw.json()
                    if data.get("type") == "event":
                        event_data = data["event"].get("data", {})
                        try:
                            await self._on_event(event_data)
                        except Exception:
                            _LOGGER.exception("action event handler failed")
```

- [ ] **Step 2: Implement `action_handler.py`**

```python
# smart_suggestions/src/action_handler.py
"""Handle user actions on suggestions: run / accept / dismiss / snooze."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone

from automation_builder import create_pattern_automation

_LOGGER = logging.getLogger(__name__)

RUN_SUPPRESS = timedelta(hours=2)
SNOOZE_FOR = timedelta(hours=24)

_SERVICE = {"turn_on": "turn_on", "set_state_on": "turn_on",
            "turn_off": "turn_off", "set_state_off": "turn_off",
            "currently_on": "turn_off"}


class ActionHandler:
    def __init__(self, ledger, ha_client, matcher, refresh_cb):
        self._ledger = ledger
        self._ha = ha_client
        self._matcher = matcher
        self._refresh = refresh_cb

    async def handle(self, data: dict) -> None:
        action = data.get("action")
        sig = data.get("signature")
        if not action or not sig:
            return
        row = await self._ledger.get(sig)
        if row is None:
            _LOGGER.warning("action %s for unknown signature %s", action, sig)
            return
        now = datetime.now(timezone.utc)

        if action == "run":
            act_entity, act_action = self._resolve_act(row)
            service = _SERVICE.get(act_action)
            if service:
                ok = await self._ha.call_service("homeassistant", service, act_entity)
                if ok:
                    await self._ledger.record_run(sig)
            self._matcher.suppress(sig, now + RUN_SUPPRESS)
        elif action == "accept":
            if row["lifecycle"] == "automated":
                return
            result = await create_pattern_automation(row, self._ha)
            if result.get("success"):
                await self._ledger.mark_automated(
                    sig, result.get("automation_id", ""))
        elif action == "dismiss":
            await self._ledger.dismiss(sig, now)
            self._matcher.suppress(sig, now + timedelta(days=14))
        elif action == "snooze":
            await self._ledger.snooze(sig, (now + SNOOZE_FOR).timestamp())
        else:
            _LOGGER.warning("unknown action: %s", action)
            return
        await self._refresh()

    @staticmethod
    def _resolve_act(row: dict) -> tuple[str, str]:
        if row["miner_type"] == "sequence":
            d = json.loads(row["details_json"])
            return d["target_entity"], d["target_action"]
        return row["entity_id"], row["action"]
```

- [ ] **Step 3: Sanity-run full suite (imports still clean)**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/ -v --ignore=smart_suggestions/tests/test_ws_server.py --ignore=smart_suggestions/tests/test_main_smoke.py --ignore=smart_suggestions/tests/test_context_builder.py`
Expected: PASS (legacy-coupled tests excluded; they die in Tasks 10–11)

- [ ] **Step 4: Commit**

```bash
git add smart_suggestions/src/ha_ws.py smart_suggestions/src/action_handler.py
git commit -m "feat: HA event listener + ActionHandler for run/accept/dismiss/snooze"
```

---

### Task 9: Lovelace card

**Files:**
- Create: `smart_suggestions/card/smart-suggestions-card.js`

**Interfaces:**
- Consumes: the three sensors' `attributes.suggestions` arrays (payload contract from Task 7).
- Produces: custom element `smart-suggestions-card`; sends actions via `hass.callApi("POST", "events/smart_suggestions_action", {action, signature})`. Installed by main.py copying to `/config/www/` (Task 11); user registers `/local/smart-suggestions-card.js` as a dashboard resource once (instructions shown in panel).

- [ ] **Step 1: Write the card**

```javascript
// smart_suggestions/card/smart-suggestions-card.js
// Lovelace card for the Smart Suggestions add-on.
// Resource: /local/smart-suggestions-card.js  (type: JavaScript Module)
// Card config: - type: custom:smart-suggestions-card

const SENSORS = {
  now: "sensor.smart_suggestions_now",
  discoveries: "sensor.smart_suggestions_discoveries",
  noticed: "sensor.smart_suggestions_noticed",
};
const ZONE_TITLES = { now: "Right now", discoveries: "Discovered patterns", noticed: "Noticed" };

class SmartSuggestionsCard extends HTMLElement {
  setConfig(config) { this._config = config; }
  getCardSize() { return 4; }

  set hass(hass) {
    this._hass = hass;
    const key = Object.values(SENSORS)
      .map((id) => hass.states[id]?.last_updated ?? "")
      .join("|");
    if (key === this._renderedKey) return;
    this._renderedKey = key;
    this._render();
  }

  _suggestions(zone) {
    const st = this._hass.states[SENSORS[zone]];
    return (st && st.attributes.suggestions) || [];
  }

  _fire(action, signature) {
    this._hass.callApi("POST", "events/smart_suggestions_action", {
      action, signature,
    });
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const zones = ["now", "noticed", "discoveries"];
    const sections = zones
      .map((zone) => {
        const items = this._suggestions(zone);
        if (!items.length) return "";
        const rows = items
          .map(
            (s, i) => `
          <div class="item">
            <div class="text">
              <div class="title">${s.title}</div>
              <div class="desc">${s.description}</div>
            </div>
            <div class="btns" data-zone="${zone}" data-i="${i}">
              ${zone !== "discoveries" ? `<button class="run">Run</button>` : ""}
              ${s.can_automate ? `<button class="accept">Automate</button>` : ""}
              ${zone === "noticed" ? `<button class="snooze">Snooze</button>` : ""}
              <button class="dismiss">✕</button>
            </div>
          </div>`
          )
          .join("");
        return `<div class="zone"><h3>${ZONE_TITLES[zone]}</h3>${rows}</div>`;
      })
      .join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px 16px; }
        h3 { margin: 8px 0 4px; font-size: 0.85em; text-transform: uppercase;
             letter-spacing: 0.06em; color: var(--secondary-text-color); }
        .item { display: flex; align-items: center; gap: 8px;
                padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
        .item:last-child { border-bottom: none; }
        .text { flex: 1; min-width: 0; }
        .title { font-weight: 500; }
        .desc { font-size: 0.85em; color: var(--secondary-text-color); }
        .btns { display: flex; gap: 4px; flex-shrink: 0; }
        button { border: none; border-radius: 12px; padding: 6px 10px;
                 cursor: pointer; font: inherit; font-size: 0.8em;
                 background: var(--secondary-background-color);
                 color: var(--primary-text-color); }
        button.run, button.accept { background: var(--primary-color); color: #fff; }
        .empty { color: var(--secondary-text-color); padding: 12px 0; }
      </style>
      <ha-card header="Smart Suggestions">
        ${sections || `<div class="empty">Nothing to suggest right now — still learning your patterns.</div>`}
      </ha-card>`;

    this.shadowRoot.querySelectorAll(".btns").forEach((btns) => {
      const zone = btns.dataset.zone;
      const item = this._suggestions(zone)[Number(btns.dataset.i)];
      if (!item) return;
      const map = { run: "run", accept: "accept", snooze: "snooze", dismiss: "dismiss" };
      Object.entries(map).forEach(([cls, action]) => {
        const b = btns.querySelector(`.${cls}`);
        if (b) b.addEventListener("click", () => this._fire(action, item.signature));
      });
    });
  }
}

customElements.define("smart-suggestions-card", SmartSuggestionsCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "smart-suggestions-card",
  name: "Smart Suggestions Card",
  description: "One-tap contextual actions, discoveries, and waste alerts.",
});
```

- [ ] **Step 2: Commit**

```bash
git add smart_suggestions/card/smart-suggestions-card.js
git commit -m "feat: Lovelace card rendering the three suggestion zones"
```

---

### Task 10: Ingress panel rewrite (`ws_server.py`)

Replace the 1,071-line legacy panel with a slim one: four sections (Now / Noticed / Discoveries / Emerging), live logs, card-setup instructions, action buttons hitting `POST /action`. Delete its legacy test file.

**Files:**
- Rewrite: `smart_suggestions/src/ws_server.py`
- Delete: `smart_suggestions/tests/test_ws_server.py`

**Interfaces:**
- Consumes: zones dict from `Publisher.publish` (Task 7) plus an `emerging` list main adds; `ActionHandler.handle` (Task 8).
- Produces:
  - `WSServer(port: int = 8099)`
  - `register_action_handler(cb)` — async cb taking the same `{"action","signature"}` dict as ActionHandler
  - `set_zones(zones: dict) -> None` (stores; served at `GET /zones` and pushed over `/ws`)
  - `await broadcast_zones()`, `await broadcast_log(level, msg)`
  - `await start()`, `await stop()`
- Routes: `GET /` (HTML), `GET /zones` (JSON), `POST /action`, `GET /ws` (WebSocket pushing `{"type":"zones"|"log", ...}`).

- [ ] **Step 1: Rewrite `ws_server.py`** (replace entire file)

```python
# smart_suggestions/src/ws_server.py
"""Slim ingress panel: zones view + live logs + action relay."""
from __future__ import annotations
import json
import logging

from aiohttp import web, WSMsgType

_LOGGER = logging.getLogger(__name__)

_UI_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Suggestions</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family:-apple-system,system-ui,sans-serif;
         background:#111418; color:#e8eaed; padding:16px; }
  h1 { font-size:1.2em; } h2 { font-size:0.8em; text-transform:uppercase;
       letter-spacing:.08em; color:#9aa0a6; margin:20px 0 6px; }
  .item { display:flex; align-items:center; gap:10px; background:#1b1f24;
          border-radius:10px; padding:10px 12px; margin-bottom:6px; }
  .t { flex:1; min-width:0; } .title { font-weight:600; }
  .desc, .meta { font-size:.82em; color:#9aa0a6; }
  button { border:0; border-radius:9px; padding:7px 11px; cursor:pointer;
           font:inherit; font-size:.8em; background:#2b3138; color:#e8eaed; }
  button.pri { background:#2f6fed; color:#fff; }
  .empty { color:#9aa0a6; font-size:.9em; padding:6px 0 14px; }
  #logs { background:#0b0d10; border-radius:10px; padding:10px; font:11px/1.5 monospace;
          height:180px; overflow-y:auto; white-space:pre-wrap; }
  code { background:#1b1f24; padding:2px 5px; border-radius:5px; }
</style></head><body>
<h1>Smart Suggestions</h1>
<div class="desc">Add the dashboard card: copy <code>smart-suggestions-card.js</code> is auto-installed to
<code>/config/www/</code>. Register <code>/local/smart-suggestions-card.js</code> as a JavaScript-Module
dashboard resource once, then add <code>custom:smart-suggestions-card</code> to a dashboard.</div>
<div id="zones"></div>
<h2>Logs</h2><div id="logs"></div>
<script>
const ZONES = [["now","Right now"],["noticed","Noticed"],
               ["discoveries","Discovered patterns"],["emerging","Emerging (still learning)"]];
let zones = {};
function act(action, signature) {
  fetch("action", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action, signature})});
}
function render() {
  const root = document.getElementById("zones");
  root.innerHTML = ZONES.map(([key, label]) => {
    const items = zones[key] || [];
    const body = items.length ? items.map(s => `
      <div class="item"><div class="t">
        <div class="title">${s.title}</div><div class="desc">${s.description}</div>
        <div class="meta">${s.miner_type} · ${(s.confidence*100).toFixed(0)}% · seen ${s.occurrences}×</div>
      </div><div>
        ${key==="now"||key==="noticed" ? `<button class="pri" onclick="act('run','${s.signature}')">Run</button>` : ""}
        ${s.can_automate && key!=="emerging" ? `<button class="pri" onclick="act('accept','${s.signature}')">Automate</button>` : ""}
        ${key==="noticed" ? `<button onclick="act('snooze','${s.signature}')">Snooze</button>` : ""}
        ${key!=="emerging" ? `<button onclick="act('dismiss','${s.signature}')">✕</button>` : ""}
      </div></div>`).join("")
      : `<div class="empty">Nothing here yet.</div>`;
    return `<h2>${label}</h2>${body}`;
  }).join("");
}
function addLog(line) {
  const el = document.getElementById("logs");
  el.textContent += line + "\\n";
  el.scrollTop = el.scrollHeight;
}
fetch("zones").then(r => r.json()).then(z => { zones = z; render(); });
const proto = location.protocol === "https:" ? "wss" : "ws";
const ws = new WebSocket(`${proto}://${location.host}${location.pathname.replace(/\\/$/,"")}/ws`);
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "zones") { zones = msg.zones; render(); }
  if (msg.type === "log") addLog(`[${msg.level}] ${msg.message}`);
};
</script></body></html>"""


class WSServer:
    def __init__(self, port: int = 8099):
        self._port = port
        self._zones: dict = {"now": [], "noticed": [], "discoveries": [], "emerging": []}
        self._action_handler = None
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None

    def register_action_handler(self, cb) -> None:
        self._action_handler = cb

    def set_zones(self, zones: dict) -> None:
        self._zones = zones

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._ui)
        app.router.add_get("/zones", self._zones_handler)
        app.router.add_post("/action", self._action)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        _LOGGER.info("Ingress panel on :%d", self._port)

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def _ui(self, request: web.Request) -> web.Response:
        return web.Response(text=_UI_HTML, content_type="text/html")

    async def _zones_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self._zones)

    async def _action(self, request: web.Request) -> web.Response:
        data = await request.json()
        if self._action_handler:
            await self._action_handler(data)
        return web.json_response({"ok": True})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            await ws.send_json({"type": "zones", "zones": self._zones})
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self._clients.discard(ws)
        return ws

    async def _send_all(self, payload: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def broadcast_zones(self) -> None:
        await self._send_all({"type": "zones", "zones": self._zones})

    async def broadcast_log(self, level: str, message: str) -> None:
        await self._send_all({"type": "log", "level": level, "message": message})
```

- [ ] **Step 2: Delete the legacy panel test**

```bash
git rm smart_suggestions/tests/test_ws_server.py
```

- [ ] **Step 3: Verify import health**

Run: `cd smart_suggestions/src && ../../.venv/bin/python -c "import ws_server; print('ok')" && cd ../..`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add -A smart_suggestions/src/ws_server.py smart_suggestions/tests/
git commit -m "feat: slim ingress panel — zones, logs, action relay"
```

---

### Task 11: Rewire main.py, delete legacy, config v4.0.0

**Files:**
- Rewrite: `smart_suggestions/src/main.py`
- Rewrite: `smart_suggestions/tests/test_main_smoke.py`
- Modify: `smart_suggestions/config.yaml`, `smart_suggestions/Dockerfile`
- Delete: `src/statistical_engine.py`, `src/scene_engine.py`, `src/narrator.py`, `src/pattern_store.py`, `src/usage_log.py`, `src/dismissal_store.py`, `src/candidate_filter.py`, `src/const.py`, `seed_patterns.json`, `tests/test_statistical_engine.py`, `tests/test_scene_engine.py`, `tests/test_context_builder.py`, `tests/test_pattern_store.py`, `tests/test_usage_log.py`, `tests/test_dismissal_store.py`, `tests/test_candidate_filter.py`, `tests/test_const.py`

**Interfaces:**
- Consumes: everything from Tasks 1–10.
- Produces: the running add-on.

- [ ] **Step 1: Delete legacy modules and tests**

```bash
git rm smart_suggestions/src/statistical_engine.py smart_suggestions/src/scene_engine.py \
  smart_suggestions/src/narrator.py smart_suggestions/src/pattern_store.py \
  smart_suggestions/src/usage_log.py smart_suggestions/src/dismissal_store.py \
  smart_suggestions/src/candidate_filter.py smart_suggestions/src/const.py \
  smart_suggestions/seed_patterns.json \
  smart_suggestions/tests/test_statistical_engine.py smart_suggestions/tests/test_scene_engine.py \
  smart_suggestions/tests/test_context_builder.py smart_suggestions/tests/test_pattern_store.py \
  smart_suggestions/tests/test_usage_log.py smart_suggestions/tests/test_dismissal_store.py \
  smart_suggestions/tests/test_candidate_filter.py smart_suggestions/tests/test_const.py
```

- [ ] **Step 2: Rewrite `main.py`** (replace entire file)

```python
# smart_suggestions/src/main.py
"""Smart Suggestions v4 — pattern ledger pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic as _anthropic

from action_handler import ActionHandler
from context_matcher import ContextMatcher
from db_reader import DbReader
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

    async def _noop_states_ready(self, states: dict) -> None:
        return

    # ── mining ──────────────────────────────────────────────────────

    def _domain_ok(self, entity_id: str) -> bool:
        return not self._domains or entity_id.split(".")[0] in self._domains

    async def mine_once(self) -> None:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=self._history_days)
        changes = await self._db_reader.get_all_state_changes(since)
        if not changes:
            _LOGGER.info("mining: no history rows — skipping run")
            return
        history_days = min(
            self._history_days, max(1.0, (now - changes[0].ts).total_seconds() / 86400)
        )

        automated = await self._ha.get_automated_entities()
        candidates = []
        for miner_coro in (
            TemporalMiner(MINE_MIN_OCCURRENCES, MINE_MIN_PROB).run(changes, now=now),
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
        since = now - timedelta(days=self._history_days)
        history = await self._db_reader.get_all_state_changes(since)
        current = await self._ha.get_current_on_states()
        waste = await WasteDetector().run(history, current, now)
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
        items = self._matcher.match(
            rows, self._ha.get_states(), datetime.now(timezone.utc)
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
            await self._anthropic.aclose()


if __name__ == "__main__":
    addon = SmartSuggestionsAddon(_load_options())
    asyncio.run(addon.run())
```

- [ ] **Step 3: Rewrite the smoke test**

```python
# smart_suggestions/tests/test_main_smoke.py
"""Import-level smoke test: main.py wires only living modules."""


def test_main_imports():
    import main  # noqa: F401


def test_no_legacy_imports():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "src" / "main.py").read_text()
    for legacy in ("narrator", "statistical_engine", "scene_engine",
                   "pattern_store", "usage_log", "dismissal_store",
                   "candidate_filter"):
        assert legacy not in src, f"main.py still references {legacy}"
```

- [ ] **Step 4: Update `config.yaml`** (replace `version`, `description`, `options`, `schema`, `map`)

```yaml
name: Smart Suggestions
version: "4.0.0"
slug: smart_suggestions
description: Learns your habits from HA history and offers one-tap actions, automations, and waste alerts
url: https://github.com/64bitjoe/smart-suggestions-addon
arch:
  - aarch64
  - amd64
  - armhf
  - armv7
  - i386
init: false
ingress: true
ingress_port: 8099
homeassistant_api: true
auth_api: true
map:
  - config:rw
options:
  ha_url: "http://homeassistant.local:8123"
  ha_token: ""
  ai_api_key: ""
  ai_model: "claude-haiku-4-5-20251001"
  secondary_base_url: ""
  secondary_model: ""
  describe_backend: "primary"
  mining_interval_hours: 1
  waste_check_interval_minutes: 5
  history_days: 30
  max_now_suggestions: 5
  domains:
    - light
    - switch
    - climate
    - lock
    - media_player
    - cover
    - fan
    - vacuum
    - input_boolean
schema:
  ha_url: str
  ha_token: password?
  ai_api_key: password?
  ai_model: str?
  secondary_base_url: str?
  secondary_model: str?
  describe_backend: list(primary|secondary)?
  mining_interval_hours: int
  waste_check_interval_minutes: int
  history_days: int
  max_now_suggestions: int
  domains:
    - str
```

- [ ] **Step 5: Update `Dockerfile`** — drop `openai` from pip install and the seed-patterns copy:

```dockerfile
RUN pip3 install --no-cache-dir --break-system-packages anthropic aiosqlite
```

Remove the `COPY seed_patterns.json …` line, and add after `COPY src/ ./`:

```dockerfile
COPY card/ ./card/
```

Note: `_CARD_SRC = "/app/card/smart-suggestions-card.js"` matches `WORKDIR /app` + this COPY.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/ -v`
Expected: all PASS (lifecycle, ledger, describer templates, automation builder, context matcher, publisher contract, miners, remaining ha_client, main smoke)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: v4.0.0 — wire pattern-ledger pipeline, delete legacy engines and narrator"
```

---

### Task 12: Live verification against the owner's HA instance

No code — a checklist executed with the owner (superpowers:verification-before-completion applies before calling this done).

- [ ] Build/install the add-on on the HA box (`Settings → Add-ons → rebuild from local repo`), start it, watch the ingress panel logs.
- [ ] Confirm the mining loop logs `X candidates, Y ingested, Z newly confirmed` and the Emerging section populates from real history.
- [ ] Confirm the three sensors exist in Developer Tools → States with `attributes.suggestions` matching the contract.
- [ ] Register `/local/smart-suggestions-card.js` as a dashboard resource, add the card, confirm zones render.
- [ ] Tap **Run** on a "now" suggestion → device toggles, suggestion disappears (suppression), `accepted_runs` increments.
- [ ] Tap **Automate** on a discovery → automation appears in HA, pattern leaves the discoveries zone.
- [ ] Tap **Dismiss** → pattern vanishes and stays gone next mining run.
- [ ] Leave something on long enough (or lower `MIN_DURATION_HOURS` temporarily) → Noticed zone fires with template text.
- [ ] Check Anthropic console: token usage is a handful of small describe calls, not a treadmill.
