# Smart Suggestions v4.1 — UI Polish + Auto-Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Graduated-trust auto-pilot (explicit promote after 3 accepted runs, add-on-executed auto-runs with undoable activity trail) plus a polished Lovelace card and ingress panel.

**Architecture:** New `autopilot` lifecycle state in the existing pattern ledger; the 60-second match loop partitions matches — confirmed rows suggest, autopilot rows execute (throttled 10/hour) and log to a new `activity` table. Promote/demote/undo flow through the existing action dispatch (HA event bus + panel `/action`). Both UIs are rewritten in place.

**Tech Stack:** Python 3/asyncio/aiosqlite (add-on), vanilla-JS custom element with native `ha-icon` (card), inline HTML/JS panel served by aiohttp.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-19-v41-ui-autopilot-design.md`. Baseline: v4.0.7, all 62 tests green.
- Flat imports (`from lifecycle import …`); run tests from repo root: `.venv/bin/python -m pytest smart_suggestions/tests/ -q`; pytest.ini has `asyncio_mode = auto`.
- Exact values: `PROMOTE_MIN_RUNS = 3`; `NEVER_AUTOPILOT_DOMAINS = {"lock"}`; `MAX_AUTORUNS_PER_HOUR = 10`; activity zone shows most recent 15 entries from last 24 h; promotion is always explicit (user tap), never automatic; undo/demote reset `accepted_runs` to 0.
- Testing minimal (owner's rule): eligibility truth table, partition/throttle, undo reversal mapping + idempotency, updated payload contract. UIs verified live.
- All UI interpolation HTML-escaped (`esc()` pattern already in both files).
- No new pip dependencies. Version bumps to `4.1.0` in Task 5.
- Commits end with trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## File Structure (end state)

```
src/lifecycle.py        MODIFIED — LIFECYCLE_AUTOPILOT, can_promote()
src/pattern_ledger.py   MODIFIED — activity table + lifecycle/activity methods
src/autopilot.py        NEW — pure partition + throttle helpers
src/action_handler.py   MODIFIED — promote/demote/undo/execute_autorun, reverse_service()
src/publisher.py        MODIFIED — payload additions, activity zone, 4th sensor
src/main.py             MODIFIED — wiring: match-loop execution, stats, activity publish
src/ws_server.py        REWRITTEN — panel v2 (stats header, sections, filters)
card/smart-suggestions-card.js  REWRITTEN — card v2
```

---

### Task 1: Lifecycle + ledger (autopilot state, activity table)

**Files:**
- Modify: `smart_suggestions/src/lifecycle.py`
- Modify: `smart_suggestions/src/pattern_ledger.py`
- Test: `smart_suggestions/tests/test_lifecycle.py`, `smart_suggestions/tests/test_pattern_ledger.py` (append)

**Interfaces:**
- Consumes: existing lifecycle constants and `PatternLedger` internals.
- Produces:
  - `lifecycle.LIFECYCLE_AUTOPILOT = "autopilot"`, `lifecycle.PROMOTE_MIN_RUNS = 3`, `lifecycle.NEVER_AUTOPILOT_DOMAINS = {"lock"}`
  - `lifecycle.can_promote(row: dict, act_entity: str) -> bool`
  - `await PatternLedger.set_lifecycle(sig, lifecycle, reset_runs=False)`
  - `await PatternLedger.add_activity(ts: float, signature, act_entity, act_action) -> int` (row id)
  - `await PatternLedger.recent_activity(since_ts: float, limit=15) -> list[dict]` — newest first, LEFT JOINed `title` from patterns
  - `await PatternLedger.get_activity(activity_id: int) -> dict | None`
  - `await PatternLedger.mark_activity_undone(activity_id: int)`
  - `await PatternLedger.autoruns_since(since_ts: float) -> int`
  - `await PatternLedger.lifecycle_counts() -> dict[str, int]`

- [ ] **Step 1: Append failing tests**

Append to `smart_suggestions/tests/test_lifecycle.py`:

```python
def test_can_promote_truth_table():
    from lifecycle import can_promote

    base = {"lifecycle": "confirmed", "accepted_runs": 3, "dismiss_count": 0}
    assert can_promote(base, "light.porch")
    assert not can_promote({**base, "accepted_runs": 2}, "light.porch")
    assert not can_promote({**base, "dismiss_count": 1}, "light.porch")
    assert not can_promote({**base, "lifecycle": "emerging"}, "light.porch")
    assert not can_promote({**base, "lifecycle": "autopilot"}, "light.porch")
    assert not can_promote(base, "lock.front_door")  # never locks
```

Append to `smart_suggestions/tests/test_pattern_ledger.py`:

```python
async def test_set_lifecycle_and_reset_runs(ledger):
    await ledger.upsert_evidence(_cand())
    sig = _cand().signature()
    await ledger.record_run(sig)
    await ledger.record_run(sig)
    await ledger.set_lifecycle(sig, "autopilot")
    assert (await ledger.get(sig))["lifecycle"] == "autopilot"
    await ledger.set_lifecycle(sig, "confirmed", reset_runs=True)
    row = await ledger.get(sig)
    assert row["lifecycle"] == "confirmed"
    assert row["accepted_runs"] == 0


