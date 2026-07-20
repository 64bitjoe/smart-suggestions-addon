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
