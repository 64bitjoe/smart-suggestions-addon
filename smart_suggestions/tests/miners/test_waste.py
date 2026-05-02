import pytest
from datetime import datetime, timezone, timedelta
from db_reader import StateChange
from miners.waste import WasteDetector


async def test_detects_garage_light_left_on_far_longer_than_baseline():
    """Baseline: garage light typically on for ~30 min in afternoon. Today: 14h."""
    now = datetime(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc)
    history = []
    # 30 days of normal: on for 30 min around 11am
    for d in range(1, 31):
        on_t = now - timedelta(days=d, hours=3)  # 11am d days ago
        history.append(StateChange("light.garage", "on", on_t))
        history.append(StateChange("light.garage", "off", on_t + timedelta(minutes=30)))
    # Today: turned on 14 hours ago, still on
    today_on = now - timedelta(hours=14)
    history.append(StateChange("light.garage", "on", today_on))
    current = {"light.garage": ("on", today_on)}

    detector = WasteDetector()
    candidates = await detector.run(history, current_states=current, now=now)

    matching = [c for c in candidates if c.entity_id == "light.garage"]
    assert len(matching) == 1
    c = matching[0]
    assert c.action == "currently_on"
    assert c.details["duration_seconds"] >= 14 * 3600
    assert c.details["baseline_seconds"] <= 60 * 60


async def test_does_not_flag_normal_duration():
    now = datetime(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc)
    history = []
    for d in range(1, 31):
        on_t = now - timedelta(days=d, hours=3)
        history.append(StateChange("light.garage", "on", on_t))
        history.append(StateChange("light.garage", "off", on_t + timedelta(minutes=30)))
    # Today: on for 20 minutes, normal
    today_on = now - timedelta(minutes=20)
    history.append(StateChange("light.garage", "on", today_on))
    current = {"light.garage": ("on", today_on)}

    detector = WasteDetector()
    candidates = await detector.run(history, current_states=current, now=now)
    assert candidates == []


async def test_detects_climate_left_on_far_longer_than_baseline():
    """Heater on for 8 hours when baseline is 1 hour should fire."""
    now = datetime(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc)
    history = []
    # 30 days: heater on for 1 hour at 6pm
    for d in range(1, 31):
        on_t = now - timedelta(days=d) + timedelta(hours=4)  # 6pm d days ago
        history.append(StateChange("climate.living_room", "heat", on_t))
        history.append(StateChange("climate.living_room", "off", on_t + timedelta(hours=1)))
    # Today: turned on 8 hours ago, still in heat mode
    today_on = now - timedelta(hours=8)
    history.append(StateChange("climate.living_room", "heat", today_on))
    current = {"climate.living_room": ("heat", today_on)}

    detector = WasteDetector()
    candidates = await detector.run(history, current_states=current, now=now)

    matching = [c for c in candidates if c.entity_id == "climate.living_room"]
    assert len(matching) == 1
    assert matching[0].action == "currently_on"
    assert matching[0].details["duration_seconds"] >= 8 * 3600


async def test_skips_entity_with_unrecognized_state():
    """A device in 'unavailable' state should not be evaluated."""
    now = datetime(2026, 4, 30, 14, 0, 0, tzinfo=timezone.utc)
    history = []
    for d in range(1, 31):
        on_t = now - timedelta(days=d, hours=3)
        history.append(StateChange("light.garage", "on", on_t))
        history.append(StateChange("light.garage", "off", on_t + timedelta(minutes=30)))
    today_on = now - timedelta(hours=14)
    current = {"light.garage": ("unavailable", today_on)}

    detector = WasteDetector()
    candidates = await detector.run(history, current_states=current, now=now)
    assert candidates == []
