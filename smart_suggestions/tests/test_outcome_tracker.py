import pytest
from datetime import datetime, timezone, timedelta
from outcome_tracker import OutcomeTracker


@pytest.fixture
async def tracker(tmp_path):
    t = OutcomeTracker(db_path=tmp_path / "outcomes.db")
    await t.init()
    return t


async def test_record_and_list_pending(tracker):
    now = datetime.now(timezone.utc)
    await tracker.record_pending(
        signature="temporal:light.hall:turn_on:abc",
        miner_type="temporal",
        target_entity="light.hall",
        target_action="turn_on",
        suggested_at=now,
    )
    pending = await tracker.list_pending(max_age=timedelta(hours=1))
    assert len(pending) == 1
    assert pending[0]["target_entity"] == "light.hall"
    assert pending[0]["outcome"] is None


async def test_resolve_marks_completed(tracker):
    now = datetime.now(timezone.utc)
    await tracker.record_pending(
        signature="temporal:light.hall:turn_on:abc",
        miner_type="temporal",
        target_entity="light.hall",
        target_action="turn_on",
        suggested_at=now,
    )
    pending = await tracker.list_pending(max_age=timedelta(hours=1))
    assert len(pending) == 1
    oid = pending[0]["id"]
    await tracker.resolve(oid, "acted", datetime.now(timezone.utc))
    pending_after = await tracker.list_pending(max_age=timedelta(hours=1))
    assert len(pending_after) == 0


async def test_stats_per_miner(tracker):
    """Record 5 for temporal, resolve 3 acted + 1 expired, leave 1 pending.
    stats_per_miner should return suggested=4 (resolved only), acted_on=3."""
    now = datetime.now(timezone.utc)
    for i in range(5):
        await tracker.record_pending(
            signature=f"temporal:light.x{i}:turn_on:abc{i}",
            miner_type="temporal",
            target_entity=f"light.x{i}",
            target_action="turn_on",
            suggested_at=now,
        )
    pending = await tracker.list_pending(max_age=timedelta(hours=1))
    assert len(pending) == 5

    # Resolve 3 as acted
    for entry in pending[:3]:
        await tracker.resolve(entry["id"], "acted", datetime.now(timezone.utc))
    # Resolve 1 as expired
    await tracker.resolve(pending[3]["id"], "expired", datetime.now(timezone.utc))
    # Leave pending[4] unresolved

    stats = await tracker.stats_per_miner(window=timedelta(days=1))
    assert "temporal" in stats
    stat = stats["temporal"]
    assert stat.suggested == 4   # only resolved count
    assert stat.acted_on == 3


async def test_self_init(tmp_path):
    """OutcomeTracker self-initializes."""
    t = OutcomeTracker(db_path=tmp_path / "auto.db")
    now = datetime.now(timezone.utc)
    await t.record_pending("sig", "temporal", "light.x", "turn_on", now)
    pending = await t.list_pending()
    assert len(pending) == 1
