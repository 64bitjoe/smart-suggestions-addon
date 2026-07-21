"""Handle user actions on suggestions: run / accept / dismiss / snooze."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone

from automation_builder import create_pattern_automation
from lifecycle import LIFECYCLE_AUTOPILOT, LIFECYCLE_CONFIRMED, can_promote

_LOGGER = logging.getLogger(__name__)

RUN_SUPPRESS = timedelta(hours=2)
SNOOZE_FOR = timedelta(hours=24)

_SERVICE = {"turn_on": "turn_on", "set_state_on": "turn_on",
            "turn_off": "turn_off", "set_state_off": "turn_off",
            "currently_on": "turn_off"}

_REVERSE = {"turn_on": "turn_off", "turn_off": "turn_on",
            "set_state_on": "turn_off", "set_state_off": "turn_on",
            "currently_on": "turn_on"}


def reverse_service(act_action: str) -> str | None:
    """Service that undoes an auto-run action; None if not reversible."""
    return _REVERSE.get(act_action)


class ActionHandler:
    def __init__(self, ledger, ha_client, matcher, refresh_cb):
        self._ledger = ledger
        self._ha = ha_client
        self._matcher = matcher
        self._refresh = refresh_cb

    async def handle(self, data: dict) -> None:
        action = data.get("action")
        if action == "undo":
            await self._undo(data.get("activity_id"))
            await self._refresh()
            return
        sig = data.get("signature")
        if not action or not sig:
            return
        row = await self._ledger.get(sig)
        if row is None:
            _LOGGER.warning("action %s for unknown signature %s", action, sig)
            return
        now = datetime.now(timezone.utc)

        if action == "run":
            act_entity, act_action = self._resolve_act(row)
            service = _SERVICE.get(act_action)
            if service:
                ok = await self._ha.call_service("homeassistant", service, act_entity)
                if ok:
                    await self._ledger.record_run(sig)
            self._matcher.suppress(sig, now + RUN_SUPPRESS)
        elif action == "accept":
            if row["lifecycle"] == "automated":
                return
            result = await create_pattern_automation(row, self._ha)
            if result.get("success"):
                await self._ledger.mark_automated(
                    sig, result.get("automation_id", ""))
        elif action == "dismiss":
            await self._ledger.dismiss(sig, now)
            self._matcher.suppress(sig, now + timedelta(days=14))
        elif action == "snooze":
            await self._ledger.snooze(sig, (now + SNOOZE_FOR).timestamp())
        elif action == "promote":
            act_entity, _ = self._resolve_act(row)
            if can_promote(row, act_entity):
                ok = await self._ledger.set_lifecycle(
                    sig, LIFECYCLE_AUTOPILOT, expected=LIFECYCLE_CONFIRMED
                )
                if ok:
                    _LOGGER.info("promoted to autopilot: %s", sig)
                else:
                    _LOGGER.warning(
                        "promote guard blocked transition (lifecycle changed "
                        "underneath us): %s", sig,
                    )
            else:
                _LOGGER.warning("promote rejected (not eligible): %s", sig)
                return
        elif action == "demote":
            ok = await self._ledger.set_lifecycle(
                sig, LIFECYCLE_CONFIRMED, reset_runs=True,
                expected=LIFECYCLE_AUTOPILOT,
            )
            if ok:
                _LOGGER.info("demoted to suggest-only: %s", sig)
            else:
                _LOGGER.warning(
                    "demote guard blocked transition (lifecycle changed "
                    "underneath us): %s", sig,
                )
        else:
            _LOGGER.warning("unknown action: %s", action)
            return
        await self._refresh()

    async def _undo(self, activity_id) -> None:
        try:
            activity_id = int(activity_id)
        except (TypeError, ValueError):
            return
        entry = await self._ledger.get_activity(activity_id)
        if entry is None or entry["undone"]:
            return  # idempotent
        # Reversing a FAILED autorun could toggle a device the user set
        # manually — a failed run only demotes.
        service = reverse_service(entry["act_action"])
        if service and entry.get("success", 1):
            await self._ha.call_service(
                "homeassistant", service, entry["act_entity"]
            )
        await self._ledger.mark_activity_undone(activity_id)
        # Veto: back to suggest-only, trust re-earned from zero.
        ok = await self._ledger.set_lifecycle(
            entry["signature"], LIFECYCLE_CONFIRMED, reset_runs=True,
            expected=LIFECYCLE_AUTOPILOT,
        )
        if ok:
            _LOGGER.info("undo: reversed activity %d, demoted %s",
                         activity_id, entry["signature"])
        else:
            _LOGGER.warning(
                "undo: reversed activity %d but demote guard blocked "
                "(pattern no longer autopilot): %s",
                activity_id, entry["signature"],
            )

    async def execute_autorun(self, row: dict, now) -> bool:
        """Execute a matched autopilot pattern. Returns True when executed."""
        act_entity, act_action = row.get("act_entity"), row.get("act_action")
        if not act_entity:
            act_entity, act_action = self._resolve_act(row)
        service = _SERVICE.get(act_action)
        if service is None:
            return False
        ok = await self._ha.call_service("homeassistant", service, act_entity)
        # Log even on failure (spec): the window fired; undo of a failed run
        # simply demotes.
        await self._ledger.add_activity(
            now.timestamp(), row["signature"], act_entity, act_action,
            success=ok,
        )
        if ok:
            await self._ledger.record_run(row["signature"])
        self._matcher.suppress(row["signature"], now + RUN_SUPPRESS)
        if not ok:
            _LOGGER.warning("autorun service call failed: %s %s",
                            service, act_entity)
        return True

    @staticmethod
    def _resolve_act(row: dict) -> tuple[str, str]:
        if row["miner_type"] == "sequence":
            d = json.loads(row["details_json"])
            return d["target_entity"], d["target_action"]
        return row["entity_id"], row["action"]
