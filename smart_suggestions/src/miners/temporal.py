from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from candidate import Candidate, MinerType
from db_reader import StateChange


# Tunable knobs (also exposed via config in a later task).
CLUSTER_WIDTH_MINUTES = 15
MIN_OCCURRENCES = 5
MIN_CONDITIONAL_PROB = 0.7


def _state_to_action(state: str) -> str | None:
    if state == "on":
        return "turn_on"
    if state == "off":
        return "turn_off"
    return None


class TemporalMiner:
    def __init__(
        self,
        min_occurrences: int = MIN_OCCURRENCES,
        min_conditional_prob: float = MIN_CONDITIONAL_PROB,
    ):
        self.min_occurrences = min_occurrences
        self.min_conditional_prob = min_conditional_prob

    async def run(
        self, changes: list[StateChange], now: datetime
    ) -> list[Candidate]:
        """Mine temporal routine candidates from state-change history.

        `now` is reserved for recency-decay weighting (not implemented in v1).
        """
        # Group: (entity_id, action) -> list of datetime
        buckets: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        for c in changes:
            action = _state_to_action(c.state)
            if action is None:
                continue
            buckets[(c.entity_id, action)].append(c.ts)

        candidates: list[Candidate] = []
        for (entity_id, action), timestamps in buckets.items():
            cluster = self._find_densest_cluster(timestamps)
            if cluster is None:
                continue
            cluster_count, center_minute_of_day, weekdays = cluster
            total_for_action = len(timestamps)
            # NOTE: We emit at most one candidate per (entity, action). An entity with
            # two equally-strong routines (e.g. morning on + evening on) splits the
            # probability and may be rejected. v1 limitation.
            cond_prob = cluster_count / total_for_action if total_for_action else 0
            if cond_prob < self.min_conditional_prob:
                continue
            candidates.append(
                Candidate(
                    miner_type=MinerType.TEMPORAL,
                    entity_id=entity_id,
                    action=action,
                    details={
                        "hour": center_minute_of_day // 60,
                        "minute": center_minute_of_day % 60,
                        "weekdays": sorted(weekdays),
                    },
                    occurrences=cluster_count,
                    conditional_prob=cond_prob,
                )
            )
        return candidates

    def _find_densest_cluster(
        self, timestamps: list[datetime]
    ) -> tuple[int, int, set[int]] | None:
        """Returns the maximum-count cluster within a 2*CLUSTER_WIDTH_MINUTES span. Ties broken by leftmost position.

        Handles the midnight wrap by extending the sorted minute axis with
        +1440 copies and sliding across the extension; the window size is
        capped at the original point count so no event is counted twice.

        Returns (count, center, weekdays_set) or None."""
        if len(timestamps) < self.min_occurrences:
            return None
        minutes_of_day = sorted((t.hour * 60 + t.minute, t.weekday()) for t in timestamps)

        best_count = 0
        best_center = 0
        best_weekdays: set[int] = set()
        width = CLUSTER_WIDTH_MINUTES

        n = len(minutes_of_day)
        extended = minutes_of_day + [(m + 1440, wd) for m, wd in minutes_of_day]
        left = 0
        for right in range(len(extended)):
            while extended[right][0] - extended[left][0] > 2 * width:
                left += 1
            # Cap at n so a window spanning both copies of the same event
            # can't double-count.
            while right - left + 1 > n:
                left += 1
            count = right - left + 1
            if count > best_count:
                best_count = count
                best_center = (
                    (extended[left][0] + extended[right][0]) // 2
                ) % 1440
                best_weekdays = {wd for _, wd in extended[left : right + 1]}

        if best_count < self.min_occurrences:
            return None
        return best_count, best_center, best_weekdays
