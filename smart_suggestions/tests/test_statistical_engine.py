# smart_suggestions/tests/test_statistical_engine.py
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import MagicMock
from statistical_engine import StatisticalEngine, score_scene_match


def make_state(entity_id: str, state: str, attributes: dict | None = None) -> dict:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes or {"friendly_name": entity_id.split(".")[1]},
        "last_changed": datetime.now(timezone.utc).isoformat(),
    }


def make_routine(entity_id: str, typical_time: str, days: list, confidence: float = 0.8) -> dict:
    from datetime import timezone, timedelta
    return {
        "name": f"Test routine {entity_id}",
        "entity_id": entity_id,
        "typical_time": typical_time,
        "days": days,
        "confidence": confidence,
        "source": "anthropic",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }


def test_score_scene_match_full_match():
    states = {
        "scene.evening": {
            "entity_id": "scene.evening",
            "state": "scening",
            "attributes": {
                "friendly_name": "Evening",
                "entities": {
                    "light.living_room": {"state": "on"},
                    "light.kitchen": {"state": "off"},
                }
            },
            "last_changed": datetime.now(timezone.utc).isoformat(),
        },
        "light.living_room": make_state("light.living_room", "on"),
        "light.kitchen": make_state("light.kitchen", "off"),
    }
    ratio = score_scene_match("scene.evening", states)
    assert ratio == 1.0


def test_score_scene_match_partial():
    states = {
        "scene.evening": {
            "entity_id": "scene.evening",
            "state": "scening",
            "attributes": {
                "friendly_name": "Evening",
                "entities": {
                    "light.living_room": {"state": "on"},
                    "light.kitchen": {"state": "off"},
                }
            },
            "last_changed": datetime.now(timezone.utc).isoformat(),
        },
        "light.living_room": make_state("light.living_room", "on"),
        "light.kitchen": make_state("light.kitchen", "on"),  # wrong state
    }
    ratio = score_scene_match("scene.evening", states)
    assert ratio == 0.5


def test_score_scene_match_no_attributes():
    states = {
        "scene.empty": {
            "entity_id": "scene.empty",
            "state": "scening",
            "attributes": {},
            "last_changed": datetime.now(timezone.utc).isoformat(),
        }
    }
    ratio = score_scene_match("scene.empty", states)
    assert ratio == 0.0


def test_score_realtime_returns_scene_candidates(tmp_path):
    from pattern_store import PatternStore
    store = PatternStore(path=str(tmp_path / "patterns.json"))

    # Add a routine for scene.evening matching current day/time
    now = datetime.now()
    day_abbrev = now.strftime("%a")  # Mon, Tue, etc.
    typical_time = now.strftime("%H:%M")
    store.merge({"routines": [
        make_routine("scene.evening", typical_time, [day_abbrev])
    ], "correlations": [], "anomalies": []})

    states = {
        "scene.evening": {
            "entity_id": "scene.evening",
            "state": "scening",
            "attributes": {"friendly_name": "Evening", "entities": {}},
            "last_changed": datetime.now(timezone.utc).isoformat(),
        }
    }
    engine = StatisticalEngine(store)
    candidates = engine.score_realtime(states)
    assert any(c["entity_id"] == "scene.evening" for c in candidates)
    scene_cand = next(c for c in candidates if c["entity_id"] == "scene.evening")
    assert scene_cand["score"] > 0
    assert scene_cand["type"] == "scene"


def test_score_realtime_wrong_day_routine_not_boosted(tmp_path):
    """Routine for a different day should not boost score."""
    from pattern_store import PatternStore
    store = PatternStore(path=str(tmp_path / "patterns.json"))

    now = datetime.now()
    # Use a day that is NOT today
    all_days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    today_abbrev = now.strftime("%a")
    other_day = next(d for d in all_days if d != today_abbrev)
    typical_time = now.strftime("%H:%M")

    store.merge({"routines": [
        make_routine("scene.morning", typical_time, [other_day])
    ], "correlations": [], "anomalies": []})

    states = {
        "scene.morning": {
            "entity_id": "scene.morning",
            "state": "scening",
            "attributes": {"friendly_name": "Morning", "entities": {}},
            "last_changed": datetime.now(timezone.utc).isoformat(),
        }
    }
    engine = StatisticalEngine(store)
    candidates = engine.score_realtime(states)
    scene_cand = next((c for c in candidates if c["entity_id"] == "scene.morning"), None)
    # May appear (scene match path) but should not have high routine-based score
    if scene_cand:
        assert scene_cand.get("routine_match") is False or scene_cand["score"] < 50
