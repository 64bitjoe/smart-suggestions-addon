from __future__ import annotations
import aiosqlite
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class OutcomeStat:
    miner_type: str
    suggested: int  # total resolved suggestions (acted + expired) in window
    acted_on: int   # follow-through count


class OutcomeTracker:
    """Tracks whether the user actually performed the suggested action within 30 min."""

    def __init__(self, db_path: str | Path, action_window: timedelta = timedelta(minutes=30)):
        self.db_path = Path(db_path)
        self.action_window = action_window
        self._initialized = False

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signature TEXT NOT NULL,
                    miner_type TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    target_action TEXT NOT NULL,
                    suggested_at REAL NOT NULL,
                    resolved_at REAL,
                    outcome TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome_pending ON outcomes(resolved_at) WHERE resolved_at IS NULL"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome_miner ON outcomes(miner_type, resolved_at)"
            )
            await db.commit()
        self._initialized = True

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.init()

    async def record_pending(
        self,
        signature: str,
        miner_type: str,
        target_entity: str,
        target_action: str,
        suggested_at: datetime,
    ) -> None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO outcomes (signature, miner_type, target_entity, target_action, suggested_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (signature, miner_type, target_entity, target_action, suggested_at.timestamp()),
            )
            await db.commit()

    async def list_pending(self, max_age: timedelta = timedelta(hours=1)) -> list[dict]:
        """Return suggestions still pending and within max_age."""
        await self._ensure_initialized()
        cutoff = (datetime.now(timezone.utc) - max_age).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM outcomes WHERE resolved_at IS NULL AND suggested_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def resolve(self, outcome_id: int, outcome: str, when: datetime) -> None:
        """outcome: 'acted' | 'expired'"""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE outcomes SET resolved_at = ?, outcome = ? WHERE id = ?",
                (when.timestamp(), outcome, outcome_id),
            )
            await db.commit()

    async def stats_per_miner(self, window: timedelta) -> dict[str, OutcomeStat]:
        """Return suggested vs acted counts per miner type within window (resolved only)."""
        await self._ensure_initialized()
        cutoff = (datetime.now(timezone.utc) - window).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT miner_type,
                          COUNT(*) AS suggested,
                          SUM(CASE WHEN outcome = 'acted' THEN 1 ELSE 0 END) AS acted
                   FROM outcomes
                   WHERE suggested_at >= ? AND resolved_at IS NOT NULL
                   GROUP BY miner_type""",
                (cutoff,),
            )
            rows = await cursor.fetchall()
        out: dict[str, OutcomeStat] = {}
        for row in rows:
            out[row[0]] = OutcomeStat(
                miner_type=row[0],
                suggested=row[1] or 0,
                acted_on=row[2] or 0,
            )
        return out
