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
        signal_store=None,
        outcome_tracker=None,
        min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
        min_conditional_prob: float = DEFAULT_MIN_CONDITIONAL_PROB,
        # Legacy alias: allow callers passing dismissal_store= to keep working
        dismissal_store=None,
    ):
        if min_conditional_prob > MAX_THRESHOLD:
            raise ValueError(
                f"min_conditional_prob ({min_conditional_prob}) must be <= MAX_THRESHOLD ({MAX_THRESHOLD})"
            )
        self.automated_entities = automated_entities
        # Accept either signal_store= or legacy dismissal_store=
        self.signal_store = signal_store or dismissal_store
        self.outcome_tracker = outcome_tracker
        self.min_occurrences = min_occurrences
        self.min_conditional_prob = min_conditional_prob

    async def filter(self, candidates: list[Candidate]) -> list[Candidate]:
        from signal_store import SignalType

        # Compute per-miner-type effective threshold (adjusted by up/down/dismiss signals).
        thresholds: dict[MinerType, float] = {}
        for mt in MinerType:
            if self.signal_store is not None:
                # Check if signal_store is a new SignalStore (has signals_per_miner_in_window)
                # or a legacy DismissalStore (has dismissals_per_miner_in_window)
                if hasattr(self.signal_store, 'signals_per_miner_in_window'):
                    ups = await self.signal_store.signals_per_miner_in_window(mt, SignalType.UP, THRESHOLD_BUMP_WINDOW)
                    downs = await self.signal_store.signals_per_miner_in_window(mt, SignalType.DOWN, THRESHOLD_BUMP_WINDOW)
                    dismisses = await self.signal_store.signals_per_miner_in_window(mt, SignalType.DISMISS, THRESHOLD_BUMP_WINDOW)
                    net_negative = downs + dismisses - ups
                    bumps = max(-3, net_negative // 3)
                else:
                    # Legacy DismissalStore fallback
                    n = await self.signal_store.dismissals_per_miner_in_window(mt, THRESHOLD_BUMP_WINDOW)
                    bumps = n // 3
            else:
                bumps = 0
            # Allow negative bumps (from upvotes) to lower threshold below the baseline,
            # but floor at 0.5 to avoid degenerate cases.
            thresholds[mt] = max(
                0.5,
                min(MAX_THRESHOLD, self.min_conditional_prob + bumps * THRESHOLD_BUMP_PER_3_DISMISSALS),
            )

        # Apply outcome-tracker secondary signal
        if self.outcome_tracker is not None:
            try:
                outcome_stats = await self.outcome_tracker.stats_per_miner(window=timedelta(days=7))
                for mt in MinerType:
                    stat = outcome_stats.get(mt.value)
                    if stat and stat.suggested >= 3:
                        follow_through_rate = stat.acted_on / stat.suggested
                        if follow_through_rate > 0.5:
                            thresholds[mt] -= 0.05  # users like this miner type, loosen
                        elif follow_through_rate < 0.2:
                            thresholds[mt] += 0.05  # users ignore this miner type, tighten
                    thresholds[mt] = max(0.5, min(MAX_THRESHOLD, thresholds[mt]))
            except Exception:
                pass  # outcome_tracker failure must not break filtering

        survivors: list[Candidate] = []
        for c in candidates:
            if c.miner_type not in _FREQUENCY_EXEMPT and c.occurrences < self.min_occurrences:
                continue
            if c.conditional_prob < thresholds[c.miner_type]:
                continue
            if c.entity_id in self.automated_entities:
                continue
            if self.signal_store is not None and await self.signal_store.is_dismissed(c.signature(), DISMISSAL_WINDOW):
                continue
            survivors.append(c)
        return survivors
