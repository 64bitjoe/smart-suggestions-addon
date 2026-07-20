# smart_suggestions/tests/test_main_wiring.py
"""Wiring tests for the auto-run execution loop (match_once)."""
from datetime import datetime, timezone

from main import SmartSuggestionsAddon


class StubLedger:
    def __init__(self, rows, autoruns=0):
        self.rows, self.autoruns = rows, autoruns
        self.activity = []
    async def get_rows(self, lifecycles):
        return [r for r in self.rows if r["lifecycle"] in lifecycles]
    async def autoruns_since(self, ts):
        return self.autoruns + len(self.activity)
    async def add_activity(self, ts, sig, e, a):
        self.activity.append(sig); return len(self.activity)
    async def record_run(self, sig): pass
    async def recent_activity(self, since_ts, limit=15): return []
    async def lifecycle_counts(self): return {}


class StubMatcher:
    def match(self, rows, states, now): return list(rows)
    def suppress(self, sig, until): pass


class StubHA:
    def __init__(self): self.calls = []
    def get_states(self): return {}
    async def call_service(self, domain, service, entity):
        self.calls.append((service, entity)); return True


class StubPublisher:
    def __init__(self): self.published = None
    async def publish(self, now_items, disc, noticed, activity):
        self.published = {"now": now_items, "discoveries": disc}
        return {"now": [], "discoveries": [], "noticed": [], "activity": []}


def _row(sig, lifecycle, action="turn_on"):
    return {"signature": sig, "lifecycle": lifecycle, "miner_type": "temporal",
            "entity_id": "light.x", "action": action,
            "details_json": "{}", "occurrences": 5, "conditional_prob": 0.9,
            "accepted_runs": 0, "dismiss_count": 0, "title": "t",
            "description": "", "snoozed_until": None, "last_seen": 0.0,
            "act_entity": "light.x", "act_action": action}


async def _addon_with(rows, autoruns=0):
    addon = SmartSuggestionsAddon({})
    addon._ledger = StubLedger(rows, autoruns)
    addon._matcher = StubMatcher()
    ha = StubHA()
    addon._ha = ha
    addon._action_handler._ledger = addon._ledger
    addon._action_handler._ha = ha
    addon._action_handler._matcher = addon._matcher
    addon._publisher = StubPublisher()
    addon._ws_server.set_zones = lambda z: None
    async def _noop(): pass
    addon._ws_server.broadcast_zones = _noop
    addon._tz = timezone.utc
    return addon, ha


async def test_autopilot_match_executes_and_logs():
    addon, ha = await _addon_with([_row("a", "autopilot")])
    await addon.match_once()
    assert ha.calls == [("turn_on", "light.x")]
    assert addon._ledger.activity == ["a"]
    assert addon._now_items == []


async def test_confirmed_match_suggests_not_executes():
    addon, ha = await _addon_with([_row("b", "confirmed")])
    await addon.match_once()
    assert ha.calls == []
    assert [r["signature"] for r in addon._now_items] == ["b"]


async def test_throttle_trip_falls_back_to_suggestion():
    addon, ha = await _addon_with([_row("c", "autopilot")], autoruns=10)
    await addon.match_once()
    assert ha.calls == []
    assert [r["signature"] for r in addon._now_items] == ["c"]


async def test_unmapped_action_falls_back_to_suggestion():
    addon, ha = await _addon_with([_row("d", "autopilot", action="weird")])
    addon._ledger.rows[0]["act_action"] = "weird"
    await addon.match_once()
    assert ha.calls == []
    assert [r["signature"] for r in addon._now_items] == ["d"]
