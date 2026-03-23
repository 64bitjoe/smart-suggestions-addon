import json
import os
import pytest
import aiosqlite
from usage_log import UsageLog


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "usage.db")


@pytest.fixture
async def log(db_path):
    ul = UsageLog(db_path)
    await ul.start()
    yield ul
    await ul.stop()


@pytest.mark.asyncio
async def test_log_and_get_avoided_pairs(log):
    await log.log("light.study", "turn_off", "dismissed", 0.8)
    await log.log("light.study", "turn_off", "dismissed", 0.8)
    await log.log("light.kitchen", "turn_on", "run", 0.9)

    avoided = await log.get_avoided_pairs(hours=24, limit=10)
    assert any(p["entity_id"] == "light.study" and p["action"] == "turn_off" for p in avoided)
    assert not any(p["entity_id"] == "light.kitchen" for p in avoided)


@pytest.mark.asyncio
async def test_get_feedback_scores(log):
    await log.log("light.study", "turn_off", "run", 0.8)
    await log.log("light.study", "turn_off", "run", 0.8)
    await log.log("light.study", "turn_off", "dismissed", 0.8)

    scores = await log.get_feedback_scores(["light.study", "light.kitchen"])
    assert scores["light.study"]["up"] == 2
    assert scores["light.study"]["down"] == 1
    assert scores["light.kitchen"] == {"up": 0, "down": 0}


@pytest.mark.asyncio
async def test_migrate_from_json(db_path, tmp_path):
    json_path = str(tmp_path / "feedback.json")
    data = {
        "light.study": {"up": 3, "down": 1},
        "scene.evening": {"up": 0, "down": 2},
    }
    with open(json_path, "w") as f:
        json.dump(data, f)

    ul = UsageLog(db_path)
    await ul.start()
    await ul.migrate_from_json(json_path)

    scores = await ul.get_feedback_scores(["light.study", "scene.evening"])
    assert scores["light.study"]["up"] == 3
    assert scores["light.study"]["down"] == 1
    assert scores["scene.evening"]["down"] == 2

    # Original file renamed to .bak
    assert os.path.exists(json_path + ".bak")
    assert not os.path.exists(json_path)
    await ul.stop()


@pytest.mark.asyncio
async def test_migrate_caps_at_100_rows(db_path, tmp_path):
    json_path = str(tmp_path / "feedback.json")
    data = {"light.x": {"up": 999, "down": 0}}
    with open(json_path, "w") as f:
        json.dump(data, f)

    ul = UsageLog(db_path)
    await ul.start()
    await ul.migrate_from_json(json_path)
    scores = await ul.get_feedback_scores(["light.x"])
    assert scores["light.x"]["up"] == 100
    await ul.stop()


@pytest.mark.asyncio
async def test_avoided_pairs_only_last_24h(log):
    import aiosqlite
    from datetime import datetime, timezone, timedelta

    # Insert an old dismissed record manually
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    async with aiosqlite.connect(log._db_path) as db:
        await db.execute(
            "INSERT INTO outcomes (timestamp, entity_id, action, outcome, confidence) VALUES (?, ?, ?, ?, ?)",
            (old_ts, "light.old", "turn_off", "dismissed", 0.5)
        )
        await db.commit()

    avoided = await log.get_avoided_pairs(hours=24, limit=10)
    assert not any(p["entity_id"] == "light.old" for p in avoided)
