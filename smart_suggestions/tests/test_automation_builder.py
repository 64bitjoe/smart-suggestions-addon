import pytest
from unittest.mock import AsyncMock, MagicMock
from automation_builder import AutomationBuilder, _build_automation_prompt


def test_build_prompt_contains_scene_entity():
    ctx = {
        "entity_id": "scene.evening",
        "name": "Evening Scene",
        "typical_time": "18:30",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    }
    prompt = _build_automation_prompt(ctx)
    assert "scene.evening" in prompt
    assert "18:30" in prompt
    assert "Mon" in prompt


@pytest.mark.asyncio
async def test_build_calls_anthropic_and_ha():
    valid_yaml = """alias: Evening Scene Weekdays
trigger:
  - platform: time
    at: "18:30:00"
condition:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
action:
  - service: scene.turn_on
    target:
      entity_id: scene.evening
mode: single"""

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=valid_yaml)]
    mock_ai_client = MagicMock()
    mock_ai_client.messages = MagicMock()
    mock_ai_client.messages.create = MagicMock(return_value=mock_message)

    mock_ha = MagicMock()
    mock_ha.create_automation = AsyncMock(return_value={"success": True, "automation_id": "xyz"})

    builder = AutomationBuilder(ai_provider="anthropic", ai_api_key="test", ai_model="claude-opus-4-5")
    builder._client = mock_ai_client

    ctx = {
        "entity_id": "scene.evening",
        "name": "Evening Scene",
        "typical_time": "18:30",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    }
    result = await builder.build(ctx, mock_ha)
    assert result["success"] is True
    assert result["automation_id"] == "xyz"
    mock_ha.create_automation.assert_called_once()


@pytest.mark.asyncio
async def test_build_returns_yaml_on_ha_failure():
    valid_yaml = "alias: Test\ntrigger: []\naction: []"
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=valid_yaml)]
    mock_ai_client = MagicMock()
    mock_ai_client.messages = MagicMock()
    mock_ai_client.messages.create = MagicMock(return_value=mock_message)

    mock_ha = MagicMock()
    mock_ha.create_automation = AsyncMock(return_value={"success": False, "error": "HA error"})

    builder = AutomationBuilder(ai_provider="anthropic", ai_api_key="test", ai_model="claude-opus-4-5")
    builder._client = mock_ai_client

    ctx = {"entity_id": "scene.evening", "name": "Evening", "typical_time": "18:30", "days": ["Mon"]}
    result = await builder.build(ctx, mock_ha)
    assert result["success"] is False
    assert "yaml" in result


@pytest.mark.asyncio
async def test_build_returns_error_when_no_client():
    builder = AutomationBuilder(ai_provider="anthropic", ai_api_key="", ai_model="claude-opus-4-5")
    ctx = {"entity_id": "scene.evening", "name": "Evening", "typical_time": "18:30", "days": ["Mon"]}
    result = await builder.build(ctx, MagicMock())
    assert result["success"] is False


@pytest.mark.asyncio
async def test_build_returns_error_on_invalid_yaml():
    """When _call_api returns non-YAML content, build() should return success=False with the raw text."""
    invalid_text = "this is not yaml: : : invalid"
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=invalid_text)]
    mock_ai_client = MagicMock()
    mock_ai_client.messages = MagicMock()
    mock_ai_client.messages.create = MagicMock(return_value=mock_message)

    builder = AutomationBuilder(ai_provider="anthropic", ai_api_key="test", ai_model="claude-opus-4-5")
    builder._client = mock_ai_client

    ctx = {"entity_id": "scene.evening", "name": "Evening", "typical_time": "18:30", "days": ["Mon"]}
    result = await builder.build(ctx, MagicMock())
    assert result["success"] is False
    assert "error" in result
    assert result["yaml"] == invalid_text
