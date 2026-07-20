# smart_suggestions/tests/test_ha_client.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_create_automation_success():
    from ha_client import HAClient

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"result": "ok"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = HAClient(on_states_ready=AsyncMock())
    client._session = mock_session

    result = await client.create_automation({"alias": "Test", "trigger": []})
    assert result["success"] is True
    assert result["automation_id"].isdigit()
    mock_session.post.assert_called_once()
    call_url = str(mock_session.post.call_args[0][0])
    assert "/config/automation/config/" in call_url
    assert call_url.rsplit("/", 1)[-1] == result["automation_id"]
    call_kwargs = mock_session.post.call_args[1]
    assert call_kwargs["json"] == {"alias": "Test", "trigger": []}


@pytest.mark.asyncio
async def test_create_automation_http_error():
    from ha_client import HAClient

    mock_resp = AsyncMock()
    mock_resp.status = 400
    mock_resp.text = AsyncMock(return_value="bad request")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = HAClient(on_states_ready=AsyncMock())
    client._session = mock_session

    result = await client.create_automation({"alias": "Test"})
    assert result["success"] is False
    assert "HTTP 400" in result["error"]


@pytest.mark.asyncio
async def test_create_automation_no_session():
    from ha_client import HAClient

    client = HAClient(on_states_ready=AsyncMock())
    client._session = None

    result = await client.create_automation({"alias": "Test"})
    assert result["success"] is False


@pytest.mark.asyncio
async def test_get_automations_returns_friendly_names():
    from unittest.mock import AsyncMock, patch
    from ha_client import HAClient

    client = HAClient(
        on_states_ready=AsyncMock(),
        ha_url="http://homeassistant.local:8123",
        ha_token="test_token",
    )

    mock_states = [
        {"entity_id": "automation.goodnight", "state": "on",
         "attributes": {"friendly_name": "Goodnight routine"}},
        {"entity_id": "automation.motion_hall", "state": "off",
         "attributes": {"friendly_name": "Hallway motion light"}},
        {"entity_id": "light.kitchen", "state": "on",
         "attributes": {"friendly_name": "Kitchen Light"}},
    ]

    with patch.object(client, "_api_get", new=AsyncMock(return_value=mock_states)):
        result = await client.get_automations()

    assert "Goodnight routine" in result
    assert "Hallway motion light" in result
    assert len(result) == 2


def test_fetch_dow_history_not_present():
    """fetch_dow_history must be removed — ensure it no longer exists on HAClient."""
    from ha_client import HAClient
    assert not hasattr(HAClient, "fetch_dow_history"), (
        "fetch_dow_history should have been removed from HAClient"
    )


def test_collect_entity_ids_deep_walk_new_actions_format():
    from ha_client import _collect_entity_ids

    # Real-world shape: HA 2024.10+ "actions" list mixing a device action
    # (registry UUID entity_id — must be skipped) and a target entity.
    actions = [
        {"type": "turn_on", "device_id": "14546ff...", "entity_id": "f5b3aaa16fce", "domain": "light"},
        {"action": "light.turn_on", "data": {}, "target": {"entity_id": "light.garage_outdoor_lights"}},
        {"if": [{"condition": "state", "entity_id": "binary_sensor.dark"}],
         "then": [{"action": "homeassistant.turn_on",
                   "target": {"entity_id": ["switch.a", "switch.b"]}}]},
    ]
    out = _collect_entity_ids(actions)
    assert out == {"light.garage_outdoor_lights", "binary_sensor.dark", "switch.a", "switch.b"}
