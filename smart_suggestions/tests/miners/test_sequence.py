import pytest
from datetime import datetime, timezone, timedelta
from db_reader import StateChange
from miners.sequence import SequenceMiner


async def test_finds_lamp_a_then_lamp_b_pattern():
    base = datetime(2026, 4, 1, 20, 0, 0, tzinfo=timezone.utc)
    changes = []
    # 6 nights: A on, then B on within 30s. Plus background noise.
    for d in range(6):
        t = base + timedelta(days=d)
        changes.append(StateChange("light.lamp_a", "on", t))
        changes.append(StateChange("light.lamp_b", "on", t + timedelta(seconds=30)))
    # Add noise: lamp_b also turns on at random other times rarely
    for d in range(2):
        changes.append(StateChange("light.lamp_b", "on", base + timedelta(days=d, hours=12)))

    changes.sort(key=lambda c: c.ts)
    miner = SequenceMiner()
    candidates = await miner.run(changes)

    sig_match = [
        c for c in candidates
        if c.entity_id == "light.lamp_a"
        and c.details.get("target_entity") == "light.lamp_b"
    ]
    assert len(sig_match) == 1
    c = sig_match[0]
    assert c.occurrences >= 5
    assert c.conditional_prob >= 0.7  # P(B|A) high
    assert c.details["delta_seconds"] <= 60


async def test_rejects_pair_without_high_conditional_prob():
    """If A turns on often but B almost never follows within 60s, reject."""
    base = datetime(2026, 4, 1, 20, 0, 0, tzinfo=timezone.utc)
    changes = []
    for d in range(20):
        changes.append(StateChange("light.lamp_a", "on", base + timedelta(days=d)))
    # B on only 2 times, not even close to A
    for d in [3, 7]:
        changes.append(StateChange("light.lamp_b", "on", base + timedelta(days=d, hours=10)))

    changes.sort(key=lambda c: c.ts)
    miner = SequenceMiner()
    candidates = await miner.run(changes)
    pair = [c for c in candidates if c.details.get("target_entity") == "light.lamp_b"]
    assert pair == []
