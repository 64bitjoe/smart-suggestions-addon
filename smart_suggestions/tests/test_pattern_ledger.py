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


async def test_purge_junk_removes_foreign_domains_and_actions(ledger):
    good = _cand()
    await ledger.upsert_evidence(good)
    junk_domain = Candidate(
        miner_type=MinerType.CROSS_AREA, entity_id="sensor.cpu_util",
        action="set_state_on", details={"trigger_entity": "person.joe",
        "latency_bucket": "0-2m"}, occurrences=9, conditional_prob=0.9,
    )
    junk_action = Candidate(
        miner_type=MinerType.CROSS_AREA, entity_id="light.hall",
        action="set_state_42.5", details={"trigger_entity": "person.joe",
        "latency_bucket": "0-2m"}, occurrences=9, conditional_prob=0.9,
    )
    await ledger.upsert_evidence(junk_domain)
    await ledger.upsert_evidence(junk_action)

    removed = await ledger.purge_junk(
        allowed_domains={"light"},
        allowed_actions={"turn_on", "turn_off", "set_state_on",
                         "set_state_off", "currently_on"},
    )
    assert removed == 2
    assert await ledger.get(good.signature()) is not None
    assert await ledger.get(junk_domain.signature()) is None
    assert await ledger.get(junk_action.signature()) is None


async def test_data_version_reset_wipes_once(ledger):
    await ledger.upsert_evidence(_cand())
    wiped = await ledger.ensure_data_version(2)
    assert wiped == 1
    assert await ledger.get(_cand().signature()) is None
    # Same version again: no wipe
    await ledger.upsert_evidence(_cand())
    assert await ledger.ensure_data_version(2) == 0
    assert await ledger.get(_cand().signature()) is not None


async def test_mark_automated_is_idempotent(ledger):
    await ledger.upsert_evidence(_cand())
    await ledger.run_lifecycle(30.0, NOW)
    sig = _cand().signature()
    assert await ledger.mark_automated(sig, "auto_123") is True
    assert await ledger.mark_automated(sig, "auto_456") is False
    assert (await ledger.get(sig))["automation_id"] == "auto_123"


async def test_set_lifecycle_and_reset_runs(ledger):
    await ledger.upsert_evidence(_cand())
    sig = _cand().signature()
    await ledger.record_run(sig)
    await ledger.record_run(sig)
    await ledger.set_lifecycle(sig, "autopilot")
    assert (await ledger.get(sig))["lifecycle"] == "autopilot"
    await ledger.set_lifecycle(sig, "confirmed", reset_runs=True)
    row = await ledger.get(sig)
    assert row["lifecycle"] == "confirmed"
    assert row["accepted_runs"] == 0


async def test_activity_log_roundtrip(ledger):
    await ledger.upsert_evidence(_cand())
    sig = _cand().signature()
    await ledger.save_description(sig, "Porch at 8", "d", "", "template")
    aid = await ledger.add_activity(1000.0, sig, "light.porch", "turn_on")
    assert isinstance(aid, int)
    rows = await ledger.recent_activity(since_ts=0.0)
    assert rows[0]["act_entity"] == "light.porch"
    assert rows[0]["title"] == "Porch at 8"      # joined from patterns
    assert rows[0]["undone"] == 0
    assert await ledger.autoruns_since(0.0) == 1
    await ledger.mark_activity_undone(aid)
    assert (await ledger.get_activity(aid))["undone"] == 1


async def test_lifecycle_counts(ledger):
    await ledger.upsert_evidence(_cand())
    counts = await ledger.lifecycle_counts()
    assert counts.get("emerging") == 1
