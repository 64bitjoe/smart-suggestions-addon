import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from anthropic_analyzer import AnthropicAnalyzer, _compact_history


def make_history(entity_id: str, states: list[str]) -> dict:
    from datetime import datetime, timezone, timedelta
    entries = []
    base = datetime.now(timezone.utc)
    for i, s in enumerate(states):
        entries.append({
            "entity_id": entity_id,
            "state": s,
            "last_changed": (base - timedelta(hours=len(states) - i)).isoformat(),
        })
    return {entity_id: entries}


def test_compact_history_excludes_unchanged_entities():
    history = {}
    history.update(make_history("light.kitchen", ["on", "on", "on"]))  # no changes
    history.update(make_history("light.living_room", ["on", "off", "on"]))  # has changes
    states = {
        "light.kitchen": {"attributes": {"friendly_name": "Kitchen Light"}},
        "light.living_room": {"attributes": {"friendly_name": "Living Room"}},
    }
    compact = _compact_history(history, states)
    assert "light.kitchen" not in compact
    assert "light.living_room" in compact


def test_compact_history_excludes_non_action_domains():
    history = make_history("sensor.temperature", ["20", "21", "22"])
    states = {"sensor.temperature": {"attributes": {"friendly_name": "Temp"}}}
    compact = _compact_history(history, states)
    assert "sensor.temperature" not in compact


@pytest.mark.asyncio
async def test_analyze_anthropic_provider_returns_patterns():
    valid_response = json.dumps({
        "routines": [
            {
                "name": "Evening Scene",
                "entity_id": "scene.evening",
                "typical_time": "18:30",
                "days": ["Mon", "Tue"],
                "confidence": 0.85,
            }
        ],
        "correlations": [],
        "anomalies": [],
    })

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=valid_response)]

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = MagicMock(return_value=mock_message)

    history = {}
    history.update(make_history("scene.evening", ["scening", "scening"]))
    states = {"scene.evening": {"attributes": {"friendly_name": "Evening"}}}

    analyzer = AnthropicAnalyzer(
        ai_provider="anthropic",
        ai_api_key="test-key",
        ai_model="claude-opus-4-5",
        analysis_depth_days=7,
    )
    analyzer._client = mock_client

    patterns = await analyzer.analyze(history, states)
    assert len(patterns["routines"]) == 1
    assert patterns["routines"][0]["entity_id"] == "scene.evening"


@pytest.mark.asyncio
async def test_analyze_returns_empty_on_no_history():
    analyzer = AnthropicAnalyzer(
        ai_provider="anthropic",
        ai_api_key="test-key",
        ai_model="claude-opus-4-5",
        analysis_depth_days=7,
    )
    patterns = await analyzer.analyze({}, {})
    assert patterns == {"routines": [], "correlations": [], "anomalies": []}


@pytest.mark.asyncio
async def test_analyze_handles_json_parse_error(caplog):
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="this is not json")]

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = MagicMock(return_value=mock_message)

    history = {}
    history.update(make_history("light.kitchen", ["on", "off", "on"]))
    states = {"light.kitchen": {"attributes": {"friendly_name": "Kitchen"}}}

    analyzer = AnthropicAnalyzer(
        ai_provider="anthropic",
        ai_api_key="test-key",
        ai_model="claude-opus-4-5",
        analysis_depth_days=7,
    )
    analyzer._client = mock_client

    patterns = await analyzer.analyze(history, states)
    assert patterns == {"routines": [], "correlations": [], "anomalies": []}
