# smart_suggestions/src/publisher.py
"""Ledger rows → HA sensor payloads. build_payload() is the card contract."""
from __future__ import annotations
import json
import logging

from lifecycle import can_promote
from llm_describer import template_description, _friendly

_LOGGER = logging.getLogger(__name__)

SENSOR_NOW = "sensor.smart_suggestions_now"
SENSOR_DISCOVERIES = "sensor.smart_suggestions_discoveries"
SENSOR_NOTICED = "sensor.smart_suggestions_noticed"
SENSOR_ACTIVITY = "sensor.smart_suggestions_activity"

# Sensor attributes have practical size limits (recorder warns >16KB) and a
# thousand-row discoveries list is useless UX anyway. Zones are pre-sorted
# by conditional_prob (ledger ORDER BY), so a slice keeps the best.
MAX_DISCOVERIES = 50
MAX_NOTICED = 20


def build_payload(row: dict, zone: str) -> dict:
    title = row.get("title")
    description = row.get("description")
    if not title:
        title, tdesc = template_description(row)
        description = description or tdesc
    act_entity = row.get("act_entity")
    act_action = row.get("act_action")
    if act_entity is None:
        if row["miner_type"] == "sequence":
            d = json.loads(row["details_json"])
            act_entity = d.get("target_entity", row["entity_id"])
            act_action = d.get("target_action", row["action"])
        else:
            act_entity, act_action = row["entity_id"], row["action"]
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
        "lifecycle": row.get("lifecycle", ""),
        "accepted_runs": row.get("accepted_runs", 0),
        "can_promote": can_promote(row, act_entity),
    }


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


class Publisher:
    def __init__(self, ha_client):
        self._ha = ha_client

    async def publish(
        self, now_items: list[dict], discovery_rows: list[dict],
        noticed_rows: list[dict], activity_entries: list[dict],
    ) -> dict:
        if len(discovery_rows) > MAX_DISCOVERIES:
            _LOGGER.info(
                "publish: %d discoveries, showing top %d",
                len(discovery_rows), MAX_DISCOVERIES,
            )
        zones = {
            "now": [build_payload(r, "now") for r in now_items],
            "discoveries": [
                build_payload(r, "discoveries")
                for r in discovery_rows[:MAX_DISCOVERIES]
            ],
            "noticed": [build_payload(r, "noticed") for r in noticed_rows[:MAX_NOTICED]],
            "activity": [build_activity_payload(e) for e in activity_entries],
        }
        for sensor, key, name in (
            (SENSOR_NOW, "now", "Smart Suggestions: Now"),
            (SENSOR_DISCOVERIES, "discoveries", "Smart Suggestions: Discoveries"),
            (SENSOR_NOTICED, "noticed", "Smart Suggestions: Noticed"),
            (SENSOR_ACTIVITY, "activity", "Smart Suggestions: Auto-Pilot"),
        ):
            await self._ha.push_sensor(
                sensor, str(len(zones[key])),
                {"suggestions": zones[key], "friendly_name": name},
            )
        return zones
