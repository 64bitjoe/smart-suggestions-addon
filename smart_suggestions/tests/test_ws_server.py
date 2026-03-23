# smart_suggestions/tests/test_ws_server.py
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from ws_server import WSServer


def test_broadcast_token_removed():
    """broadcast_token must be removed — streaming is gone."""
    server = WSServer()
    assert not hasattr(server, "broadcast_token"), (
        "broadcast_token should have been removed from WSServer"
    )


def test_register_automation_handler():
    server = WSServer()
    handler = AsyncMock()
    server.register_automation_handler(handler)
    assert server._automation_handler is handler


@pytest.mark.asyncio
async def test_broadcast_automation_result_queues_message():
    server = WSServer()
    # No connected clients — just verify it doesn't raise
    await server.broadcast_automation_result({"success": True, "automation_id": "abc"})


@pytest.mark.asyncio
async def test_save_automation_message_calls_handler():
    server = WSServer()
    handler = AsyncMock()
    server.register_automation_handler(handler)

    suggestion = {"entity_id": "scene.evening", "can_save_as_automation": True}
    await server._handle_save_automation(suggestion)
    handler.assert_called_once_with(suggestion)


@pytest.mark.asyncio
async def test_save_automation_without_handler_does_not_raise():
    server = WSServer()
    await server._handle_save_automation({"entity_id": "scene.evening"})


@pytest.mark.asyncio
async def test_outcome_handler_calls_usage_log():
    from unittest.mock import AsyncMock, MagicMock
    from ws_server import WSServer

    server = WSServer()
    mock_usage_log = AsyncMock()
    server.set_usage_log(mock_usage_log)

    msg = {"type": "outcome", "entity_id": "light.study", "action": "turn_off",
           "outcome": "dismissed", "confidence": 0.8}
    await server._handle_client_message(msg)

    mock_usage_log.log.assert_called_once_with("light.study", "turn_off", "dismissed", 0.8)


@pytest.mark.asyncio
async def test_build_yaml_handler_calls_automation_builder():
    import json
    from unittest.mock import AsyncMock, MagicMock
    from ws_server import WSServer

    server = WSServer()
    mock_ws = AsyncMock()
    mock_builder = MagicMock()
    mock_builder.build = AsyncMock(return_value={"yaml": "alias: Test\n..."})
    server.set_automation_builder(mock_builder)
    server.set_ha_client(AsyncMock())

    msg = {"type": "build_yaml", "entity_id": "light.study", "action": "turn_off",
           "name": "Study Light", "reason": "No motion for 45 min"}
    await server._handle_client_message(msg, ws=mock_ws)

    mock_builder.build.assert_called_once()
    mock_ws.send_str.assert_called_once()
    sent = json.loads(mock_ws.send_str.call_args[0][0])
    assert sent["type"] == "yaml_result"
    assert sent["entity_id"] == "light.study"
