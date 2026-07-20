"""SQLite pattern ledger — single source of truth for mined patterns.

Absorbs the roles of the old DismissalStore and the LlmDescriber cache.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from candidate import Candidate
from lifecycle import (
    LIFECYCLE_AUTOMATED, LIFECYCLE_CONFIRMED, LIFECYCLE_DISMISSED,
    LIFECYCLE_EMERGING, passes_confirmed, should_resurface,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    signature TEXT PRIMARY KEY,
    miner_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    conditional_prob REAL NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'emerging',
    title TEXT,
    description TEXT,
    automation_yaml TEXT,
    described_by TEXT,
    describe_attempts INTEGER NOT NULL DEFAULT 0,
    dismiss_count INTEGER NOT NULL DEFAULT 0,
    dismissed_at REAL,
    prob_at_dismissal REAL,
    snoozed_until REAL,
    accepted_runs INTEGER NOT NULL DEFAULT 0,
    automation_id TEXT
)
"""

DISMISSAL_BUMP_WINDOW = timedelta(days=7)


class PatternLedger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_SCHEMA)
            await db.commit()

    async def upsert_evidence(
        self, c: Candidate, initial_lifecycle: str = LIFECYCLE_EMERGING
    ) -> None:
        now_ts = datetime.now(timezone.utc).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO patterns
                   (signature, miner_type, entity_id, action, details_json,
                    occurrences, conditional_prob, first_seen, last_seen, lifecycle)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(signature) DO UPDATE SET
                    occurrences=excluded.occurrences,
                    conditional_prob=excluded.conditional_prob,
                    details_json=excluded.details_json,
                    last_seen=excluded.last_seen""",
                (
                    c.signature(), c.miner_type.value, c.entity_id, c.action,
                    json.dumps(c.details, sort_keys=True),
                    c.occurrences, c.conditional_prob, now_ts, now_ts,
                    initial_lifecycle,
                ),
            )
            await db.commit()

    async def _dismissals_per_miner(self, db, now: datetime) -> dict[str, int]:
        cutoff = (now - DISMISSAL_BUMP_WINDOW).timestamp()
        cur = await db.execute(
            "SELECT miner_type, COUNT(*) AS n FROM patterns "
            "WHERE dismissed_at IS NOT NULL AND dismissed_at >= ? GROUP BY miner_type",
            (cutoff,),
        )
        return {row[0]: row[1] for row in await cur.fetchall()}

    async def run_lifecycle(self, history_days: float, now: datetime) -> list[dict]:
        newly_confirmed: list[dict] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            bumps = await self._dismissals_per_miner(db, now)
            cur = await db.execute(
                "SELECT * FROM patterns WHERE lifecycle IN (?, ?)",
                (LIFECYCLE_EMERGING, LIFECYCLE_DISMISSED),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            for row in rows:
                d7 = bumps.get(row["miner_type"], 0)
                if row["lifecycle"] == LIFECYCLE_DISMISSED:
                    days_since = (
                        (now.timestamp() - row["dismissed_at"]) / 86400.0
                        if row["dismissed_at"] else 999.0
                    )
                    if should_resurface(
                        row["conditional_prob"], row["prob_at_dismissal"], days_since
                    ):
                        await db.execute(
                            "UPDATE patterns SET lifecycle=? WHERE signature=?",
                            (LIFECYCLE_EMERGING, row["signature"]),
                        )
                        row["lifecycle"] = LIFECYCLE_EMERGING
                    else:
                        continue
                if row["lifecycle"] == LIFECYCLE_EMERGING and passes_confirmed(
                    row["occurrences"], row["conditional_prob"], history_days, d7
                ):
                    await db.execute(
                        "UPDATE patterns SET lifecycle=? WHERE signature=?",
                        (LIFECYCLE_CONFIRMED, row["signature"]),
                    )
                    row["lifecycle"] = LIFECYCLE_CONFIRMED
                    newly_confirmed.append(row)
            await db.commit()
        return newly_confirmed

    async def get_rows(self, lifecycles: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" for _ in lifecycles)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM patterns WHERE lifecycle IN ({marks}) "
                "ORDER BY conditional_prob DESC",
                lifecycles,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get(self, signature: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM patterns WHERE signature=?", (signature,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def needing_description(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM patterns WHERE lifecycle=? AND title IS NULL "
                "AND describe_attempts < 3 AND miner_type != 'waste'",
                (LIFECYCLE_CONFIRMED,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def save_description(
        self, sig: str, title: str, description: str,
        automation_yaml: str, described_by: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET title=?, description=?, automation_yaml=?, "
                "described_by=? WHERE signature=?",
                (title, description, automation_yaml, described_by, sig),
            )
            await db.commit()

    async def bump_describe_attempts(self, sig: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET describe_attempts=describe_attempts+1 "
                "WHERE signature=?", (sig,),
            )
            await db.commit()

    async def dismiss(self, sig: str, now: datetime) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET lifecycle=?, dismiss_count=dismiss_count+1, "
                "dismissed_at=?, prob_at_dismissal=conditional_prob WHERE signature=?",
                (LIFECYCLE_DISMISSED, now.timestamp(), sig),
            )
            await db.commit()

    async def snooze(self, sig: str, until_ts: float) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET snoozed_until=? WHERE signature=?",
                (until_ts, sig),
            )
            await db.commit()

    async def record_run(self, sig: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE patterns SET accepted_runs=accepted_runs+1 WHERE signature=?",
                (sig,),
            )
            await db.commit()

    async def mark_automated(self, sig: str, automation_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE patterns SET lifecycle=?, automation_id=? "
                "WHERE signature=? AND lifecycle != ?",
                (LIFECYCLE_AUTOMATED, automation_id, sig, LIFECYCLE_AUTOMATED),
            )
            await db.commit()
            return cur.rowcount > 0
