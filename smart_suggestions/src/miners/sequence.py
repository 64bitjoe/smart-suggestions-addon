from __future__ import annotations
from collections import defaultdict
from candidate import Candidate, MinerType
from db_reader import StateChange


DELTA_SECONDS = 60
MIN_OCCURRENCES = 5
MIN_CONDITIONAL_PROB = 0.7


class SequenceMiner:
    async def run(self, changes: list[StateChange]) -> list[Candidate]:
        """Mine 'X then Y within Δt' candidate sequences.

        Considers only 'on' transitions in v1. For each A 'on' event, looks at
        the next ≤ DELTA_SECONDS seconds for distinct entities B that also
        turned on. Conditional probability P(B|A) = follows_with(A,B) / count(A on).
        """
        if not changes:
            return []

        followings: dict[str, int] = defaultdict(int)
        follows_with: dict[tuple[str, str], list[float]] = defaultdict(list)

        # Filter to "on" transitions only for v1.
        ons = [c for c in changes if c.state == "on"]
        ons.sort(key=lambda c: c.ts)

        for i, a in enumerate(ons):
            followings[a.entity_id] += 1
            j = i + 1
            seen_in_window: set[str] = set()
            while j < len(ons) and (ons[j].ts - a.ts).total_seconds() <= DELTA_SECONDS:
                b = ons[j]
                if b.entity_id != a.entity_id and b.entity_id not in seen_in_window:
                    follows_with[(a.entity_id, b.entity_id)].append(
                        (b.ts - a.ts).total_seconds()
                    )
                    seen_in_window.add(b.entity_id)
                j += 1

        candidates: list[Candidate] = []
        for (a, b), deltas in follows_with.items():
            occurrences = len(deltas)
            cond_prob = occurrences / followings[a] if followings[a] else 0
            if occurrences < MIN_OCCURRENCES or cond_prob < MIN_CONDITIONAL_PROB:
                continue
            avg_delta = sum(deltas) / len(deltas)
            candidates.append(
                Candidate(
                    miner_type=MinerType.SEQUENCE,
                    entity_id=a,
                    action="turn_on",
                    details={
                        "target_entity": b,
                        "target_action": "turn_on",
                        "delta_seconds": int(round(avg_delta)),
                    },
                    occurrences=occurrences,
                    conditional_prob=cond_prob,
                )
            )
        return candidates
