import pytest
from user_pattern_store import UserPatternStore


@pytest.fixture
async def store(tmp_path):
    s = UserPatternStore(db_path=tmp_path / "user_patterns.db")
    await s.init()
    return s


async def test_add_and_list(store):
    import asyncio
    await store.add("light.hall", "turn_on", "light.kitchen", "turn_on", 30, "Morning routine")
    # Small sleep to ensure ordering by created_at DESC is stable
    await asyncio.sleep(0.01)
    await store.add("switch.coffee", "turn_on", "light.living_room", "turn_on", 0, "Coffee time")
    patterns = await store.list_all()
    assert len(patterns) == 2
    # Most recent first
    assert patterns[0].trigger_entity == "switch.coffee"
    assert patterns[1].trigger_entity == "light.hall"


async def test_add_returns_id(store):
    pid = await store.add("light.hall", "turn_on", "light.kitchen", "turn_on")
    assert isinstance(pid, int) and pid > 0


async def test_delete(store):
    pid = await store.add("light.hall", "turn_on", "light.kitchen", "turn_off")
    patterns = await store.list_all()
    assert len(patterns) == 1
    await store.delete(pid)
    patterns = await store.list_all()
    assert len(patterns) == 0


async def test_self_init(tmp_path):
    """Store self-initializes without explicit init() call."""
    s = UserPatternStore(db_path=tmp_path / "auto_init.db")
    pid = await s.add("light.x", "turn_on", "light.y", "turn_off")
    assert pid > 0
    patterns = await s.list_all()
    assert len(patterns) == 1
