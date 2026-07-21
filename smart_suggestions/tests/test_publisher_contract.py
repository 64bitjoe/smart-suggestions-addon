# smart_suggestions/tests/test_publisher_contract.py
import json
from publisher import build_payload

# The card and panel render exactly these keys. If this contract breaks,
# suggestions silently vanish — which is precisely how v3 died.
CARD_CONTRACT = {
    "signature", "zone", "title", "description", "entity_id",
    "act_entity", "act_action", "miner_type", "confidence",
    "occurrences", "can_automate", "lifecycle", "accepted_runs",
    "can_promote",
}


def _row(miner_type="temporal", title="Porch at 20:00", act=None):
    row = {
        "signature": "abc", "miner_type": miner_type,
        "entity_id": "light.porch", "action": "turn_on",
        "details_json": json.dumps(
            {"hour": 20, "minute": 0, "weekdays": [0, 1, 2, 3, 4]}),
        "occurrences": 9, "conditional_prob": 0.83,
        "title": title, "description": "desc",
        "lifecycle": "confirmed", "accepted_runs": 0, "dismiss_count": 0,
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
                             "act_action", "undone", "success", "signature"}
    assert p["activity_id"] == 7
    assert "porch" in p["title"].lower()  # fallback title from entity
