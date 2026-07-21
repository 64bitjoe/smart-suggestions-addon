"""Trust-critical ActionHandler paths: undo semantics and transition guards."""
from datetime import datetime, timezone

from action_handler import ActionHandler


class StubLedger:
    def __init__(self, rows=None, activity=None):
        self.rows = rows or {}
        self.activity = activity or {}
        self.lifecycle_calls = []
        self.undone = []
    async def get(self, sig):
        return self.rows.get(sig)
    async def get_activity(self, aid):
        return self.activity.get(aid)
    async def mark_activity_undone(self, aid):
        self.undone.append(aid)
    async def set_lifecycle(self, sig, lifecycle, reset_runs=False, expected=None):
        row = self.rows.get(sig)
        if expected is not None and (row or {}).get("lifecycle") != expected:
            return False
        self.lifecycle_calls.append((sig, lifecycle, reset_runs))
        if row:
            row["lifecycle"] = lifecycle
        return True
    async def record_run(self, sig): pass
    async def add_activity(self, *a, **k): return 1


class StubHA:
    def __init__(self): self.calls = []
    async def call_service(self, domain, service, entity):
        self.calls.append((service, entity)); return True


class StubMatcher:
    def suppress(self, sig, until): pass


def _handler(ledger, ha):
    async def refresh(): pass
    return ActionHandler(ledger, ha, StubMatcher(), refresh)


async def test_undo_is_idempotent():
    ledger = StubLedger(
        rows={"s": {"lifecycle": "autopilot"}},
        activity={1: {"id": 1, "signature": "s", "act_entity": "light.x",
                      "act_action": "turn_on", "undone": 1, "success": 1}},
    )
    ha = StubHA()
    await _handler(ledger, ha).handle({"action": "undo", "activity_id": 1})
    assert ha.calls == [] and ledger.undone == []


async def test_undo_of_failed_autorun_demotes_without_reversing():
    ledger = StubLedger(
        rows={"s": {"lifecycle": "autopilot"}},
        activity={1: {"id": 1, "signature": "s", "act_entity": "light.x",
                      "act_action": "turn_on", "undone": 0, "success": 0}},
    )
    ha = StubHA()
    await _handler(ledger, ha).handle({"action": "undo", "activity_id": 1})
    assert ha.calls == []                      # no reverse service call
    assert ledger.undone == [1]
    assert ledger.lifecycle_calls == [("s", "confirmed", True)]


async def test_undo_of_successful_autorun_reverses():
    ledger = StubLedger(
        rows={"s": {"lifecycle": "autopilot"}},
        activity={1: {"id": 1, "signature": "s", "act_entity": "light.x",
                      "act_action": "turn_on", "undone": 0, "success": 1}},
    )
    ha = StubHA()
    await _handler(ledger, ha).handle({"action": "undo", "activity_id": 1})
    assert ha.calls == [("turn_off", "light.x")]


async def test_promote_rejected_when_ineligible():
    row = {"lifecycle": "confirmed", "accepted_runs": 1, "dismiss_count": 0,
           "miner_type": "temporal", "entity_id": "light.x", "action": "turn_on",
           "details_json": "{}"}
    ledger = StubLedger(rows={"s": row})
    ha = StubHA()
    await _handler(ledger, ha).handle({"action": "promote", "signature": "s"})
    assert ledger.lifecycle_calls == []
    assert row["lifecycle"] == "confirmed"


async def test_demote_guard_blocks_automated_pattern():
    row = {"lifecycle": "automated", "miner_type": "temporal",
           "entity_id": "light.x", "action": "turn_on", "details_json": "{}"}
    ledger = StubLedger(rows={"s": row})
    ha = StubHA()
    await _handler(ledger, ha).handle({"action": "demote", "signature": "s"})
    assert row["lifecycle"] == "automated"
    assert ledger.lifecycle_calls == []
