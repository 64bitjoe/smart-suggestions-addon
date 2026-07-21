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
    async def add_activity(self, ts, sig, e, a, success=True):
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


class StubHADevices(StubHA):
    def __init__(self, device_map):
        super().__init__()
        self._devices = device_map
    async def get_device_id(self, entity_id):
        return self._devices.get(entity_id)


async def test_dedup_mirrors_renamed_pair_same_device():
    from candidate import Candidate, MinerType
    addon = SmartSuggestionsAddon({})
    addon._ha = StubHADevices({
        "light.third_reality": "devA",
        "switch.third_reality_2": "devA",
        "light.other": "devB",
    })
    mk = lambda eid: Candidate(
        miner_type=MinerType.TEMPORAL, entity_id=eid, action="turn_on",
        details={"hour": 9, "minute": 3, "weekdays": [1, 2, 3]},
        occurrences=4, conditional_prob=0.7,
    )
    out = await addon._dedup_mirrors(
        [mk("light.third_reality"), mk("switch.third_reality_2"), mk("light.other")]
    )
    ids = sorted(c.entity_id for c in out)
    # Renamed wrapper pair collapses to the light; unrelated entity survives.
    assert ids == ["light.other", "light.third_reality"]


async def test_dedup_mirrors_different_devices_not_collapsed():
    from candidate import Candidate, MinerType
    addon = SmartSuggestionsAddon({})
    addon._ha = StubHADevices({
        "light.lamp": "devA",
        "switch.lamp_2": "devC",   # genuinely different device
    })
    mk = lambda eid: Candidate(
        miner_type=MinerType.TEMPORAL, entity_id=eid, action="turn_on",
        details={"hour": 9, "minute": 3, "weekdays": [1]},
        occurrences=4, conditional_prob=0.7,
    )
    out = await addon._dedup_mirrors([mk("light.lamp"), mk("switch.lamp_2")])
    assert len(out) == 2
