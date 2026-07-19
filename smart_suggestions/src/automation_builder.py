"""Deterministic pattern → HA automation config. No LLM involved."""
from __future__ import annotations
import json
import logging

_LOGGER = logging.getLogger(__name__)

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _service_for(action: str) -> str | None:
    if action in ("turn_on", "set_state_on"):
        return "homeassistant.turn_on"
    if action in ("turn_off", "set_state_off"):
        return "homeassistant.turn_off"
    return None


def _action_step(action: str, entity_id: str) -> list[dict] | None:
    service = _service_for(action)
    if service is None:
        return None
    return [{"service": service, "target": {"entity_id": entity_id}}]


def build_pattern_automation(row: dict) -> dict | None:
    d = json.loads(row["details_json"])
    mt = row["miner_type"]
    alias = row.get("title") or f"Smart Suggestion: {row['entity_id']}"

    if mt == "temporal":
        steps = _action_step(row["action"], row["entity_id"])
        if steps is None:
            return None
        weekdays = sorted(d.get("weekdays", []))
        condition = (
            [] if len(weekdays) >= 7
            else [{"condition": "time", "weekday": [_WEEKDAYS[w] for w in weekdays]}]
        )
        return {
            "alias": alias,
            "trigger": [{"platform": "time", "at": f"{d['hour']:02d}:{d['minute']:02d}:00"}],
            "condition": condition,
            "action": steps,
            "mode": "single",
        }

    if mt == "sequence":
        steps = _action_step(d["target_action"], d["target_entity"])
        if steps is None:
            return None
        return {
            "alias": alias,
            "trigger": [{"platform": "state", "entity_id": row["entity_id"], "to": "on"}],
            "condition": [],
            "action": steps,
            "mode": "single",
        }

    if mt == "cross_area":
        steps = _action_step(row["action"], row["entity_id"])
        if steps is None:
            return None
        trig = d["trigger_entity"]
        to_state = (
            "home" if trig.startswith(("person.", "device_tracker.")) else "on"
        )
        return {
            "alias": alias,
            "trigger": [{"platform": "state", "entity_id": trig, "to": to_state}],
            "condition": [],
            "action": steps,
            "mode": "single",
        }

    return None  # waste and anything unknown: not automatable


async def create_pattern_automation(row: dict, ha_client) -> dict:
    config = build_pattern_automation(row)
    if config is None:
        return {"success": False, "error": "pattern is not automatable"}
    result = await ha_client.create_automation(config)
    if not result.get("success"):
        _LOGGER.warning("create_pattern_automation failed: %s", result.get("error"))
    return result
