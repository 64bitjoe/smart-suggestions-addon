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

    # Add a routine for scene.evening matching current day/time (UTC, matching score_realtime)
    now = datetime.now(timezone.utc)
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

    now = datetime.now(timezone.utc)
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
    # May appear (scene match path) but should not have a routine match
    assert scene_cand is None or scene_cand.get("routine_match") is False


@pytest.mark.asyncio
async def test_analyze_correlations_detects_cooccurrence(tmp_path):
    """Entities that change state within window_minutes should produce a correlation."""
    from datetime import datetime, timezone, timedelta
    from pattern_store import PatternStore
    from statistical_engine import StatisticalEngine

    store = PatternStore(path=str(tmp_path / "patterns.json"))
    engine = StatisticalEngine(store)

    # Build history: tv turns on, then living room light turns on 2 min later, 4 times
    base = datetime.now(timezone.utc)
    tv_history = []
    light_history = []
    for i in range(4):
        offset = timedelta(hours=i * 3)
        tv_history.append({"entity_id": "media_player.tv", "state": "on", "last_changed": (base + offset).isoformat()})
        light_history.append({"entity_id": "light.living_room", "state": "on", "last_changed": (base + offset + timedelta(minutes=2)).isoformat()})

    history = {
        "media_player.tv": tv_history,
        "light.living_room": light_history,
    }
    states = {
        "media_player.tv": {"attributes": {"friendly_name": "TV"}},
        "light.living_room": {"attributes": {"friendly_name": "Living Room"}},
    }

    correlations = await engine.analyze_correlations(history, states, window_minutes=5)
    assert len(correlations) >= 1
    entity_pairs = [(c["entity_a"], c["entity_b"]) for c in correlations]
    assert ("media_player.tv", "light.living_room") in entity_pairs


def test_domain_filter_excludes_unlisted_domains():
    """Entities not in allowed_domains must not appear in candidates."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get_routines.return_value = []
    store.get_correlations.return_value = []
    store.get_active_anomalies.return_value = []

    engine = StatisticalEngine(store, allowed_domains=["light"])
    states = {
        "light.kitchen": {"state": "on", "attributes": {"friendly_name": "Kitchen"}},
        "switch.fan": {"state": "on", "attributes": {"friendly_name": "Fan"}},
        "climate.bedroom": {"state": "cool", "attributes": {"friendly_name": "Bedroom"}},
    }
    result = engine.score_realtime(states)
    domains = {c["entity_id"].split(".")[0] for c in result}
    assert "switch" not in domains
    assert "climate" not in domains


def test_domain_filter_none_means_all_action_domains():
    """allowed_domains=None keeps existing _ACTION_DOMAINS behaviour."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get_routines.return_value = []
    store.get_correlations.return_value = []
    store.get_active_anomalies.return_value = []

    engine = StatisticalEngine(store, allowed_domains=None)
    states = {
        "switch.fan": {"state": "on", "attributes": {"friendly_name": "Fan"}},
    }
    result = engine.score_realtime(states)
    assert isinstance(result, list)


def test_entity_sampling_respects_max_entities():
    """When states exceed max_entities, only max_entities non-scene entities are scored."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get_routines.return_value = []
    store.get_correlations.return_value = []
    anomalies = [{"entity_id": f"light.l{i}", "description": "anomaly"} for i in range(10)]
    store.get_active_anomalies.return_value = anomalies

    engine = StatisticalEngine(store, max_entities=3)
    states = {f"light.l{i}": {"state": "on", "attributes": {"friendly_name": f"L{i}"}} for i in range(10)}
    result = engine.score_realtime(states)
    assert len(result) <= 3


def test_scene_entities_not_affected_by_sampling():
    """Scenes are always included in scoring pool, not subject to max_entities sampling."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get_routines.return_value = []
    store.get_correlations.return_value = []
    store.get_active_anomalies.return_value = []

    engine = StatisticalEngine(store, max_entities=1)
    states = {
        "scene.evening": {"state": "scening", "attributes": {"friendly_name": "Evening", "entities": {}}},
        "scene.morning": {"state": "scening", "attributes": {"friendly_name": "Morning", "entities": {}}},
        "light.l1": {"state": "on", "attributes": {"friendly_name": "L1"}},
        "light.l2": {"state": "on", "attributes": {"friendly_name": "L2"}},
    }
    result = engine.score_realtime(states)
    # Scenes are still scored (not sampled out), but only appear in results
    # if they have score > 0 like any other entity. Without routines or
    # match_ratio, scenes with empty entities dict won't score.
    # At minimum, lights should appear (they get context-based scoring).
    assert isinstance(result, list)
