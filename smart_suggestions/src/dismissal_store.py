from __future__ import annotations
import aiosqlite
from datetime import datetime, timedelta, timezone
from pathlib import Path
from candidate import MinerType


class DismissalStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS dismissals (
                    signature TEXT NOT NULL,
                    miner_type TEXT NOT NULL,
                    dismissed_at REAL NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sig ON dismissals(signature)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_miner_ts ON dismissals(miner_type, dismissed_at)")
            await db.commit()

    async def add_dismissal(self, signature: str, miner_type: MinerType, when: datetime):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO dismissals (signature, miner_type, dismissed_at) VALUES (?, ?, ?)",
                (signature, miner_type.value, when.timestamp()),
            )
            await db.commit()

    async def is_dismissed(self, signature: str, within: timedelta) -> bool:
        cutoff = (datetime.now(timezone.utc) - within).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM dismissals WHERE signature = ? AND dismissed_at >= ? LIMIT 1",
                (signature, cutoff),
            )
            return await cursor.fetchone() is not None

    async def dismissals_per_miner_in_window(
        self, miner_type: MinerType, window: timedelta
    ) -> int:
        cutoff = (datetime.now(timezone.utc) - window).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM dismissals WHERE miner_type = ? AND dismissed_at >= ?",
                (miner_type.value, cutoff),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
