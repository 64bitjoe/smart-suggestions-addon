"""Handle user actions on suggestions: run / accept / dismiss / snooze."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone

from automation_builder import create_pattern_automation

_LOGGER = logging.getLogger(__name__)

RUN_SUPPRESS = timedelta(hours=2)
SNOOZE_FOR = timedelta(hours=24)

_SERVICE = {"turn_on": "turn_on", "set_state_on": "turn_on",
            "turn_off": "turn_off", "set_state_off": "turn_off",
            "currently_on": "turn_off"}


class ActionHandler:
    def __init__(self, ledger, ha_client, matcher, refresh_cb):
        self._ledger = ledger
        self._ha = ha_client
        self._matcher = matcher
        self._refresh = refresh_cb

    async def handle(self, data: dict) -> None:
        action = data.get("action")
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
        else:
            _LOGGER.warning("unknown action: %s", action)
            return
        await self._refresh()

    @staticmethod
    def _resolve_act(row: dict) -> tuple[str, str]:
        if row["miner_type"] == "sequence":
            d = json.loads(row["details_json"])
            return d["target_entity"], d["target_action"]
        return row["entity_id"], row["action"]
