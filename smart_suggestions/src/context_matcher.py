"""Match confirmed patterns against the current moment. Pure Python, no LLM."""
from __future__ import annotations
import json
import logging
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

TEMPORAL_WINDOW_MINUTES = 30
CROSS_AREA_WINDOW_SECONDS = 300
SEQUENCE_GRACE_SECONDS = 120

_DESIRED_STATE = {"turn_on": "on", "set_state_on": "on",
                  "turn_off": "off", "set_state_off": "off"}


def _entity_state(states: dict, eid: str) -> str | None:
    s = states.get(eid)
    return s.get("state") if s else None


def _changed_ago_seconds(states: dict, eid: str, now: datetime) -> float | None:
    s = states.get(eid)
    if not s or not s.get("last_changed"):
        return None
    try:
        changed = datetime.fromisoformat(s["last_changed"].replace("Z", "+00:00"))
        return (now - changed).total_seconds()
    except (ValueError, TypeError):
        return None


class ContextMatcher:
    def __init__(self, max_now: int = 5):
        self.max_now = max_now
        self._suppressed: dict[str, datetime] = {}

    def suppress(self, signature: str, until: datetime) -> None:
        self._suppressed[signature] = until

    def _is_suppressed(self, signature: str, now: datetime) -> bool:
        until = self._suppressed.get(signature)
        if until is None:
            return False
        if now >= until:
            del self._suppressed[signature]
            return False
        return True

    def match(self, rows: list[dict], states: dict, now: datetime) -> list[dict]:
        matched: list[dict] = []
        for row in rows:
            if self._is_suppressed(row["signature"], now):
                continue
            try:
                d = json.loads(row["details_json"])
                act = self._match_one(row, d, states, now)
            except Exception:
                _LOGGER.exception("matcher error on %s", row.get("signature"))
                continue
            if act is not None:
                enriched = dict(row)
                enriched["act_entity"], enriched["act_action"] = act
                matched.append(enriched)
        matched.sort(key=lambda r: r["conditional_prob"], reverse=True)
        return matched[: self.max_now]

    def _match_one(
        self, row: dict, d: dict, states: dict, now: datetime
    ) -> tuple[str, str] | None:
        mt = row["miner_type"]
        if mt == "temporal":
            minute_now = now.hour * 60 + now.minute
            minute_pat = d["hour"] * 60 + d["minute"]
            if abs(minute_now - minute_pat) > TEMPORAL_WINDOW_MINUTES:
                return None
            if now.weekday() not in d.get("weekdays", []):
                return None
            desired = _DESIRED_STATE.get(row["action"])
            if desired and _entity_state(states, row["entity_id"]) == desired:
                return None
            return row["entity_id"], row["action"]

        if mt == "sequence":
            ago = _changed_ago_seconds(states, row["entity_id"], now)
            if ago is None or _entity_state(states, row["entity_id"]) != "on":
                return None
            if ago > d["delta_seconds"] + SEQUENCE_GRACE_SECONDS:
                return None
            desired = _DESIRED_STATE.get(d["target_action"], "on")
            if _entity_state(states, d["target_entity"]) == desired:
                return None
            return d["target_entity"], d["target_action"]

        if mt == "cross_area":
            trig = d["trigger_entity"]
            if _entity_state(states, trig) not in ("home", "on"):
                return None
            ago = _changed_ago_seconds(states, trig, now)
            if ago is None or ago > CROSS_AREA_WINDOW_SECONDS:
                return None
            desired = _DESIRED_STATE.get(row["action"])
            if desired and _entity_state(states, row["entity_id"]) == desired:
                return None
            return row["entity_id"], row["action"]

        return None  # waste is never a "now" one-tap
