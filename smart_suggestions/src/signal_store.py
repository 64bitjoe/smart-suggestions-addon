from __future__ import annotations
import aiosqlite
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from candidate import MinerType


class SignalType(str, Enum):
    UP = "up"
    DOWN = "down"
    DISMISS = "dismiss"


class SignalStore:
    """Records user feedback signals (up/down/dismiss) on suggestions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._initialized = False

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    signature TEXT NOT NULL,
                    miner_type TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sig_type ON signals(signature, signal_type)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_miner_type_ts ON signals(miner_type, signal_type, created_at)")
            await db.commit()
        self._initialized = True

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.init()

    async def add_signal(self, signature: str, miner_type: MinerType, signal_type: SignalType, when: datetime):
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO signals (signature, miner_type, signal_type, created_at) VALUES (?, ?, ?, ?)",
                (signature, miner_type.value, signal_type.value, when.timestamp()),
            )
            await db.commit()

    async def is_dismissed(self, signature: str, within: timedelta) -> bool:
        """True if signature has been dismissed (signal_type='dismiss') within the window."""
        await self._ensure_initialized()
        cutoff = (datetime.now(timezone.utc) - within).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM signals WHERE signature = ? AND signal_type = 'dismiss' AND created_at >= ? LIMIT 1",
                (signature, cutoff),
            )
            return await cursor.fetchone() is not None

    async def signals_per_miner_in_window(
        self, miner_type: MinerType, signal_type: SignalType, window: timedelta
    ) -> int:
        await self._ensure_initialized()
        cutoff = (datetime.now(timezone.utc) - window).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM signals WHERE miner_type = ? AND signal_type = ? AND created_at >= ?",
                (miner_type.value, signal_type.value, cutoff),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
