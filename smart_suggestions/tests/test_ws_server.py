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