async def test_activity_log_roundtrip(ledger):
    await ledger.upsert_evidence(_cand())
    sig = _cand().signature()
    await ledger.save_description(sig, "Porch at 8", "d", "", "template")
    aid = await ledger.add_activity(1000.0, sig, "light.porch", "turn_on")
    assert isinstance(aid, int)
    rows = await ledger.recent_activity(since_ts=0.0)
    assert rows[0]["act_entity"] == "light.porch"
    assert rows[0]["title"] == "Porch at 8"      # joined from patterns
    assert rows[0]["undone"] == 0
    assert await ledger.autoruns_since(0.0) == 1
    await ledger.mark_activity_undone(aid)
    assert (await ledger.get_activity(aid))["undone"] == 1


async def test_lifecycle_counts(ledger):
    await ledger.upsert_evidence(_cand())
    counts = await ledger.lifecycle_counts()
    assert counts.get("emerging") == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_lifecycle.py smart_suggestions/tests/test_pattern_ledger.py -q`
Expected: FAIL — `ImportError: cannot import name 'can_promote'` and `AttributeError` on new ledger methods

- [ ] **Step 3: Implement lifecycle.py additions** (append after existing constants)

```python
LIFECYCLE_AUTOPILOT = "autopilot"
PROMOTE_MIN_RUNS = 3
NEVER_AUTOPILOT_DOMAINS = {"lock"}


def can_promote(row: dict, act_entity: str) -> bool:
    """Eligible for the auto-run promote chip. Promotion itself is always an
    explicit user action — this only gates the offer."""
    return (
        row.get("lifecycle") == LIFECYCLE_CONFIRMED
        and row.get("accepted_runs", 0) >= PROMOTE_MIN_RUNS
        and row.get("dismiss_count", 0) == 0
        and act_entity.split(".", 1)[0] not in NEVER_AUTOPILOT_DOMAINS
    )
```

- [ ] **Step 4: Implement pattern_ledger.py additions**

Add to `_SCHEMA` handling in `init()` — execute a second statement after the patterns table:

```python
_ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    signature TEXT NOT NULL,
    act_entity TEXT NOT NULL,
    act_action TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
)
"""
```

(in `init()`: `await db.execute(_ACTIVITY_SCHEMA)` before commit.)

New methods on `PatternLedger`:

```python
    async def set_lifecycle(
        self, sig: str, lifecycle: str, reset_runs: bool = False
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            if reset_runs:
                await db.execute(
                    "UPDATE patterns SET lifecycle=?, accepted_runs=0 WHERE signature=?",
                    (lifecycle, sig),
                )
            else:
                await db.execute(
                    "UPDATE patterns SET lifecycle=? WHERE signature=?",
                    (lifecycle, sig),
                )
            await db.commit()

    async def add_activity(
        self, ts: float, signature: str, act_entity: str, act_action: str
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO activity (ts, signature, act_entity, act_action) "
                "VALUES (?, ?, ?, ?)",
                (ts, signature, act_entity, act_action),
            )
            await db.commit()
            return cur.lastrowid

    async def recent_activity(self, since_ts: float, limit: int = 15) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT a.*, p.title FROM activity a "
                "LEFT JOIN patterns p ON a.signature = p.signature "
                "WHERE a.ts >= ? ORDER BY a.ts DESC LIMIT ?",
                (since_ts, limit),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_activity(self, activity_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM activity WHERE id=?", (activity_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_activity_undone(self, activity_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE activity SET undone=1 WHERE id=?", (activity_id,)
            )
            await db.commit()

    async def autoruns_since(self, since_ts: float) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM activity WHERE ts >= ?", (since_ts,)
            )
            return (await cur.fetchone())[0]

    async def lifecycle_counts(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT lifecycle, COUNT(*) FROM patterns GROUP BY lifecycle"
            )
            return {row[0]: row[1] for row in await cur.fetchall()}
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_lifecycle.py smart_suggestions/tests/test_pattern_ledger.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add smart_suggestions/src/lifecycle.py smart_suggestions/src/pattern_ledger.py smart_suggestions/tests/
git commit -m "feat: autopilot lifecycle state, promote eligibility, activity log"
```

---

### Task 2: Auto-pilot partition/throttle + action reversal

**Files:**
- Create: `smart_suggestions/src/autopilot.py`
- Modify: `smart_suggestions/src/action_handler.py`
- Test: `smart_suggestions/tests/test_autopilot.py`

**Interfaces:**
- Consumes: `lifecycle.LIFECYCLE_AUTOPILOT`.
- Produces:
  - `autopilot.MAX_AUTORUNS_PER_HOUR = 10`
  - `autopilot.partition_matches(items: list[dict]) -> tuple[list[dict], list[dict]]` — `(to_execute, to_suggest)` by `lifecycle == "autopilot"`
  - `autopilot.within_throttle(recent_autoruns: int) -> bool`
  - `action_handler.reverse_service(act_action: str) -> str | None` — turn_on↔turn_off; `set_state_on`→`turn_off`; `set_state_off`→`turn_on`; `currently_on`→`turn_on`; unknown→None

- [ ] **Step 1: Write failing tests**

```python
# smart_suggestions/tests/test_autopilot.py
from autopilot import partition_matches, within_throttle, MAX_AUTORUNS_PER_HOUR
from action_handler import reverse_service


def test_partition_by_lifecycle():
    items = [
        {"signature": "a", "lifecycle": "autopilot"},
        {"signature": "b", "lifecycle": "confirmed"},
        {"signature": "c", "lifecycle": "autopilot"},
    ]
    execute, suggest = partition_matches(items)
    assert [r["signature"] for r in execute] == ["a", "c"]
    assert [r["signature"] for r in suggest] == ["b"]


def test_throttle():
    assert within_throttle(0)
    assert within_throttle(MAX_AUTORUNS_PER_HOUR - 1)
    assert not within_throttle(MAX_AUTORUNS_PER_HOUR)


def test_reverse_service_mapping():
    assert reverse_service("turn_on") == "turn_off"
    assert reverse_service("turn_off") == "turn_on"
    assert reverse_service("set_state_on") == "turn_off"
    assert reverse_service("set_state_off") == "turn_on"
    assert reverse_service("currently_on") == "turn_on"
    assert reverse_service("bogus") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_autopilot.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autopilot'`

- [ ] **Step 3: Implement `autopilot.py`**

```python
# smart_suggestions/src/autopilot.py
"""Pure helpers for graduated-trust auto-run decisions."""
from __future__ import annotations

from lifecycle import LIFECYCLE_AUTOPILOT

MAX_AUTORUNS_PER_HOUR = 10


def partition_matches(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split context-matched rows into (to_execute, to_suggest)."""
    execute = [r for r in items if r.get("lifecycle") == LIFECYCLE_AUTOPILOT]
    suggest = [r for r in items if r.get("lifecycle") != LIFECYCLE_AUTOPILOT]
    return execute, suggest


