"""SQLite-backed usage log for suggestion outcomes."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import aiosqlite

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_entity_action ON outcomes (entity_id, action);
CREATE INDEX IF NOT EXISTS idx_timestamp ON outcomes (timestamp);
"""


class UsageLog:
    def __init__(self, db_path: str = "/data/usage.db") -> None:
        self._db_path = db_path

    async def start(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def stop(self) -> None:
        pass  # aiosqlite connections are context-managed per operation

    async def log(self, entity_id: str, action: str, outcome: str, confidence: float) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "INSERT INTO outcomes (timestamp, entity_id, action, outcome, confidence) VALUES (?, ?, ?, ?, ?)",
                    (ts, entity_id, action, outcome, confidence),
                )
                await db.commit()
        except Exception as e:
            _LOGGER.warning("UsageLog.log failed: %s", e)

    async def get_avoided_pairs(self, hours: int = 24, limit: int = 10) -> list[dict]:
        """Return top dismissed entity+action pairs from the last `hours` hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT entity_id, action, COUNT(*) as n
                    FROM outcomes
                    WHERE outcome = 'dismissed' AND timestamp > ?
                    GROUP BY entity_id, action
                    ORDER BY n DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                )
                rows = await cursor.fetchall()
                return [{"entity_id": r["entity_id"], "action": r["action"], "count": r["n"]} for r in rows]
        except Exception as e:
            _LOGGER.warning("UsageLog.get_avoided_pairs failed: %s", e)
            return []

    async def get_feedback_scores(self, entity_ids: list[str]) -> dict[str, dict]:
        """Return {entity_id: {"up": N, "down": N}} all-time counts.
        'run' and 'saved' outcomes count as up; 'dismissed' counts as down.
        """
        result = {eid: {"up": 0, "down": 0} for eid in entity_ids}
        if not entity_ids:
            return result
        placeholders = ",".join("?" * len(entity_ids))
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    f"""
                    SELECT entity_id, outcome, COUNT(*) as n
                    FROM outcomes
                    WHERE entity_id IN ({placeholders})
                    GROUP BY entity_id, outcome
                    """,
                    entity_ids,
                )
                rows = await cursor.fetchall()
                for row in rows:
                    eid = row["entity_id"]
                    if eid not in result:
                        continue
                    if row["outcome"] in ("run", "saved"):
                        result[eid]["up"] += row["n"]
                    elif row["outcome"] == "dismissed":
                        result[eid]["down"] += row["n"]
        except Exception as e:
            _LOGGER.warning("UsageLog.get_feedback_scores failed: %s", e)
        return result

    async def migrate_from_json(self, json_path: str) -> None:
        """Migrate feedback.json to SQLite. Renames source file to .bak on success."""
        try:
            with open(json_path) as f:
                data: dict = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            _LOGGER.warning("Could not read %s for migration: %s", json_path, e)
            return

        _MAX_ROWS = 100
        ts = datetime.now(timezone.utc).isoformat()
        rows: list[tuple] = []
        for entity_id, votes in data.items():
            up_count = min(int(votes.get("up", 0)), _MAX_ROWS)
            down_count = min(int(votes.get("down", 0)), _MAX_ROWS)
            for _ in range(up_count):
                rows.append((ts, entity_id, "", "run", 0.0))
            for _ in range(down_count):
                rows.append((ts, entity_id, "", "dismissed", 0.0))

        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.executemany(
                    "INSERT INTO outcomes (timestamp, entity_id, action, outcome, confidence) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                await db.commit()
            os.rename(json_path, json_path + ".bak")
            _LOGGER.info("Migrated %d feedback entries from %s", len(rows), json_path)
        except Exception as e:
            _LOGGER.error("UsageLog migration failed: %s", e)
