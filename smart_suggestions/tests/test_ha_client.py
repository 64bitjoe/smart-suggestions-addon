# smart_suggestions/tests/test_ha_client.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_create_automation_success():
    from ha_client import HAClient

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"automation_id": "abc123"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = HAClient(on_states_ready=AsyncMock())
    client._session = mock_session

    result = await client.create_automation({"alias": "Test", "trigger": []})
    assert result["success"] is True
    assert result["automation_id"] == "abc123"
    mock_session.post.assert_called_once()
    call_url = str(mock_session.post.call_args[0][0])
    assert "/config/automation/config" in call_url


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


def test_fetch_dow_history_not_present():
    """fetch_dow_history must be removed — ensure it no longer exists on HAClient."""
    from ha_client import HAClient
    assert not hasattr(HAClient, "fetch_dow_history"), (
        "fetch_dow_history should have been removed from HAClient"
    )
