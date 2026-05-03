import pytest
from datetime import datetime, timezone, timedelta
from signal_store import SignalStore, SignalType
from candidate import MinerType


@pytest.fixture
async def store(tmp_path):
    s = SignalStore(db_path=tmp_path / "signals.db")
    await s.init()
    return s


async def test_add_and_query_dismiss(store):
    sig = "temporal:light.kitchen:turn_on:abc123"
    await store.add_signal(sig, MinerType.TEMPORAL, SignalType.DISMISS, datetime.now(timezone.utc))
    assert await store.is_dismissed(sig, within=timedelta(days=14))


async def test_dismissal_expires_outside_window(store):
    sig = "temporal:light.kitchen:turn_on:abc123"
    old = datetime.now(timezone.utc) - timedelta(days=20)
    await store.add_signal(sig, MinerType.TEMPORAL, SignalType.DISMISS, old)
    assert not await store.is_dismissed(sig, within=timedelta(days=14))


async def test_signals_per_miner_counts_each_type_separately(store):
    now = datetime.now(timezone.utc)
    # 3 ups for TEMPORAL
    for i in range(3):
        await store.add_signal(f"temporal:e{i}:on:x", MinerType.TEMPORAL, SignalType.UP, now - timedelta(days=i))
    # 2 downs for TEMPORAL
    for i in range(2):
        await store.add_signal(f"temporal:d{i}:on:x", MinerType.TEMPORAL, SignalType.DOWN, now - timedelta(days=i))
    # 1 dismiss for TEMPORAL
    await store.add_signal("temporal:dm:on:x", MinerType.TEMPORAL, SignalType.DISMISS, now)

    assert await store.signals_per_miner_in_window(MinerType.TEMPORAL, SignalType.UP, timedelta(days=7)) == 3
    assert await store.signals_per_miner_in_window(MinerType.TEMPORAL, SignalType.DOWN, timedelta(days=7)) == 2
    assert await store.signals_per_miner_in_window(MinerType.TEMPORAL, SignalType.DISMISS, timedelta(days=7)) == 1
    # Counts for other types should be 0
    assert await store.signals_per_miner_in_window(MinerType.SEQUENCE, SignalType.UP, timedelta(days=7)) == 0


async def test_methods_self_init(tmp_path):
    """Caller forgot to call init() — store should self-initialize on first use."""
    s = SignalStore(db_path=tmp_path / "auto_init.db")
    # Note: NO await s.init()
    sig = "temporal:auto.init:turn_on:xyz"
    await s.add_signal(sig, MinerType.TEMPORAL, SignalType.UP, datetime.now(timezone.utc))
    assert await s.is_dismissed(sig, within=timedelta(days=1)) is False
    count = await s.signals_per_miner_in_window(MinerType.TEMPORAL, SignalType.UP, timedelta(days=1))
    assert count == 1
