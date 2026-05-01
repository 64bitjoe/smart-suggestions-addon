import aiosqlite
import pytest
from datetime import datetime, timezone, timedelta
from db_reader import DbReader, StateChange


@pytest.fixture
async def fake_db(tmp_path):
    """Build a minimal HA-recorder-shaped SQLite DB."""
    db_path = tmp_path / "home-assistant_v2.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE states_meta (
                metadata_id INTEGER PRIMARY KEY,
                entity_id TEXT NOT NULL UNIQUE
            )
        """)
        await db.execute("""
            CREATE TABLE states (
                state_id INTEGER PRIMARY KEY,
                metadata_id INTEGER NOT NULL,
                state TEXT,
                last_updated_ts REAL NOT NULL
            )
        """)
        await db.execute("INSERT INTO states_meta (metadata_id, entity_id) VALUES (1, 'light.kitchen')")
        await db.execute("INSERT INTO states_meta (metadata_id, entity_id) VALUES (2, 'light.living_room')")
        # 5 kitchen on/off pairs
        base = datetime(2026, 5, 1, 6, 45, 0, tzinfo=timezone.utc).timestamp()
        for day in range(5):
            await db.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (1, 'on', ?)",
                (base + day * 86400,),
            )
            await db.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (1, 'off', ?)",
                (base + day * 86400 + 3600,),
            )
        await db.commit()
    return db_path


async def test_get_state_changes_for_entity_returns_only_that_entity(fake_db):
    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    changes = await reader.get_state_changes_for_entity("light.kitchen", since)
    assert len(changes) == 10  # 5 on + 5 off
    assert all(c.entity_id == "light.kitchen" for c in changes)
    assert all(c.state in {"on", "off"} for c in changes)


async def test_get_state_changes_for_entity_respects_since(fake_db):
    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)  # after first 2 days
    changes = await reader.get_state_changes_for_entity("light.kitchen", since)
    assert 4 <= len(changes) <= 6  # last 2-3 days


async def test_get_all_state_changes_filters_by_prefix(fake_db):
    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    changes = await reader.get_all_state_changes(since, entity_id_prefix="light.")
    assert len(changes) == 10  # only kitchen has data; living_room has none
    assert all(c.entity_id.startswith("light.") for c in changes)