def within_throttle(recent_autoruns: int) -> bool:
    return recent_autoruns < MAX_AUTORUNS_PER_HOUR
```

- [ ] **Step 4: Add `reverse_service` to `action_handler.py`** (module level, above the class)

```python
_REVERSE = {"turn_on": "turn_off", "turn_off": "turn_on",
            "set_state_on": "turn_off", "set_state_off": "turn_on",
            "currently_on": "turn_on"}


def reverse_service(act_action: str) -> str | None:
    """Service that undoes an auto-run action; None if not reversible."""
    return _REVERSE.get(act_action)
```

- [ ] **Step 5: Run to verify pass, then commit**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_autopilot.py -q` → all PASS

```bash
git add smart_suggestions/src/autopilot.py smart_suggestions/src/action_handler.py smart_suggestions/tests/test_autopilot.py
git commit -m "feat: autopilot partition/throttle helpers and action reversal map"
```

---

### Task 3: ActionHandler — promote / demote / undo / execute_autorun

**Files:**
- Modify: `smart_suggestions/src/action_handler.py`

**Interfaces:**
- Consumes: Task 1 ledger methods, Task 2 `reverse_service`, `lifecycle.can_promote`, `lifecycle.LIFECYCLE_AUTOPILOT/LIFECYCLE_CONFIRMED`, existing `_resolve_act`, `_SERVICE`, `ContextMatcher.suppress`.
- Produces:
  - `handle(data)` accepts new actions: `promote {signature}`, `demote {signature}`, `undo {activity_id}` (activity_id int or str)
  - `await execute_autorun(row: dict, now: datetime) -> bool` — called by main's match loop; performs service call, activity insert, record_run, suppress; returns True when executed

