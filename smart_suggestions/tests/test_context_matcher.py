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
    m.suppress("t1", NOW + timedelta(minutes=5))
    assert m.match([_temporal_row()], states, NOW) == []
    assert len(m.match([_temporal_row()], states, NOW + timedelta(minutes=10))) == 1


def test_max_now_caps_and_ranks():
    rows = [_temporal_row(sig=f"t{i}", prob=0.5 + i * 0.05) for i in range(8)]
    for i, r in enumerate(rows):
        r["entity_id"] = f"light.l{i}"
    states = {f"light.l{i}": _state("off") for i in range(8)}
    out = ContextMatcher(max_now=5).match(rows, states, NOW)
    assert len(out) == 5
    assert out[0]["conditional_prob"] >= out[-1]["conditional_prob"]
