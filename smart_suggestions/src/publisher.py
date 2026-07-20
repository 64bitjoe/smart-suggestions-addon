# smart_suggestions/src/publisher.py
"""Ledger rows → HA sensor payloads. build_payload() is the card contract."""
from __future__ import annotations
import logging

from llm_describer import template_description

_LOGGER = logging.getLogger(__name__)

SENSOR_NOW = "sensor.smart_suggestions_now"
SENSOR_DISCOVERIES = "sensor.smart_suggestions_discoveries"
SENSOR_NOTICED = "sensor.smart_suggestions_noticed"


def build_payload(row: dict, zone: str) -> dict:
    title = row.get("title")
    description = row.get("description")
    if not title:
        title, tdesc = template_description(row)
        description = description or tdesc
    act_entity = row.get("act_entity", row["entity_id"])
    act_action = row.get("act_action", row["action"])
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
    }


class Publisher:
    def __init__(self, ha_client):
        self._ha = ha_client

    async def publish(
        self, now_items: list[dict], discovery_rows: list[dict],
        noticed_rows: list[dict],
    ) -> dict:
        zones = {
            "now": [build_payload(r, "now") for r in now_items],
            "discoveries": [build_payload(r, "discoveries") for r in discovery_rows],
            "noticed": [build_payload(r, "noticed") for r in noticed_rows],
        }
        for sensor, key, name in (
            (SENSOR_NOW, "now", "Smart Suggestions: Now"),
            (SENSOR_DISCOVERIES, "discoveries", "Smart Suggestions: Discoveries"),
            (SENSOR_NOTICED, "noticed", "Smart Suggestions: Noticed"),
        ):
            await self._ha.push_sensor(
                sensor, str(len(zones[key])),
                {"suggestions": zones[key], "friendly_name": name},
            )
        return zones
