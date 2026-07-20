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
