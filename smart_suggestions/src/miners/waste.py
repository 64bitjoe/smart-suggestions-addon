from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from statistics import median
from candidate import Candidate, MinerType
from db_reader import StateChange


MIN_DURATION_HOURS = 1
ANOMALY_MULTIPLIER = 3.0

# States that count as "device is actively on / running" for waste detection.
_ACTIVE_STATES = {"on", "heat", "cool", "heat_cool", "fan_only", "auto"}
# States that count as "device finished its on session".
_INACTIVE_STATES = {"off", "idle"}


# TODO(v2): context rule (e.g., heater on while window sensor open).
# TODO(v2): hour-of-day bucketed baseline (currently global median).
class WasteDetector:
    async def run(
        self,
        history: list[StateChange],
        current_states: dict[str, tuple[str, datetime]],
        now: datetime,
    ) -> list[Candidate]:
        """Detect entities currently 'on' that have been on far longer than baseline.

        history: 30 days of state changes, used to compute median on-duration per entity.
        current_states: {entity_id: (state, last_changed_dt)} for live state.
        now: current time (used to compute current duration).

        Emits a candidate when current duration >= MIN_DURATION_HOURS and
        >= ANOMALY_MULTIPLIER * baseline_median.
        """
        baseline = self._compute_baseline_durations(history)
        candidates: list[Candidate] = []

        for entity_id, (state, since) in current_states.items():
            if state not in _ACTIVE_STATES:
                continue
            current_dur = (now - since).total_seconds()
            if current_dur < MIN_DURATION_HOURS * 3600:
                continue

            base = baseline.get(entity_id)
            if base is None:
                continue
            if current_dur < base * ANOMALY_MULTIPLIER:
                continue

            candidates.append(
                Candidate(
                    miner_type=MinerType.WASTE,
                    entity_id=entity_id,
                    action="currently_on",
                    details={
                        "condition": "on_duration_anomaly",
                        "duration_seconds": int(current_dur),
                        "baseline_seconds": int(base),
                        "since": since.isoformat(),
                    },
                    occurrences=1,
                    conditional_prob=1.0,
                )
            )
        return candidates

    def _compute_baseline_durations(
        self, history: list[StateChange]
    ) -> dict[str, float]:
        """Return median on-duration per entity over the history window."""
        by_entity: dict[str, list[StateChange]] = defaultdict(list)
        for c in history:
            by_entity[c.entity_id].append(c)

        out: dict[str, float] = {}
        for entity_id, changes in by_entity.items():
            changes.sort(key=lambda c: c.ts)
            durations = []
            on_at: datetime | None = None
            for c in changes:
                if c.state in _ACTIVE_STATES and on_at is None:
                    on_at = c.ts
                elif c.state in _INACTIVE_STATES and on_at is not None:
                    durations.append((c.ts - on_at).total_seconds())
                    on_at = None
            if durations:
                out[entity_id] = median(durations)
        return out