No unit tests (owner's minimal rule — pure pieces are covered by Tasks 1–2; this is glue verified live).

- [ ] **Step 1: Extend imports and `handle()`**

Add imports at top of `action_handler.py`:

```python
from lifecycle import LIFECYCLE_AUTOPILOT, LIFECYCLE_CONFIRMED, can_promote
```

Inside `handle()`, extend the action dispatch with three new branches (before the final `else`):

```python
        elif action == "promote":
            act_entity, _ = self._resolve_act(row)
            if can_promote(row, act_entity):
                await self._ledger.set_lifecycle(sig, LIFECYCLE_AUTOPILOT)
                _LOGGER.info("promoted to autopilot: %s", sig)
            else:
                _LOGGER.warning("promote rejected (not eligible): %s", sig)
                return
        elif action == "demote":
            await self._ledger.set_lifecycle(
                sig, LIFECYCLE_CONFIRMED, reset_runs=True
            )
            _LOGGER.info("demoted to suggest-only: %s", sig)
```

`undo` targets an activity row, not a signature, so handle it BEFORE the signature lookup. Restructure the top of `handle()`:

```python
    async def handle(self, data: dict) -> None:
        action = data.get("action")
        if action == "undo":
            await self._undo(data.get("activity_id"))
            await self._refresh()
            return
        sig = data.get("signature")
        if not action or not sig:
            return
        ...  # existing flow unchanged
```

And the new method:

```python
    async def _undo(self, activity_id) -> None:
        try:
            activity_id = int(activity_id)
        except (TypeError, ValueError):
            return
        entry = await self._ledger.get_activity(activity_id)
        if entry is None or entry["undone"]:
            return  # idempotent
        service = reverse_service(entry["act_action"])
        if service:
            await self._ha.call_service(
                "homeassistant", service, entry["act_entity"]
            )
        await self._ledger.mark_activity_undone(activity_id)
        # Veto: back to suggest-only, trust re-earned from zero.
        await self._ledger.set_lifecycle(
            entry["signature"], LIFECYCLE_CONFIRMED, reset_runs=True
        )
        _LOGGER.info("undo: reversed activity %d, demoted %s",
                     activity_id, entry["signature"])
```

- [ ] **Step 2: Add `execute_autorun`**

```python
    async def execute_autorun(self, row: dict, now) -> bool:
        """Execute a matched autopilot pattern. Returns True when executed."""
        act_entity, act_action = row.get("act_entity"), row.get("act_action")
        if not act_entity:
            act_entity, act_action = self._resolve_act(row)
        service = _SERVICE.get(act_action)
        if service is None:
            return False
        ok = await self._ha.call_service("homeassistant", service, act_entity)
        # Log even on failure (spec): the window fired; undo of a failed run
        # simply demotes.
        await self._ledger.add_activity(
            now.timestamp(), row["signature"], act_entity, act_action
        )
        if ok:
            await self._ledger.record_run(row["signature"])
        self._matcher.suppress(row["signature"], now + RUN_SUPPRESS)
        if not ok:
            _LOGGER.warning("autorun service call failed: %s %s",
                            service, act_entity)
        return True
```

- [ ] **Step 3: Sanity run + commit**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/ -q` → all PASS (no behavior under test changed)

```bash
git add smart_suggestions/src/action_handler.py
git commit -m "feat: promote/demote/undo actions and autorun execution"
```

---

### Task 4: Publisher — payload additions, activity zone, 4th sensor

**Files:**
- Modify: `smart_suggestions/src/publisher.py`
- Test: `smart_suggestions/tests/test_publisher_contract.py` (update)

**Interfaces:**
- Consumes: `lifecycle.can_promote`.
- Produces:
  - `build_payload(row, zone)` — exact key set now: `{signature, zone, title, description, entity_id, act_entity, act_action, miner_type, confidence, occurrences, can_automate, lifecycle, accepted_runs, can_promote}`
  - `build_activity_payload(entry: dict) -> dict` — keys `{activity_id, ts, title, act_entity, act_action, undone, signature}`; `title` falls back to `act_entity`'s friendly form when NULL
  - `SENSOR_ACTIVITY = "sensor.smart_suggestions_activity"`
  - `Publisher.publish(now_items, discovery_rows, noticed_rows, activity_entries) -> dict` — zones dict gains `"activity"`; pushes the 4th sensor

- [ ] **Step 1: Update the contract test** (replace `CARD_CONTRACT` and add activity test)

```python
CARD_CONTRACT = {
    "signature", "zone", "title", "description", "entity_id",
    "act_entity", "act_action", "miner_type", "confidence",
    "occurrences", "can_automate", "lifecycle", "accepted_runs",
    "can_promote",
}
```

Update `_row()` to include `"lifecycle": "confirmed", "accepted_runs": 0, "dismiss_count": 0`. Add:

```python
def test_can_promote_flag_in_payload():
    row = _row(act=("light.porch", "turn_on"))
    row["accepted_runs"] = 3
    p = build_payload(row, zone="now")
    assert p["can_promote"] is True
    assert build_payload(_row(), zone="now")["can_promote"] is False


def test_activity_payload_shape():
    from publisher import build_activity_payload
    p = build_activity_payload({
        "id": 7, "ts": 1000.0, "signature": "abc", "title": None,
        "act_entity": "light.porch", "act_action": "turn_on", "undone": 0,
    })
    assert set(p.keys()) == {"activity_id", "ts", "title", "act_entity",
                             "act_action", "undone", "signature"}
    assert p["activity_id"] == 7
    assert "porch" in p["title"].lower()  # fallback title from entity
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/test_publisher_contract.py -q`
Expected: FAIL (missing keys / missing function)

- [ ] **Step 3: Implement publisher changes**

Imports: `from lifecycle import can_promote` and reuse `_friendly` from llm_describer (`from llm_describer import template_description, _friendly`).

In `build_payload`, add to the returned dict:

```python
        "lifecycle": row.get("lifecycle", ""),
        "accepted_runs": row.get("accepted_runs", 0),
        "can_promote": can_promote(row, act_entity),
```

New constant + function:

```python
SENSOR_ACTIVITY = "sensor.smart_suggestions_activity"


def build_activity_payload(entry: dict) -> dict:
    return {
        "activity_id": entry["id"],
        "ts": entry["ts"],
        "title": entry.get("title") or _friendly(entry["act_entity"]),
        "act_entity": entry["act_entity"],
        "act_action": entry["act_action"],
        "undone": entry["undone"],
        "signature": entry["signature"],
    }
```

`Publisher.publish` gains the 4th parameter and zone; push loop gains
`(SENSOR_ACTIVITY, "activity", "Smart Suggestions: Auto-Pilot")`:

```python
    async def publish(
        self, now_items, discovery_rows, noticed_rows, activity_entries,
    ) -> dict:
        ...
        zones = {
            "now": [...unchanged...],
            "discoveries": [...unchanged...],
            "noticed": [...unchanged...],
            "activity": [build_activity_payload(e) for e in activity_entries],
        }
```

(Show full updated method in the implementation; the elisions above refer to the existing v4.0.5 list comprehensions, unchanged.)

- [ ] **Step 4: Fix the one existing caller**

`main.py refresh_publish` currently calls `publish(self._now_items, discovery_rows, waste_rows)` — add `[]` as a fourth argument for now (Task 5 wires real activity): `publish(self._now_items, discovery_rows, waste_rows, [])`.

- [ ] **Step 5: Run full suite, commit**

Run: `.venv/bin/python -m pytest smart_suggestions/tests/ -q` → all PASS

```bash
git add smart_suggestions/src/publisher.py smart_suggestions/src/main.py smart_suggestions/tests/test_publisher_contract.py
git commit -m "feat: payload promote/lifecycle fields, activity zone + sensor"
```

---

### Task 5: main.py wiring + version 4.1.0

**Files:**
- Modify: `smart_suggestions/src/main.py`
- Modify: `smart_suggestions/config.yaml` (version 4.1.0)

**Interfaces:**
- Consumes: everything above; `WSServer.set_stats(dict)` (Task 7 — add a no-op guard: call only if the method exists is NOT needed, Task 7 lands before deploy; plan order note: Tasks 5–7 all land before the single 4.1.0 deploy).
- Produces: the running v4.1 pipeline.

- [ ] **Step 1: match_once executes autopilot matches**

Replace `match_once` with:

```python
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

        changed = [r["signature"] for r in to_suggest] != [
            r["signature"] for r in self._now_items
        ]
        self._now_items = to_suggest
        if changed or executed_any:
            await self.refresh_publish()
```

Imports: `from autopilot import partition_matches, within_throttle`.

- [ ] **Step 2: refresh_publish gains activity + autopilot zones + stats**

Replace `refresh_publish` with:

```python
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
            self._now_items, discovery_rows, waste_rows, activity
        )
        zones["autopilot"] = [build_payload(r, "autopilot") for r in autopilot_rows]
        emerging = await self._ledger.get_rows(("emerging",))
        zones["emerging"] = [build_payload(r, "emerging") for r in emerging]
        self._ws_server.set_zones(zones)
        counts = await self._ledger.lifecycle_counts()
        self._ws_server.set_stats({
            "counts": counts,
            "last_mining": self._last_mining_summary,
            "version": "4.1.0",
        })
        await self._ws_server.broadcast_zones()
