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
    """Reads HA recorder state-changed history.

    Supports two backends:
    - SQLite via aiosqlite (sqlite_path)
    - Any SQLAlchemy-async-compatible DB (db_url), e.g. MariaDB or PostgreSQL
    """

    def __init__(
        self,
        sqlite_path: str | Path | None = None,
        db_url: str | None = None,
    ):
        if sqlite_path and db_url:
            raise ValueError("DbReader accepts sqlite_path or db_url, not both")
        if not (sqlite_path or db_url):
            raise ValueError("DbReader must provide one of sqlite_path or db_url")
        self.sqlite_path = Path(sqlite_path) if sqlite_path else None
        self.db_url = db_url

    # ------------------------------------------------------------------
    # SQLAlchemy helper
    # ------------------------------------------------------------------

    async def _query_via_sqlalchemy(self, sql: str, params: dict) -> list[dict]:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text
        engine = create_async_engine(self.db_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                return [dict(row) for row in result.mappings()]
        finally:
            await engine.dispose()

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    async def get_state_changes_for_entity(
        self, entity_id: str, since: datetime
    ) -> list[StateChange]:
        since_ts = since.timestamp()

        if self.sqlite_path:
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

        # SQLAlchemy path — named placeholders
        sql = """
            SELECT s.state, s.last_updated_ts
            FROM states s
            JOIN states_meta m ON s.metadata_id = m.metadata_id
            WHERE m.entity_id = :entity_id
              AND s.last_updated_ts >= :since_ts
            ORDER BY s.last_updated_ts ASC
        """
        rows = await self._query_via_sqlalchemy(sql, {"entity_id": entity_id, "since_ts": since_ts})
        return [
            StateChange(
                entity_id=entity_id,
                state=row["state"],
                ts=datetime.fromtimestamp(row["last_updated_ts"], tz=timezone.utc),
            )
            for row in rows
        ]

    async def get_all_state_changes(
        self,
        since: datetime,
        entity_id_prefix: str | None = None,
        domains: list[str] | None = None,
        extra_like: list[str] | None = None,
    ) -> list[StateChange]:
        """Fetch state changes since `since`.

        domains: restrict to entities in these domains (e.g. ["light", "person"]).
        extra_like: additional raw LIKE patterns OR-ed into the domain filter
        (e.g. "binary_sensor.%motion%" — patterns are used as-is, not escaped).
        Filtering happens in SQL — on a busy recorder the irrelevant sensor
        firehose is the overwhelming majority of rows, and materializing it
        in Python costs gigabytes.
        """
        since_ts = since.timestamp()

        def _domain_patterns() -> list[str]:
            patterns = []
            for d in domains or []:
                escaped_d = (
                    d.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                patterns.append(f"{escaped_d}.%")
            patterns.extend(extra_like or [])
            return patterns

        if self.sqlite_path:
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
            patterns = _domain_patterns()
            if patterns:
                like_clauses = " OR ".join(
                    "m.entity_id LIKE ? ESCAPE '\\'" for _ in patterns
                )
                sql += f" AND ({like_clauses})"
                params.extend(patterns)
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

        # SQLAlchemy path — named placeholders
        sql = """
            SELECT m.entity_id, s.state, s.last_updated_ts
            FROM states s
            JOIN states_meta m ON s.metadata_id = m.metadata_id
            WHERE s.last_updated_ts >= :since_ts
        """
        named_params: dict = {"since_ts": since_ts}
        if entity_id_prefix:
            sql += " AND m.entity_id LIKE :prefix ESCAPE '\\'"
            escaped = (
                entity_id_prefix
                .replace("\\", "\\\\")  # escape literal backslashes first
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            named_params["prefix"] = f"{escaped}%"
        patterns = _domain_patterns()
        if patterns:
            like_clauses = " OR ".join(
                f"m.entity_id LIKE :domain_{i} ESCAPE '\\'" for i in range(len(patterns))
            )
            sql += f" AND ({like_clauses})"
            for i, p in enumerate(patterns):
                named_params[f"domain_{i}"] = p
        sql += " ORDER BY s.last_updated_ts ASC"

        rows = await self._query_via_sqlalchemy(sql, named_params)
        return [
            StateChange(
                entity_id=row["entity_id"],
                state=row["state"],
                ts=datetime.fromtimestamp(row["last_updated_ts"], tz=timezone.utc),
            )
            for row in rows
        ]
