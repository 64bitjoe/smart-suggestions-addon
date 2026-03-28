# smart_suggestions/tests/test_pattern_store.py
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
from pattern_store import PatternStore


@pytest.fixture
def tmp_store(tmp_path):
    path = str(tmp_path / "patterns.json")
    return PatternStore(path=path)


@pytest.fixture
def tmp_store_with_data(tmp_path):
    path = str(tmp_path / "patterns.json")
    data = {
        "routines": [
            {
                "name": "Evening Scene",
                "entity_id": "scene.evening",
                "typical_time": "18:30",
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                "confidence": 0.87,
                "last_seen": "2026-03-20T18:32:00",
                "source": "anthropic",
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            }
        ],
        "correlations": [],
        "anomalies": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w") as f:
        json.dump(data, f)
    return PatternStore(path=path)


def test_empty_store_returns_empty_patterns(tmp_store):
    assert tmp_store.get_routines() == []
    assert tmp_store.get_correlations() == []
    assert tmp_store.get_active_anomalies() == []


def test_seed_patterns_loaded_on_first_run(tmp_path):
    """When no patterns.json exists but a seed file does, load seed."""
    import pattern_store as ps
    old_seed = ps._SEED_PATH
    seed_path = str(tmp_path / "seed.json")
    seed_data = {
        "routines": [
            {"entity_id": "scene.morning", "typical_time": "08:00",
             "days": ["Mon"], "confidence": 0.8, "source": "seed"}
        ],
        "correlations": [
            {"entity_a": "light.a", "entity_b": "light.b",
             "confidence": 0.9, "source": "seed"}
        ],
        "anomalies": [],
    }
    with open(seed_path, "w") as f:
        json.dump(seed_data, f)
    try:
        ps._SEED_PATH = seed_path
        store = PatternStore(path=str(tmp_path / "patterns.json"))
        assert len(store.get_routines()) == 1
        assert store.get_routines()[0]["entity_id"] == "scene.morning"
        assert len(store.get_correlations()) == 1
        # Seed should have been persisted to patterns.json
        assert os.path.exists(str(tmp_path / "patterns.json"))
    finally:
        ps._SEED_PATH = old_seed


def test_save_and_load_roundtrip(tmp_store):
    patterns = {
        "routines": [
            {
                "name": "Morning",
                "entity_id": "light.kitchen",
                "typical_time": "07:00",
                "days": ["Mon"],
                "confidence": 0.8,
                "last_seen": "2026-03-20T07:01:00",
                "source": "statistical",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            }
        ],
        "correlations": [],
        "anomalies": [],
    }
    tmp_store.merge(patterns)
    store2 = PatternStore(path=tmp_store._path)
    routines = store2.get_routines()
    assert len(routines) == 1
    assert routines[0]["entity_id"] == "light.kitchen"


def test_expired_routine_is_excluded(tmp_store):
    patterns = {
        "routines": [
            {
                "name": "Stale",
                "entity_id": "scene.old",
                "typical_time": "10:00",
                "days": ["Mon"],
                "confidence": 0.9,
                "last_seen": "2026-01-01T10:00:00",
                "source": "statistical",
                "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            }
        ],
        "correlations": [],
        "anomalies": [],
    }
    tmp_store.merge(patterns)
    assert tmp_store.get_routines() == []


def test_expired_anomaly_filtered_at_read_time(tmp_store):
    patterns = {
        "routines": [],
        "correlations": [],
        "anomalies": [
            {
                "entity_id": "light.kitchen",
                "description": "on too long",
                "severity": "medium",
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            }
        ],
    }
    tmp_store.merge(patterns)
    assert tmp_store.get_active_anomalies() == []


def test_active_anomaly_returned(tmp_store):
    patterns = {
        "routines": [],
        "correlations": [],
        "anomalies": [
            {
                "entity_id": "light.kitchen",
                "description": "on too long",
                "severity": "medium",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            }
        ],
    }
    tmp_store.merge(patterns)
    anomalies = tmp_store.get_active_anomalies()
    assert len(anomalies) == 1
    assert anomalies[0]["entity_id"] == "light.kitchen"


def test_migration_adds_missing_fields(tmp_path):
    """Old-format patterns.json (no expires_at, no source) should migrate cleanly."""
    path = str(tmp_path / "patterns.json")
    old_data = {
        "routines": [
            {
                "name": "Old routine",
                "entity_id": "scene.old",
                "typical_time": "19:00",
                "days": ["Mon"],
                "confidence": 0.75,
            }
        ],
        "correlations": [],
        "right_now": [{"insight": "ignored", "entity_id": "light.x", "urgency": "high"}],
        "anomalies": [],
    }
    with open(path, "w") as f:
        json.dump(old_data, f)
    store = PatternStore(path=path)
    routines = store.get_routines()
    assert len(routines) == 1
    assert "expires_at" in routines[0]
    assert "source" in routines[0]
    assert store._data.get("right_now") is None


def test_updated_at_absent_triggers_fresh_analysis_flag(tmp_store):
    assert tmp_store.needs_fresh_analysis(analysis_depth_days=14) is True


def test_recent_updated_at_does_not_trigger(tmp_store):
    tmp_store._data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp_store._save()
    store2 = PatternStore(path=tmp_store._path)
    assert store2.needs_fresh_analysis(analysis_depth_days=14) is False


def test_merge_overwrites_existing_routine_by_entity_id(tmp_store):
    """Merging a routine with the same entity_id should replace, not duplicate."""
    first = {
        "routines": [
            {
                "name": "Morning v1",
                "entity_id": "scene.morning",
                "typical_time": "07:00",
                "days": ["Mon"],
                "confidence": 0.7,
                "last_seen": "2026-03-20T07:00:00",
                "source": "statistical",
            }
        ],
        "correlations": [],
        "anomalies": [],
    }
    second = {
        "routines": [
            {
                "name": "Morning v2",
                "entity_id": "scene.morning",
                "typical_time": "07:15",
                "days": ["Mon", "Tue"],
                "confidence": 0.9,
                "last_seen": "2026-03-21T07:15:00",
                "source": "statistical",
            }
        ],
        "correlations": [],
        "anomalies": [],
    }
    tmp_store.merge(first)
    tmp_store.merge(second)
    routines = tmp_store.get_routines()
    assert len(routines) == 1, f"Expected 1 routine, got {len(routines)}"
    assert routines[0]["name"] == "Morning v2"
    assert routines[0]["typical_time"] == "07:15"


def test_merge_replaces_anomalies_entirely(tmp_store):
    """Second merge with new anomalies should fully replace prior anomalies (not append)."""
    first_anomaly = {
        "routines": [],
        "correlations": [],
        "anomalies": [
            {
                "entity_id": "light.old",
                "description": "was on too long",
                "severity": "low",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            }
        ],
    }
    second_anomaly = {
        "routines": [],
        "correlations": [],
        "anomalies": [
            {
                "entity_id": "light.new",
                "description": "door left open",
                "severity": "high",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            }
        ],
    }
    tmp_store.merge(first_anomaly)
    tmp_store.merge(second_anomaly)
    anomalies = tmp_store.get_active_anomalies()
    assert len(anomalies) == 1
    assert anomalies[0]["entity_id"] == "light.new"
