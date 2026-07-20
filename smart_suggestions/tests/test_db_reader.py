import aiosqlite
import pytest
from datetime import datetime, timezone
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
    assert len(changes) == 4


async def test_get_all_state_changes_filters_by_prefix(fake_db):
    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    changes = await reader.get_all_state_changes(since, entity_id_prefix="light.")
    assert len(changes) == 10  # only kitchen has data; living_room has none
    assert all(c.entity_id.startswith("light.") for c in changes)


async def test_get_all_state_changes_without_prefix_returns_all(fake_db):
    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    changes = await reader.get_all_state_changes(since)
    assert len(changes) == 10
    assert all(c.entity_id == "light.kitchen" for c in changes)
    # Confirm chronological ordering
    timestamps = [c.ts for c in changes]
    assert timestamps == sorted(timestamps)


async def test_get_all_state_changes_filters_by_domains(fake_db):
    # Add a chatty sensor entity that miners never use — it must be excluded
    # by the SQL query itself, not in Python (that's the 5 GB bug).
    async with aiosqlite.connect(fake_db) as db:
        await db.execute(
            "INSERT INTO states_meta (metadata_id, entity_id) VALUES (3, 'sensor.power_meter')"
        )
        base = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
        for i in range(50):
            await db.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (3, ?, ?)",
                (str(i), base + i * 60),
            )
        await db.commit()

    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    changes = await reader.get_all_state_changes(since, domains=["light", "person"])
    assert len(changes) == 10
    assert all(c.entity_id.startswith("light.") for c in changes)
    # No filter still returns everything
    assert len(await reader.get_all_state_changes(since)) == 60


async def test_get_all_state_changes_extra_like_patterns(fake_db):
    # Motion binary sensors matter (cross-area triggers); door/connectivity
    # binary sensors are dead weight and must not be fetched.
    async with aiosqlite.connect(fake_db) as db:
        await db.execute(
            "INSERT INTO states_meta (metadata_id, entity_id) VALUES (4, 'binary_sensor.hall_motion')"
        )
        await db.execute(
            "INSERT INTO states_meta (metadata_id, entity_id) VALUES (5, 'binary_sensor.front_door_contact')"
        )
        base = datetime(2026, 5, 2, tzinfo=timezone.utc).timestamp()
        for meta_id in (4, 5):
            await db.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (?, 'on', ?)",
                (meta_id, base),
            )
        await db.commit()

    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 4, 25, tzinfo=timezone.utc)
    changes = await reader.get_all_state_changes(
        since, domains=["light"], extra_like=["binary_sensor.%motion%"]
    )
    ids = {c.entity_id for c in changes}
    assert "binary_sensor.hall_motion" in ids
    assert "binary_sensor.front_door_contact" not in ids
    assert any(i.startswith("light.") for i in ids)


async def test_get_all_state_changes_dedup_consecutive(fake_db):
    # Attribute-only recorder rows repeat the same state (on, on, on…).
    # dedup_consecutive must collapse them to real transitions, and noise
    # states (unknown/unavailable) must be excluded before dedup so
    # on→unavailable→on collapses to a single on.
    async with aiosqlite.connect(fake_db) as db:
        await db.execute(
            "INSERT INTO states_meta (metadata_id, entity_id) VALUES (6, 'light.den')"
        )
        base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        seq = ["on", "on", "on", "unavailable", "on", "off", "off", "on"]
        for i, st in enumerate(seq):
            await db.execute(
                "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (6, ?, ?)",
                (st, base + i * 60),
            )
        await db.commit()

    reader = DbReader(sqlite_path=fake_db)
    since = datetime(2026, 5, 2, tzinfo=timezone.utc)
    changes = await reader.get_all_state_changes(
        since, domains=["light"], dedup_consecutive=True
    )
    den = [c.state for c in changes if c.entity_id == "light.den"]
    assert den == ["on", "off", "on"]


def test_db_reader_accepts_db_url():
    reader = DbReader(db_url="sqlite+aiosqlite:///tmp/test.db")
    assert reader.db_url == "sqlite+aiosqlite:///tmp/test.db"
    assert reader.sqlite_path is None


def test_db_reader_requires_one_of_path_or_url():
    with pytest.raises(ValueError, match="must provide"):
        DbReader()


def test_db_reader_rejects_both_path_and_url():
    with pytest.raises(ValueError, match="not both"):
        DbReader(sqlite_path="/tmp/x.db", db_url="sqlite+aiosqlite:///tmp/y.db")
