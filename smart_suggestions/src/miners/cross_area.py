from __future__ import annotations
from collections import defaultdict
from candidate import Candidate, MinerType
from db_reader import StateChange


WINDOW_MINUTES = 5
MIN_OCCURRENCES = 5
MIN_CONDITIONAL_PROB = 0.7


_PRESENCE_BINARY_HINT = "motion"  # only treat binary_sensor.* as presence if name hints at motion


def _is_presence(entity_id: str) -> bool:
    if entity_id.startswith("person.") or entity_id.startswith("device_tracker."):
        return True
    if entity_id.startswith("binary_sensor.") and _PRESENCE_BINARY_HINT in entity_id:
        return True
    return False


def _is_arrival(state: str) -> bool:
    return state in {"home", "on"}


_NOISE_STATES = {"unavailable", "unknown", "none", "None"}


def _latency_bucket(seconds: float) -> str:
    """Bucket the average arrival-to-action latency. Window is bounded to 5 min by WINDOW_MINUTES."""
    assert seconds <= WINDOW_MINUTES * 60, f"latency {seconds}s exceeds window"
    if seconds <= 120:
        return "0-2m"
    return "2-5m"


class CrossAreaMiner:
    async def run(self, changes: list[StateChange]) -> list[Candidate]:
        """Mine cross-area patterns where presence/arrival triggers an entity action.

        Triggers: state changes on person.*, device_tracker.* to "home"; or on
        binary_sensor.*motion* to "on". For each trigger, looks at non-presence
        entity changes within next WINDOW_MINUTES (5 min). Conditional
        probability = (times target follows trigger) / (total trigger events).

        NOTE: presence-to-presence pairs are filtered out (e.g. person.joe
        arriving home → device_tracker.phone arriving home is excluded).
        """
        if not changes:
            return []
        ordered = sorted(changes, key=lambda c: c.ts)

        trigger_counts: dict[str, int] = defaultdict(int)
        # (trigger_entity, target_entity, target_action) -> list of latencies
        co: dict[tuple[str, str, str], list[float]] = defaultdict(list)

        for i, t in enumerate(ordered):
            if not _is_presence(t.entity_id) or not _is_arrival(t.state):
                continue
            trigger_counts[t.entity_id] += 1
            j = i + 1
            seen: set[tuple[str, str]] = set()
            while j < len(ordered) and (ordered[j].ts - t.ts).total_seconds() <= WINDOW_MINUTES * 60:
                target = ordered[j]
                j += 1
                if _is_presence(target.entity_id):
                    continue
                if target.state in _NOISE_STATES:
                    continue
                key = (target.entity_id, target.state)
                if key not in seen:
                    co[(t.entity_id, target.entity_id, target.state)].append(
                        (target.ts - t.ts).total_seconds()
                    )
                    seen.add(key)

        candidates: list[Candidate] = []
        for (trig, tgt_entity, tgt_state), latencies in co.items():
            occurrences = len(latencies)
            cond_prob = occurrences / trigger_counts[trig] if trigger_counts[trig] else 0
            if occurrences < MIN_OCCURRENCES or cond_prob < MIN_CONDITIONAL_PROB:
                continue
            avg_lat = sum(latencies) / len(latencies)
            candidates.append(
                Candidate(
                    miner_type=MinerType.CROSS_AREA,
                    entity_id=tgt_entity,
                    action=f"set_state_{tgt_state}",
                    details={
                        "trigger_entity": trig,
                        "latency_bucket": _latency_bucket(avg_lat),
                        "latency_seconds": int(round(avg_lat)),
                    },
                    occurrences=occurrences,
                    conditional_prob=cond_prob,
                )
            )
        return candidates