```

Add to `__init__`: `self._last_mining_summary: str = "not yet run"`. In `mine_once`, after the summary log line, set:

```python
        self._last_mining_summary = (
            f"{len(candidates)} candidates, {ingested} ingested, "
            f"{len(newly)} newly confirmed"
        )
```

- [ ] **Step 3: Version bump + full suite**

`sed -i '' 's/version: "4.0.7"/version: "4.1.0"/' smart_suggestions/config.yaml`

Run: `.venv/bin/python -m pytest smart_suggestions/tests/ -q` → all PASS

- [ ] **Step 4: Commit**

```bash
git add smart_suggestions/src/main.py smart_suggestions/config.yaml
git commit -m "feat: v4.1.0 wiring — autorun execution loop, activity/autopilot zones, panel stats"
```

---

### Task 6: Lovelace card v2

**Files:**
- Rewrite: `smart_suggestions/card/smart-suggestions-card.js`

**Interfaces:**
- Consumes: 4 sensors (`sensor.smart_suggestions_now/_activity/_noticed/_discoveries`), payload contract from Task 4, actions run/accept/snooze/dismiss/promote/undo via the event POST.
- Produces: `custom:smart-suggestions-card` v2.

- [ ] **Step 1: Rewrite the card** (full file)

```javascript
// smart_suggestions/card/smart-suggestions-card.js
// Smart Suggestions card v2 — zones, activity trail, promote chips.
// Resource: /local/smart-suggestions-card.js (JavaScript Module)

