from autopilot import partition_matches, within_throttle, MAX_AUTORUNS_PER_HOUR
from action_handler import reverse_service


def test_partition_by_lifecycle():
    items = [
        {"signature": "a", "lifecycle": "autopilot"},
        {"signature": "b", "lifecycle": "confirmed"},
        {"signature": "c", "lifecycle": "autopilot"},
    ]
    execute, suggest = partition_matches(items)
    assert [r["signature"] for r in execute] == ["a", "c"]
    assert [r["signature"] for r in suggest] == ["b"]


def test_throttle():
    assert within_throttle(0)
    assert within_throttle(MAX_AUTORUNS_PER_HOUR - 1)
    assert not within_throttle(MAX_AUTORUNS_PER_HOUR)


def test_reverse_service_mapping():
    assert reverse_service("turn_on") == "turn_off"
    assert reverse_service("turn_off") == "turn_on"
    assert reverse_service("set_state_on") == "turn_off"
    assert reverse_service("set_state_off") == "turn_on"
    assert reverse_service("currently_on") == "turn_on"
    assert reverse_service("bogus") is None
