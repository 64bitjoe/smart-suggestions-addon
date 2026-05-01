import pytest
from datetime import datetime, timezone, timedelta
from db_reader import StateChange
from miners.temporal import TemporalMiner


def _make_changes(entity, state, days, hour, minute):
    """Generate `days` state changes at the given time-of-day, one per day."""
    base = datetime(2026, 4, 1, hour, minute, 0, tzinfo=timezone.utc)
    return [
        StateChange(entity_id=entity, state=state, ts=base + timedelta(days=d))
        for d in range(days)
    ]


async def test_finds_morning_routine_with_tight_cluster():
    changes = _make_changes("light.kitchen", "on", days=10, hour=6, minute=45)
    miner = TemporalMiner()
    candidates = await miner.run(changes, now=datetime(2026, 4, 15, tzinfo=timezone.utc))

    assert len(candidates) == 1
    c = candidates[0]
    assert c.entity_id == "light.kitchen"
    assert c.action == "turn_on"
    assert c.details["hour"] == 6
    assert 30 <= c.details["minute"] <= 60  # cluster center near 45
    assert c.occurrences >= 5
    assert c.conditional_prob >= 0.7


async def test_ignores_random_distribution():
    """If state changes are scattered across the day, no cluster should form."""
    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    # 14 changes spread across 24h, no two within an hour of each other on same day
    changes = []
    for d in range(14):
        for h in range(0, 24, 3):
            if (d + h) % 5 != 0:
                continue
            changes.append(StateChange("light.kitchen", "on", base + timedelta(days=d, hours=h)))

    miner = TemporalMiner()
    candidates = await miner.run(changes, now=base + timedelta(days=15))
    assert candidates == []


async def test_separates_by_state():
    """on at 6:45 and off at 22:30 should produce two candidates."""
    on_changes = _make_changes("light.kitchen", "on", days=10, hour=6, minute=45)
    off_changes = _make_changes("light.kitchen", "off", days=10, hour=22, minute=30)
    miner = TemporalMiner()
    candidates = await miner.run(on_changes + off_changes, now=datetime(2026, 4, 15, tzinfo=timezone.utc))

    actions = sorted(c.action for c in candidates)
    assert actions == ["turn_off", "turn_on"]
