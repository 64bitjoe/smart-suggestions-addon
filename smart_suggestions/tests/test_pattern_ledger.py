import pytest
from datetime import datetime, timedelta, timezone
from candidate import Candidate, MinerType
from pattern_ledger import PatternLedger

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _cand(occ=6, prob=0.8):
    return Candidate(
        miner_type=MinerType.TEMPORAL,
        entity_id="light.porch",
        action="turn_on",
        details={"hour": 20, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
        occurrences=occ,
        conditional_prob=prob,
    )


@pytest.fixture
async def ledger(tmp_path):
    led = PatternLedger(tmp_path / "patterns.db")
    await led.init()
    return led


async def test_upsert_then_confirm(ledger):
    await ledger.upsert_evidence(_cand())
    newly = await ledger.run_lifecycle(history_days=30.0, now=NOW)
    assert [r["signature"] for r in newly] == [_cand().signature()]
    rows = await ledger.get_rows(("confirmed",))
    assert rows[0]["entity_id"] == "light.porch"
    # second run: no re-notification
    assert await ledger.run_lifecycle(30.0, NOW) == []


async def test_upsert_updates_evidence_not_lifecycle(ledger):
    await ledger.upsert_evidence(_cand())
    await ledger.run_lifecycle(30.0, NOW)
    await ledger.upsert_evidence(_cand(occ=7, prob=0.85))
    row = await ledger.get(_cand().signature())
    assert row["occurrences"] == 7
    assert row["lifecycle"] == "confirmed"


async def test_dismiss_blocks_and_resurfaces(ledger):
    await ledger.upsert_evidence(_cand(prob=0.7))
    await ledger.run_lifecycle(30.0, NOW)
    sig = _cand().signature()
    await ledger.dismiss(sig, NOW)
    assert (await ledger.get(sig))["lifecycle"] == "dismissed"
    # stronger evidence within window → resurfaces to emerging
    await ledger.upsert_evidence(_cand(prob=0.9))
    await ledger.run_lifecycle(30.0, NOW + timedelta(days=1))
    assert (await ledger.get(sig))["lifecycle"] in ("emerging", "confirmed")


async def test_dismissals_bump_threshold(ledger):
    # 3 dismissed temporal patterns in window → bar rises to 0.65; a 0.62 stays emerging
    for i in range(3):
        c = _cand()
        c.entity_id = f"light.l{i}"
        await ledger.upsert_evidence(c)
        await ledger.run_lifecycle(30.0, NOW)
        await ledger.dismiss(c.signature(), NOW)
    borderline = _cand(prob=0.62)
    borderline.entity_id = "light.borderline"
    await ledger.upsert_evidence(borderline)
    newly = await ledger.run_lifecycle(30.0, NOW)
    assert newly == []
    assert (await ledger.get(borderline.signature()))["lifecycle"] == "emerging"


async def test_mark_automated_is_idempotent(ledger):
    await ledger.upsert_evidence(_cand())
    await ledger.run_lifecycle(30.0, NOW)
    sig = _cand().signature()
    assert await ledger.mark_automated(sig, "auto_123") is True
    assert await ledger.mark_automated(sig, "auto_456") is False
    assert (await ledger.get(sig))["automation_id"] == "auto_123"
