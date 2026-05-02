from __future__ import annotations
from datetime import timedelta
from candidate import Candidate, MinerType


DEFAULT_MIN_OCCURRENCES = 5
DEFAULT_MIN_CONDITIONAL_PROB = 0.7
DISMISSAL_WINDOW = timedelta(days=14)
THRESHOLD_BUMP_WINDOW = timedelta(days=7)
THRESHOLD_BUMP_PER_3_DISMISSALS = 0.05
MAX_THRESHOLD = 0.9

# Miner types that are exempt from the min-occurrences gate. WasteDetector
# emits occurrences=1 by design (one current anomaly observation), not 5+.
_FREQUENCY_EXEMPT: frozenset[MinerType] = frozenset({MinerType.WASTE})


class CandidateFilter:
    def __init__(
        self,
        automated_entities: set[str],
        dismissal_store,
        min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
        min_conditional_prob: float = DEFAULT_MIN_CONDITIONAL_PROB,
    ):
        if min_conditional_prob > MAX_THRESHOLD:
            raise ValueError(
                f"min_conditional_prob ({min_conditional_prob}) must be <= MAX_THRESHOLD ({MAX_THRESHOLD})"
            )
        self.automated_entities = automated_entities
        self.dismissal_store = dismissal_store
        self.min_occurrences = min_occurrences
        self.min_conditional_prob = min_conditional_prob

    async def filter(self, candidates: list[Candidate]) -> list[Candidate]:
        # Compute per-miner-type effective threshold (potentially bumped by dismissals).
        thresholds: dict[MinerType, float] = {}
        for mt in MinerType:
            n = await self.dismissal_store.dismissals_per_miner_in_window(
                mt, THRESHOLD_BUMP_WINDOW
            )
            bumps = n // 3
            thresholds[mt] = min(
                MAX_THRESHOLD,
                self.min_conditional_prob + bumps * THRESHOLD_BUMP_PER_3_DISMISSALS,
            )

        survivors: list[Candidate] = []
        for c in candidates:
            if c.miner_type not in _FREQUENCY_EXEMPT and c.occurrences < self.min_occurrences:
                continue
            if c.conditional_prob < thresholds[c.miner_type]:
                continue
            if c.entity_id in self.automated_entities:
                continue
            if await self.dismissal_store.is_dismissed(c.signature(), DISMISSAL_WINDOW):
                continue
            survivors.append(c)
        return survivors