const SENSORS = {
  now: "sensor.smart_suggestions_now",
  activity: "sensor.smart_suggestions_activity",
  noticed: "sensor.smart_suggestions_noticed",
  discoveries: "sensor.smart_suggestions_discoveries",
};
const ZONE_TITLES = {
  now: "Right now", activity: "Auto-pilot",
  noticed: "Noticed", discoveries: "Discovered patterns",
};
const MINER_ICONS = {
  temporal: "mdi:clock-outline", sequence: "mdi:arrow-decision",
  cross_area: "mdi:motion-sensor", waste: "mdi:alert-circle-outline",
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const timeAgo = (ts) => {
  const mins = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
};

class SmartSuggestionsCard extends HTMLElement {
  constructor() { super(); this._expanded = new Set(); }
  setConfig(config) { this._config = config; }
  getCardSize() { return 5; }

  set hass(hass) {
    this._hass = hass;
    const key = Object.values(SENSORS)
      .map((id) => hass.states[id]?.last_updated ?? "")
      .join("|");
    if (key === this._renderedKey) return;
    this._renderedKey = key;
    this._render();
  }

  _items(zone) {
    const st = this._hass.states[SENSORS[zone]];
    return (st && st.attributes.suggestions) || [];
  }

  _fire(payload) {
    this._hass.callApi("POST", "events/smart_suggestions_action", payload);
    this._renderedKey = null; // force refresh on next hass tick
  }

  _suggestionRow(s, zone) {
    const open = this._expanded.has(s.signature);
    const pct = Math.round((s.confidence || 0) * 100);
    return `
      <div class="item" data-sig="${esc(s.signature)}">
        <ha-icon class="mi" icon="${MINER_ICONS[s.miner_type] || "mdi:lightbulb-outline"}"></ha-icon>
        <div class="text">
          <div class="title">${esc(s.title)}
            ${s.lifecycle === "autopilot" ? `<span class="badge">⚡</span>` : ""}
            <span class="chip">${s.occurrences}×</span>
          </div>
          <div class="bar"><span style="width:${pct}%"></span></div>
          ${open ? `<div class="desc">${esc(s.description)}</div>` : ""}
        </div>
        <div class="btns" data-zone="${zone}" data-sig="${esc(s.signature)}">
          ${s.can_promote ? `<button class="promote" title="Enable auto-run">⚡ Auto</button>` : ""}
          ${zone !== "discoveries" ? `<button class="run">Run</button>` : ""}
          ${s.can_automate ? `<button class="accept">Automate</button>` : ""}
          ${zone === "noticed" ? `<button class="snooze">Snooze</button>` : ""}
          <button class="dismiss">✕</button>
        </div>
      </div>`;
  }

  _activityRow(a) {
    const verb = a.act_action.includes("off") ? "Turned off" : "Turned on";
    return `
      <div class="item act ${a.undone ? "undone" : ""}">
        <ha-icon class="mi" icon="mdi:robot"></ha-icon>
        <div class="text">
          <div class="title">${verb} ${esc(a.title)}</div>
          <div class="desc">${timeAgo(a.ts)}${a.undone ? " · undone" : ""}</div>
        </div>
        ${a.undone ? "" :
          `<div class="btns"><button class="undo" data-aid="${a.activity_id}">Undo</button></div>`}
      </div>`;
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const sections = ["now", "activity", "noticed", "discoveries"]
      .map((zone) => {
        const items = this._items(zone);
        if (!items.length) return "";
        const rows = items.map((s) =>
          zone === "activity" ? this._activityRow(s) : this._suggestionRow(s, zone)
        ).join("");
        return `<div class="zone z-${zone}"><h3>${ZONE_TITLES[zone]}</h3>${rows}</div>`;
      }).join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px 16px 14px; }
        h3 { margin: 10px 0 4px; font-size: 0.78em; text-transform: uppercase;
             letter-spacing: 0.08em; color: var(--secondary-text-color); }
        .z-now h3 { color: var(--primary-color); }
        .z-noticed h3 { color: var(--warning-color, #e6a23c); }
        .item { display: flex; align-items: flex-start; gap: 10px;
                padding: 9px 0; border-bottom: 1px solid var(--divider-color); }
        .item:last-child { border-bottom: none; }
        .mi { --mdc-icon-size: 20px; color: var(--secondary-text-color);
              margin-top: 2px; flex-shrink: 0; }
        .text { flex: 1; min-width: 0; cursor: pointer; }
        .title { font-weight: 500; line-height: 1.3; }
        .chip { font-size: 0.72em; color: var(--secondary-text-color);
                background: var(--secondary-background-color);
                border-radius: 8px; padding: 1px 6px; margin-left: 6px; }
        .badge { margin-left: 4px; }
        .bar { height: 3px; border-radius: 2px; margin-top: 5px;
               background: var(--divider-color); overflow: hidden; }
        .bar span { display: block; height: 100%; border-radius: 2px;
                    background: var(--primary-color); }
        .desc { font-size: 0.85em; color: var(--secondary-text-color);
                margin-top: 5px; }
        .btns { display: flex; gap: 4px; flex-shrink: 0; flex-wrap: wrap;
                justify-content: flex-end; max-width: 45%; }
        button { border: none; border-radius: 12px; padding: 6px 10px;
                 cursor: pointer; font: inherit; font-size: 0.78em;
                 background: var(--secondary-background-color);
                 color: var(--primary-text-color); }
        button.run, button.accept { background: var(--primary-color); color: #fff; }
        button.promote { background: #7c4dff; color: #fff; }
        button.undo { background: var(--warning-color, #e6a23c); color: #fff; }
        .act.undone { opacity: 0.55; }
        .empty { color: var(--secondary-text-color); padding: 12px 0; }
      </style>
      <ha-card header="Smart Suggestions">
        ${sections || `<div class="empty">Nothing to suggest right now — still learning your patterns.</div>`}
      </ha-card>`;

    // expand/collapse on text tap
    this.shadowRoot.querySelectorAll(".item[data-sig] .text").forEach((el) => {
      const sig = el.closest(".item").dataset.sig;
      el.addEventListener("click", () => {
        this._expanded.has(sig) ? this._expanded.delete(sig) : this._expanded.add(sig);
        this._renderedKey = null;
        this._render();
      });
    });
    // suggestion actions
    this.shadowRoot.querySelectorAll(".btns[data-sig]").forEach((btns) => {
      const sig = btns.dataset.sig;
      [["run", "run"], ["accept", "accept"], ["snooze", "snooze"],
       ["dismiss", "dismiss"], ["promote", "promote"]].forEach(([cls, action]) => {
        const b = btns.querySelector(`.${cls}`);
        if (b) b.addEventListener("click", (ev) => {
          ev.stopPropagation();
          this._fire({ action, signature: sig });
        });
      });
    });
    // activity undo
    this.shadowRoot.querySelectorAll("button.undo").forEach((b) => {
      b.addEventListener("click", () =>
        this._fire({ action: "undo", activity_id: Number(b.dataset.aid) }));
    });
  }
}

customElements.define("smart-suggestions-card", SmartSuggestionsCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "smart-suggestions-card",
  name: "Smart Suggestions Card",
  description: "One-tap actions, auto-pilot activity, discoveries, waste alerts.",
});
```

- [ ] **Step 2: Syntax check + commit**

Run: `node --check smart_suggestions/card/smart-suggestions-card.js` → OK

```bash
git add smart_suggestions/card/smart-suggestions-card.js
git commit -m "feat: card v2 — activity zone, promote chips, icons, confidence bars"
```

---

### Task 7: Ingress panel v2

**Files:**
- Modify: `smart_suggestions/src/ws_server.py` (replace `_UI_HTML`; add `set_stats`)

**Interfaces:**
- Consumes: zones dict incl. `activity`, `autopilot`, `emerging` (Task 5); actions incl. promote/demote/undo via `/action`.
- Produces: `WSServer.set_stats(stats: dict)`; `GET /zones` returns `{"zones": ..., "stats": ...}`; WS pushes `{"type": "zones", "zones": ..., "stats": ...}`.

- [ ] **Step 1: WSServer plumbing**

Add to `__init__`: `self._stats: dict = {}`. Add:

```python
    def set_stats(self, stats: dict) -> None:
        self._stats = stats
```

Change `_zones_handler` to `return web.json_response({"zones": self._zones, "stats": self._stats})`; change both zone pushes (`_ws` initial send and `broadcast_zones`) to `{"type": "zones", "zones": self._zones, "stats": self._stats}`.

- [ ] **Step 2: Replace `_UI_HTML`** with the v2 panel:

```python
_UI_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Suggestions</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family:-apple-system,system-ui,sans-serif;
         background:#111418; color:#e8eaed; padding:16px; max-width:900px; margin:auto; }
  h1 { font-size:1.25em; margin:4px 0 2px; }
  .sub { color:#9aa0a6; font-size:.8em; margin-bottom:12px; }
  .stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  .stat { background:#1b1f24; border-radius:12px; padding:10px 14px; min-width:74px; }
  .stat b { display:block; font-size:1.3em; }
  .stat span { font-size:.72em; color:#9aa0a6; text-transform:uppercase; letter-spacing:.05em; }
  h2 { font-size:.78em; text-transform:uppercase; letter-spacing:.08em;
       color:#9aa0a6; margin:20px 0 6px; }
  h2.now { color:#2f6fed; } h2.noticed { color:#e6a23c; } h2.auto { color:#9b6bff; }
  .filters { display:flex; gap:6px; margin:6px 0 10px; }
  .fchip { border:0; border-radius:10px; padding:5px 11px; cursor:pointer;
           font-size:.78em; background:#1b1f24; color:#9aa0a6; }
  .fchip.on { background:#2f6fed; color:#fff; }
  .item { display:flex; align-items:flex-start; gap:10px; background:#1b1f24;
          border-radius:12px; padding:10px 12px; margin-bottom:6px; }
  .t { flex:1; min-width:0; } .title { font-weight:600; line-height:1.3; }
  .desc, .meta { font-size:.8em; color:#9aa0a6; margin-top:2px; }
  .bar { height:3px; border-radius:2px; margin-top:6px; background:#2b3138; overflow:hidden; }
  .bar span { display:block; height:100%; background:#2f6fed; border-radius:2px; }
  .btns { display:flex; gap:4px; flex-wrap:wrap; justify-content:flex-end; }
  button { border:0; border-radius:9px; padding:7px 11px; cursor:pointer;
           font:inherit; font-size:.78em; background:#2b3138; color:#e8eaed; }
  button.pri { background:#2f6fed; color:#fff; }
  button.promote { background:#7c4dff; color:#fff; }
  button.undo { background:#e6a23c; color:#fff; }
  .undone { opacity:.5; }
  .empty { color:#9aa0a6; font-size:.85em; padding:4px 0 10px; }
  #logs { background:#0b0d10; border-radius:12px; padding:10px; font:11px/1.5 monospace;
          height:170px; overflow-y:auto; white-space:pre-wrap; }
  code { background:#1b1f24; padding:2px 5px; border-radius:5px; }
</style></head><body>
<h1>Smart Suggestions</h1>
<div class="sub" id="sub"></div>
<div class="stats" id="stats"></div>
<div id="zones"></div>
<h2>Logs</h2><div id="logs"></div>
<script>
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ZONES = [["now","Right now","now"],["activity","Auto-pilot activity","auto"],
  ["autopilot","Auto-pilot patterns","auto"],["noticed","Noticed","noticed"],
  ["discoveries","Discovered patterns",""],["emerging","Emerging (still learning)",""]];
let zones = {}, stats = {}, filter = "all";
function act(payload) {
  fetch("action", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(payload)});
}
function timeAgo(ts) {
  const m = Math.max(0, Math.round(Date.now()/1000/60 - ts/60));
  return m < 1 ? "just now" : m < 60 ? m + "m ago" : Math.round(m/60) + "h ago";
}
function sugRow(s, key) {
  const pct = Math.round((s.confidence||0)*100);
  return `<div class="item"><div class="t">
    <div class="title">${esc(s.title)}${s.lifecycle==="autopilot"?" ⚡":""}</div>
    <div class="desc">${esc(s.description)}</div>
    <div class="meta">${esc(s.miner_type)} · ${pct}% · seen ${s.occurrences}×${s.accepted_runs?` · run ${s.accepted_runs}×`:""}</div>
    <div class="bar"><span style="width:${pct}%"></span></div>
  </div><div class="btns">
    ${s.can_promote?`<button class="promote" onclick='act({action:"promote",signature:"${esc(s.signature)}"})'>⚡ Auto</button>`:""}
    ${key==="autopilot"?`<button onclick='act({action:"demote",signature:"${esc(s.signature)}"})'>Demote</button>`:""}
    ${key==="now"||key==="noticed"?`<button class="pri" onclick='act({action:"run",signature:"${esc(s.signature)}"})'>Run</button>`:""}
    ${s.can_automate&&key!=="emerging"?`<button class="pri" onclick='act({action:"accept",signature:"${esc(s.signature)}"})'>Automate</button>`:""}
    ${key==="noticed"?`<button onclick='act({action:"snooze",signature:"${esc(s.signature)}"})'>Snooze</button>`:""}
    ${key!=="emerging"?`<button onclick='act({action:"dismiss",signature:"${esc(s.signature)}"})'>✕</button>`:""}
  </div></div>`;
}
function actRow(a) {
  const verb = a.act_action.includes("off") ? "Turned off" : "Turned on";
  return `<div class="item ${a.undone?"undone":""}"><div class="t">
    <div class="title">🤖 ${verb} ${esc(a.title)}</div>
    <div class="meta">${timeAgo(a.ts)}${a.undone?" · undone":""}</div></div>
    ${a.undone?"":`<div class="btns"><button class="undo" onclick='act({action:"undo",activity_id:${Number(a.activity_id)}})'>Undo</button></div>`}
  </div>`;
}
function render() {
  const s = stats.counts || {};
  document.getElementById("sub").textContent =
    `v${stats.version || "?"} · last mining: ${stats.last_mining || "…"}`;
  document.getElementById("stats").innerHTML =
    [["emerging","Emerging"],["confirmed","Confirmed"],["autopilot","Auto-pilot"],
     ["automated","Automated"],["dismissed","Dismissed"]]
    .map(([k,l]) => `<div class="stat"><b>${s[k]||0}</b><span>${l}</span></div>`).join("");
  document.getElementById("zones").innerHTML = ZONES.map(([key,label,cls]) => {
    let items = zones[key] || [];
    const isFilterable = key === "discoveries" || key === "emerging";
    if (isFilterable && filter !== "all")
      items = items.filter((s) => s.miner_type === filter);
    const chips = isFilterable && key === "discoveries" ?
      `<div class="filters">${["all","temporal","sequence","cross_area"].map((f) =>
        `<button class="fchip ${filter===f?"on":""}" onclick="setFilter('${f}')">${f}</button>`).join("")}</div>` : "";
    const body = items.length
      ? items.map((x) => key === "activity" ? actRow(x) : sugRow(x, key)).join("")
      : `<div class="empty">Nothing here yet.</div>`;
    return `<h2 class="${cls}">${label}</h2>${chips}${body}`;
  }).join("");
}
function setFilter(f) { filter = f; render(); }
function addLog(line) {
  const el = document.getElementById("logs");
  el.textContent += line + "\\n"; el.scrollTop = el.scrollHeight;
}
fetch("zones").then(r => r.json()).then((d) => { zones = d.zones||{}; stats = d.stats||{}; render(); });
const proto = location.protocol === "https:" ? "wss" : "ws";
const ws = new WebSocket(`${proto}://${location.host}${location.pathname.replace(/\\/$/,"")}/ws`);
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "zones") { zones = msg.zones||{}; stats = msg.stats||{}; render(); }
  if (msg.type === "log") addLog(`[${msg.level}] ${msg.message}`);
};
</script></body></html>"""
```

- [ ] **Step 3: Verify import + full suite, commit**

Run: `cd smart_suggestions/src && ../../.venv/bin/python -c "import ws_server; print('ok')" && cd ../..` → ok
Run: `.venv/bin/python -m pytest smart_suggestions/tests/ -q` → all PASS

```bash
git add smart_suggestions/src/ws_server.py
git commit -m "feat: panel v2 — stats header, autopilot/activity sections, filters"
```

---

### Task 8: Deploy + live verification

- [ ] Push main; Supervisor store reload; add-on update to 4.1.0 (established MCP loop).
- [ ] Logs: clean start, mining summary, no push failures.
- [ ] Panel: stats header shows counts + version 4.1.0; sections render; filter chips work.
- [ ] Card (after user's HA restart + hard refresh): icons, confidence bars, tap-to-expand.
- [ ] Trust loop: tap Run on one suggestion 3 times (or set accepted_runs=3 via a run) → ⚡ Auto chip appears → promote → pattern shows under Auto-pilot with Demote → when its window matches, verify an activity entry appears and the service fired → tap Undo → action reversed, pattern back in Discoveries with runs reset.
- [ ] Throttle + lock exclusion sanity: confirm a lock-domain row never shows the promote chip.
