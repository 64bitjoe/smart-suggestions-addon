# smart_suggestions/tests/test_context_builder.py
"""Tests for SmartSuggestionsAddon._build_narrator_context()."""
import asyncio
import types
import unittest.mock as mock
import pytest

# ---------------------------------------------------------------------------
# Helper: build a minimal fake addon instance that has just enough attributes
# for _build_narrator_context to work, without touching real I/O.
# ---------------------------------------------------------------------------

def _make_addon():
    """Return a bare object wired with _build_narrator_context and mock deps."""
    import sys
    import os
    # Ensure src is on path (conftest already does this, but be explicit)
    src_path = os.path.join(os.path.dirname(__file__), "..", "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    # Import the unbound method from main
    import main as main_module

    addon = types.SimpleNamespace()
    addon._ha = None  # default: no HA client

    # Mock usage_log with get_avoided_pairs returning []
    usage_log_mock = mock.AsyncMock()
    usage_log_mock.get_avoided_pairs = mock.AsyncMock(return_value=[])
    addon._usage_log = usage_log_mock

    # Bind the method from the class onto our fake instance
    addon._build_narrator_context = (
        main_module.SmartSuggestionsAddon._build_narrator_context.__get__(addon)
    )
    return addon


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_narrator_context_presence():
    """Only persons with state 'home' appear in presence list."""
    addon = _make_addon()
    states = {
        "person.alice": {"state": "home", "last_changed": ""},
        "person.bob": {"state": "away", "last_changed": ""},
    }
    ctx = await addon._build_narrator_context(states)
    assert ctx["presence"] == ["person.alice"]


@pytest.mark.asyncio
async def test_build_narrator_context_weather_entity():
    """Weather entity state becomes condition; attributes.temperature used when no sensor."""
    addon = _make_addon()
    states = {
        "weather.home": {
            "state": "sunny",
            "attributes": {"temperature": 22},
            "last_changed": "",
        }
    }
    ctx = await addon._build_narrator_context(states)
    assert ctx["weather"] == {"condition": "sunny", "temperature": 22}


@pytest.mark.asyncio
async def test_build_narrator_context_outdoor_temp_sensor():
    """sensor.outdoor_temperature takes precedence over weather entity temperature."""
    addon = _make_addon()
    states = {
        "sensor.outdoor_temperature": {"state": "18.5", "last_changed": ""},
        "weather.home": {
            "state": "cloudy",
            "attributes": {"temperature": 20},
            "last_changed": "",
        },
    }
    ctx = await addon._build_narrator_context(states)
    assert ctx["weather"]["temperature"] == "18.5"
    assert ctx["weather"]["condition"] == "cloudy"
