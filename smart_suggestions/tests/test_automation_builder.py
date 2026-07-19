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
