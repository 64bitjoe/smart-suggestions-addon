import pytest
from datetime import timedelta
from candidate import Candidate, MinerType
from candidate_filter import CandidateFilter


class FakeDismissalStore:
    def __init__(self, dismissed_signatures=()):
        self.dismissed = set(dismissed_signatures)
        self.dismissals_by_miner: dict[MinerType, int] = {}

    async def is_dismissed(self, sig, within):
        return sig in self.dismissed

    async def dismissals_per_miner_in_window(self, mt, window):
        return self.dismissals_by_miner.get(mt, 0)


def _temporal(entity, occ=10, prob=0.85):
    return Candidate(
        miner_type=MinerType.TEMPORAL,
        entity_id=entity,
        action="turn_on",
        details={"hour": 6, "minute": 45, "weekdays": [0, 1, 2, 3, 4]},
        occurrences=occ,
        conditional_prob=prob,
    )


def _waste(entity):
    return Candidate(
        miner_type=MinerType.WASTE,
        entity_id=entity,
        action="currently_on",
        details={"condition": "on_duration_anomaly", "duration_seconds": 50000, "baseline_seconds": 1800},
        occurrences=1,
        conditional_prob=1.0,
    )


async def test_filters_by_min_occurrences():
    f = CandidateFilter(automated_entities=set(), dismissal_store=FakeDismissalStore())
    weak = _temporal("light.weak", occ=4)
    strong = _temporal("light.strong", occ=10)
    out = await f.filter([weak, strong])
    assert [c.entity_id for c in out] == ["light.strong"]


async def test_filters_by_conditional_prob():
    f = CandidateFilter(automated_entities=set(), dismissal_store=FakeDismissalStore())
    weak = _temporal("light.weak", prob=0.5)
    strong = _temporal("light.strong", prob=0.85)
    out = await f.filter([weak, strong])
    assert [c.entity_id for c in out] == ["light.strong"]


async def test_drops_already_automated():
    f = CandidateFilter(
        automated_entities={"light.already"},
        dismissal_store=FakeDismissalStore(),
    )
    out = await f.filter([_temporal("light.already"), _temporal("light.new")])
    assert [c.entity_id for c in out] == ["light.new"]


async def test_drops_dismissed():
    c = _temporal("light.kitchen")
    store = FakeDismissalStore(dismissed_signatures={c.signature()})
    f = CandidateFilter(automated_entities=set(), dismissal_store=store)
    out = await f.filter([c])
    assert out == []


async def test_threshold_bumps_with_dismissal_history():
    """If 3+ dismissals on same miner type in last 7d, bump threshold by 5pp."""
    store = FakeDismissalStore()
    store.dismissals_by_miner[MinerType.TEMPORAL] = 3
    f = CandidateFilter(automated_entities=set(), dismissal_store=store)
    borderline = _temporal("light.borderline", prob=0.71)  # passes default 0.7, fails 0.75
    out = await f.filter([borderline])
    assert out == []


async def test_waste_exempt_from_min_occurrences():
    """Waste candidates have occurrences=1 by design; filter must not drop them on that basis."""
    f = CandidateFilter(automated_entities=set(), dismissal_store=FakeDismissalStore())
    out = await f.filter([_waste("light.garage")])
    assert len(out) == 1
    assert out[0].entity_id == "light.garage"


async def test_waste_still_subject_to_dismissal():
    """A previously-dismissed waste candidate must still be dropped."""
    c = _waste("light.garage")
    store = FakeDismissalStore(dismissed_signatures={c.signature()})
    f = CandidateFilter(automated_entities=set(), dismissal_store=store)
    out = await f.filter([c])
    assert out == []


async def test_waste_still_subject_to_already_automated():
    """A waste candidate for an entity already in an active automation must be dropped."""
    f = CandidateFilter(
        automated_entities={"light.garage"},
        dismissal_store=FakeDismissalStore(),
    )
    out = await f.filter([_waste("light.garage")])
    assert out == []
