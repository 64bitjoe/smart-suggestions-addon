import pytest
from candidate import Candidate, MinerType
from candidate_filter import CandidateFilter
from signal_store import SignalType


class FakeSignalStore:
    def __init__(self, dismissed_signatures=()):
        self.dismissed = set(dismissed_signatures)
        self.signals_by_miner: dict[tuple, int] = {}

    async def is_dismissed(self, sig, within):
        return sig in self.dismissed

    async def signals_per_miner_in_window(self, mt, signal_type, window):
        return self.signals_by_miner.get((mt, signal_type), 0)


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
    f = CandidateFilter(automated_entities=set(), signal_store=FakeSignalStore())
    weak = _temporal("light.weak", occ=4)
    strong = _temporal("light.strong", occ=10)
    out = await f.filter([weak, strong])
    assert [c.entity_id for c in out] == ["light.strong"]


async def test_filters_by_conditional_prob():
    f = CandidateFilter(automated_entities=set(), signal_store=FakeSignalStore())
    weak = _temporal("light.weak", prob=0.5)
    strong = _temporal("light.strong", prob=0.85)
    out = await f.filter([weak, strong])
    assert [c.entity_id for c in out] == ["light.strong"]


async def test_drops_already_automated():
    f = CandidateFilter(
        automated_entities={"light.already"},
        signal_store=FakeSignalStore(),
    )
    out = await f.filter([_temporal("light.already"), _temporal("light.new")])
    assert [c.entity_id for c in out] == ["light.new"]


async def test_drops_dismissed():
    c = _temporal("light.kitchen")
    store = FakeSignalStore(dismissed_signatures={c.signature()})
    f = CandidateFilter(automated_entities=set(), signal_store=store)
    out = await f.filter([c])
    assert out == []


async def test_threshold_bumps_with_dismissal_history():
    """If 3+ dismissals on same miner type in last 7d, bump threshold by 5pp."""
    store = FakeSignalStore()
    store.signals_by_miner[(MinerType.TEMPORAL, SignalType.DISMISS)] = 3
    f = CandidateFilter(automated_entities=set(), signal_store=store)
    borderline = _temporal("light.borderline", prob=0.71)  # passes default 0.7, fails 0.75
    out = await f.filter([borderline])
    assert out == []


async def test_upvotes_lower_threshold():
    """3 ups - 0 downs - 0 dismissals = net_negative=-3 -> bumps=max(-3,-1)=-1 -> threshold lowered by 0.05."""
    store = FakeSignalStore()
    store.signals_by_miner[(MinerType.TEMPORAL, SignalType.UP)] = 3
    f = CandidateFilter(automated_entities=set(), signal_store=store)
    # Would normally pass at 0.7 anyway; let's verify it still passes and that lower threshold
    # allows a candidate that would normally be at min_conditional_prob - epsilon
    # With bumps=-1 threshold = max(0.5, 0.7 + (-1)*0.05) = 0.65
    borderline = _temporal("light.borderline", prob=0.67)  # fails default 0.7, passes lowered 0.65
    out = await f.filter([borderline])
    assert [c.entity_id for c in out] == ["light.borderline"]


async def test_waste_exempt_from_min_occurrences():
    """Waste candidates have occurrences=1 by design; filter must not drop them on that basis."""
    f = CandidateFilter(automated_entities=set(), signal_store=FakeSignalStore())
    out = await f.filter([_waste("light.garage")])
    assert len(out) == 1
    assert out[0].entity_id == "light.garage"


async def test_waste_still_subject_to_dismissal():
    """A previously-dismissed waste candidate must still be dropped."""
    c = _waste("light.garage")
    store = FakeSignalStore(dismissed_signatures={c.signature()})
    f = CandidateFilter(automated_entities=set(), signal_store=store)
    out = await f.filter([c])
    assert out == []


async def test_waste_still_subject_to_already_automated():
    """A waste candidate for an entity already in an active automation must be dropped."""
    f = CandidateFilter(
        automated_entities={"light.garage"},
        signal_store=FakeSignalStore(),
    )
    out = await f.filter([_waste("light.garage")])
    assert out == []


async def test_threshold_bump_caps_at_max():
    """Many dismissals (e.g. 30 → 10 bumps × 0.05 = 0.5) should cap at 0.9, not exceed it."""
    store = FakeSignalStore()
    store.signals_by_miner[(MinerType.TEMPORAL, SignalType.DISMISS)] = 30
    f = CandidateFilter(automated_entities=set(), signal_store=store)
    # A candidate at 0.89 should fail (cap is 0.9), a candidate at 0.91 should pass.
    fails = _temporal("light.fails", prob=0.89)
    passes = _temporal("light.passes", prob=0.91)
    out = await f.filter([fails, passes])
    assert [c.entity_id for c in out] == ["light.passes"]


async def test_threshold_bump_two_steps():
    """6 dismissals → 2 bumps → threshold 0.80."""
    store = FakeSignalStore()
    store.signals_by_miner[(MinerType.TEMPORAL, SignalType.DISMISS)] = 6
    f = CandidateFilter(automated_entities=set(), signal_store=store)
    borderline = _temporal("light.borderline", prob=0.79)  # passes 0.75 (1 bump), fails 0.80 (2 bumps)
    out = await f.filter([borderline])
    assert out == []


def test_constructor_rejects_min_prob_above_cap():
    with pytest.raises(ValueError, match="must be <="):
        CandidateFilter(
            automated_entities=set(),
            signal_store=FakeSignalStore(),
            min_conditional_prob=0.95,  # > MAX_THRESHOLD
        )


async def test_no_store_does_not_crash():
    """CandidateFilter works with no signal_store at all."""
    f = CandidateFilter(automated_entities=set())
    out = await f.filter([_temporal("light.x")])
    assert [c.entity_id for c in out] == ["light.x"]
