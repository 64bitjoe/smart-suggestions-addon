import pytest
from datetime import datetime, timezone, timedelta
from dismissal_store import DismissalStore
from candidate import MinerType


@pytest.fixture
async def store(tmp_path):
    s = DismissalStore(db_path=tmp_path / "dismissals.db")
    await s.init()
    return s


async def test_dismissal_round_trip(store):
    sig = "temporal:light.kitchen:turn_on:abc123"
    await store.add_dismissal(sig, MinerType.TEMPORAL, datetime.now(timezone.utc))
    assert await store.is_dismissed(sig, within=timedelta(days=14))


async def test_dismissal_expires(store):
    sig = "temporal:light.kitchen:turn_on:abc123"
    old = datetime.now(timezone.utc) - timedelta(days=20)
    await store.add_dismissal(sig, MinerType.TEMPORAL, old)
    assert not await store.is_dismissed(sig, within=timedelta(days=14))


async def test_dismissals_per_miner_in_window(store):
    now = datetime.now(timezone.utc)
    for i in range(3):
        await store.add_dismissal(f"temporal:e{i}:on:x", MinerType.TEMPORAL, now - timedelta(days=i))
    await store.add_dismissal("sequence:e0:on:y", MinerType.SEQUENCE, now)
    count = await store.dismissals_per_miner_in_window(MinerType.TEMPORAL, timedelta(days=7))
    assert count == 3
    assert await store.dismissals_per_miner_in_window(MinerType.SEQUENCE, timedelta(days=7)) == 1
