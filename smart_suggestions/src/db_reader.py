from __future__ import annotations
import aiosqlite
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

@dataclass(frozen=True)
class StateChange:
    entity_id: str
    state: str
    ts: datetime  # UTC


class DbReader:
    """Reads HA recorder state-changed history. SQLite only in v1."""

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path)

    async def get_state_changes_for_entity(
        self, entity_id: str, since: datetime
    ) -> list[StateChange]:
        since_ts = since.timestamp()
        async with aiosqlite.connect(self.sqlite_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.state, s.last_updated_ts
                FROM states s
                JOIN states_meta m ON s.metadata_id = m.metadata_id
                WHERE m.entity_id = ?
                  AND s.last_updated_ts >= ?
                ORDER BY s.last_updated_ts ASC
                """,
                (entity_id, since_ts),
            )
            rows = await cursor.fetchall()
        return [
            StateChange(
                entity_id=entity_id,
                state=row["state"],
                ts=datetime.fromtimestamp(row["last_updated_ts"], tz=timezone.utc),
            )
            for row in rows
        ]

    async def get_all_state_changes(
        self, since: datetime, entity_id_prefix: str | None = None
    ) -> list[StateChange]:
        since_ts = since.timestamp()
        sql = """
            SELECT m.entity_id, s.state, s.last_updated_ts
            FROM states s
            JOIN states_meta m ON s.metadata_id = m.metadata_id
            WHERE s.last_updated_ts >= ?
        """
        params: list = [since_ts]
        if entity_id_prefix:
            sql += " AND m.entity_id LIKE ? ESCAPE '\\'"
            escaped = (
                entity_id_prefix
                .replace("\\", "\\\\")  # escape literal backslashes first
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params.append(f"{escaped}%")
        sql += " ORDER BY s.last_updated_ts ASC"

        async with aiosqlite.connect(self.sqlite_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [
            StateChange(
                entity_id=row["entity_id"],
                state=row["state"],
                ts=datetime.fromtimestamp(row["last_updated_ts"], tz=timezone.utc),
            )
            for row in rows
        ]
