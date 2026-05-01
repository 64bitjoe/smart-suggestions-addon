import pytest
from datetime import datetime, timezone, timedelta
from db_reader import StateChange
from miners.cross_area import CrossAreaMiner


async def test_arrival_home_triggers_office_heater():
    base = datetime(2026, 4, 1, 17, 30, 0, tzinfo=timezone.utc)
    changes = []
    for d in range(7):  # 7 weekdays
        t = base + timedelta(days=d)
        changes.append(StateChange("person.joe", "home", t))
        changes.append(StateChange("climate.office", "heat", t + timedelta(minutes=4)))

    changes.sort(key=lambda c: c.ts)
    miner = CrossAreaMiner()
    candidates = await miner.run(changes)

    matching = [
        c for c in candidates
        if c.details.get("trigger_entity") == "person.joe"
        and c.entity_id == "climate.office"
    ]
    assert len(matching) == 1
    c = matching[0]
    assert c.occurrences >= 5
    assert c.conditional_prob >= 0.7
    assert c.details["latency_bucket"] in {"0-2m", "2-5m"}


async def test_ignores_presence_to_presence_pairs():
    base = datetime(2026, 4, 1, 17, 30, 0, tzinfo=timezone.utc)
    changes = []
    for d in range(7):
        t = base + timedelta(days=d)
        changes.append(StateChange("person.joe", "home", t))
        changes.append(StateChange("device_tracker.phone", "home", t + timedelta(seconds=10)))
    miner = CrossAreaMiner()
    candidates = await miner.run(changes)
    assert candidates == []


async def test_ignores_unavailable_target_states():
    """A target that bounces to 'unavailable' within the window should not produce a candidate."""
    base = datetime(2026, 4, 1, 17, 30, 0, tzinfo=timezone.utc)
    changes = []
    for d in range(7):
        t = base + timedelta(days=d)
        changes.append(StateChange("person.joe", "home", t))
        changes.append(StateChange("climate.office", "unavailable", t + timedelta(seconds=30)))
    miner = CrossAreaMiner()
    candidates = await miner.run(changes)
    matching = [c for c in candidates if c.entity_id == "climate.office"]
    assert matching == []
