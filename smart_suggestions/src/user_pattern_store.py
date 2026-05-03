from __future__ import annotations
import aiosqlite
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class UserPattern:
    pattern_id: int
    trigger_entity: str
    trigger_action: str
    target_entity: str
    target_action: str
    latency_seconds: int  # 0 if not specified
    label: str  # user-provided label, can be empty
    created_at: datetime


class UserPatternStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._initialized = False

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_entity TEXT NOT NULL,
                    trigger_action TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    target_action TEXT NOT NULL,
                    latency_seconds INTEGER NOT NULL DEFAULT 0,
                    label TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            await db.commit()
        self._initialized = True

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.init()

    async def add(self, trigger_entity: str, trigger_action: str, target_entity: str,
                  target_action: str, latency_seconds: int = 0, label: str = "") -> int:
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO user_patterns
                   (trigger_entity, trigger_action, target_entity, target_action, latency_seconds, label, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trigger_entity, trigger_action, target_entity, target_action, latency_seconds, label,
                 datetime.now(timezone.utc).timestamp())
            )
            await db.commit()
            return cursor.lastrowid

    async def list_all(self) -> list[UserPattern]:
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM user_patterns ORDER BY created_at DESC")
            rows = await cursor.fetchall()
        return [
            UserPattern(
                pattern_id=row["id"],
                trigger_entity=row["trigger_entity"],
                trigger_action=row["trigger_action"],
                target_entity=row["target_entity"],
                target_action=row["target_action"],
                latency_seconds=row["latency_seconds"],
                label=row["label"],
                created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
            )
            for row in rows
        ]

    async def delete(self, pattern_id: int) -> None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM user_patterns WHERE id = ?", (pattern_id,))
            await db.commit()
