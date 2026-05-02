"""Home Assistant REST client — direct REST connection."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
import aiohttp
import yarl

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_DEFAULT_BASE = "http://supervisor/core/api"

POLL_INTERVAL = 30          # seconds between state polls (keeps _states fresh)
REFRESH_INTERVAL = 600      # seconds between Ollama refresh cycles (default 10 min)


class HAClient:
    """Polls HA states via REST and fetches history."""

    def __init__(
        self,
        on_states_ready: Callable,
        refresh_interval_seconds: int = REFRESH_INTERVAL,
        ha_url: str = "",
        ha_token: str = "",
    ) -> None:
        self._on_states_ready = on_states_ready
        self._refresh_interval = refresh_interval_seconds
        # Prefer explicit ha_url/ha_token; fall back to Supervisor proxy
        if ha_url and ha_token:
            self._base = ha_url.rstrip("/") + "/api"
            self._token = ha_token
            _LOGGER.info("HAClient: using direct HA URL %s", self._base)
        else:
            self._base = _DEFAULT_BASE
            self._token = SUPERVISOR_TOKEN
            _LOGGER.info("HAClient: using Supervisor proxy")
        self._session: aiohttp.ClientSession | None = None
        self._states: dict = {}
        self._running = False
        self._last_refresh: float = 0.0

    async def start(self) -> None:
        if not self._token:
            _LOGGER.error("No auth token available — set ha_token option or check homeassistant_api permission")
        else:
            _LOGGER.info("Auth token present (%d chars)", len(self._token))

        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"}
        )
        self._running = True

        # Poll states frequently to keep _states fresh; only trigger Ollama
        # refresh every refresh_interval seconds.
        while self._running:
            await self._fetch_states()
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def _fetch_states(self) -> None:
        """Fetch all current entity states and trigger Ollama refresh if due."""
        import time
        try:
            async with self._session.get(
                f"{self._base}/states",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("GET /states returned HTTP %s", resp.status)
                    return
                states_list = await resp.json()
                self._states = {s["entity_id"]: s for s in states_list}
                _LOGGER.debug("Fetched %d entity states", len(self._states))
        except Exception as e:
            _LOGGER.error("Failed to fetch states: %s", e)
            return

        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_interval:
            self._last_refresh = now
            _LOGGER.info("Triggering refresh cycle (%d states)", len(self._states))
            await self._on_states_ready(self._states)

    # ── Possible DB paths (mapped via config:ro) ──
    _DB_PATHS = [
        "/config/home-assistant_v2.db",
        "/homeassistant/home-assistant_v2.db",
    ]

    async def fetch_history(self, hours: int) -> dict[str, list]:
        """Fetch entity history — tries SQLite DB first, falls back to REST API."""
        if not self._states:
            return {}

        from const import _ACTION_DOMAINS, _CONTEXT_ONLY_DOMAINS
        relevant_domains = _ACTION_DOMAINS | _CONTEXT_ONLY_DOMAINS
        relevant_eids = {eid for eid in self._states if eid.split(".")[0] in relevant_domains}

        # Try direct DB read first (much more reliable than REST history API)
        result = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_history_from_db, hours, relevant_eids
        )
        if result:
            _LOGGER.info("Fetched history for %d entities from SQLite DB", len(result))
            return result

        # Fallback to REST API
        _LOGGER.info("SQLite DB not available — falling back to REST history API")
        return await self._fetch_history_rest(hours, relevant_eids)

    def _fetch_history_from_db(self, hours: int, relevant_eids: set[str]) -> dict[str, list]:
        """Read history directly from HA's SQLite recorder database."""
        db_path = None
        for p in self._DB_PATHS:
            if Path(p).exists():
                db_path = p
                break
        if not db_path:
            _LOGGER.info("No HA SQLite DB found at %s", self._DB_PATHS)
            return {}

        _LOGGER.info("Reading history from %s", db_path)
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row

            # Detect schema version: new HA uses states_meta table
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            use_meta = "states_meta" in tables

            if use_meta:
                _LOGGER.info("Using new HA schema (states_meta)")
                rows = conn.execute("""
                    SELECT sm.entity_id, s.state, s.last_changed_ts, s.last_updated_ts
                    FROM states s
                    JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                    WHERE s.last_updated_ts > ?
                    ORDER BY s.last_updated_ts ASC
                """, (cutoff_ts,)).fetchall()
            else:
                _LOGGER.info("Using legacy HA schema (entity_id in states)")
                rows = conn.execute("""
                    SELECT entity_id, state, last_changed, last_updated
                    FROM states
                    WHERE last_updated > ?
                    ORDER BY last_updated ASC
                """, (datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat(),)).fetchall()

            conn.close()
        except Exception as e:
            _LOGGER.warning("Failed to read HA DB: %s", e)
            return {}

        _LOGGER.info("DB query returned %d rows", len(rows))

        # Group by entity_id into REST-API-compatible format
        result: dict[str, list] = {}
        for row in rows:
            eid = row["entity_id"] if "entity_id" in row.keys() else row[0]
            if eid not in relevant_eids:
                continue
            state = row["state"] if "state" in row.keys() else row[1]
            if state in ("unknown", "unavailable"):
                continue

            # Convert timestamps
            if use_meta:
                ts_val = row["last_changed_ts"] if "last_changed_ts" in row.keys() else row[2]
                if ts_val:
                    last_changed = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
                else:
                    last_changed = ""
            else:
                last_changed = row["last_changed"] if "last_changed" in row.keys() else row[2]
                if last_changed is None:
                    last_changed = ""

            entry = {
                "entity_id": eid,
                "state": state,
                "last_changed": last_changed,
            }
            result.setdefault(eid, []).append(entry)

        _LOGGER.info("Grouped history: %d entities with state changes", len(result))
        return result

    async def _fetch_history_rest(self, hours: int, relevant_eids: set[str]) -> dict[str, list]:
        """Fetch entity history via HA REST API, batched to avoid URL length limits."""
        if not self._session:
            return {}

        start = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_ids = list(relevant_eids)
        chunk_size = 50
        chunks = [all_ids[i:i + chunk_size] for i in range(0, len(all_ids), chunk_size)]
        _LOGGER.info("REST fetch_history: %d entities, %d chunks, start=%s", len(all_ids), len(chunks), start)

        result: dict[str, list] = {}
        for chunk_idx, chunk in enumerate(chunks):
            entity_ids_str = ",".join(chunk)
            raw_url = f"{self._base}/history/period/{start}?filter_entity_id={entity_ids_str}"
            url = yarl.URL(raw_url, encoded=True)
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for entity_history in data:
                        if entity_history:
                            eid = entity_history[0].get("entity_id")
                            if eid:
                                result[eid] = entity_history
            except Exception as e:
                _LOGGER.warning("Failed to fetch history chunk %d: %s", chunk_idx, e)

        _LOGGER.info("REST fetched history for %d entities", len(result))
        return result

    async def write_suggestions_state(self, suggestions: list) -> None:
        """Write final suggestions to HA state via REST."""
        if not self._session:
            return
        payload = {
            "state": "ready",
            "attributes": {
                "suggestions": suggestions,
                "last_updated": datetime.now().isoformat(),
                "friendly_name": "Smart Suggestions",
                "count": len(suggestions),
            },
        }
        try:
            async with self._session.post(
                f"{self._base}/states/smart_suggestions.suggestions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("Failed to write HA state: HTTP %s", resp.status)
        except Exception as e:
            _LOGGER.warning("Error writing HA state: %s", e)

    async def send_notification(self, title: str, message: str, notification_id: str = "smart_suggestions") -> None:
        """Create a HA persistent notification (shows in the bell icon)."""
        if not self._session:
            return
        try:
            async with self._session.post(
                f"{self._base}/services/persistent_notification/create",
                json={"title": title, "message": message, "notification_id": notification_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning("Failed to send notification: HTTP %s", resp.status)
        except Exception as e:
            _LOGGER.warning("Error sending notification: %s", e)

    async def _api_get(self, path: str) -> list:
        """Thin REST GET helper returning parsed JSON list."""
        if not self._session:
            raise RuntimeError("HAClient: no active session — call start() first")
        url = self._base.rstrip("/") + path
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_automations(self) -> list[str]:
        """Return list of existing automation friendly names from HA states."""
        try:
            states = await self._api_get("/states")
            return [
                s.get("attributes", {}).get("friendly_name", s["entity_id"])
                for s in states
                if s.get("entity_id", "").startswith("automation.")
            ]
        except Exception as e:
            _LOGGER.warning("get_automations failed: %s", e)
            return []

    async def get_states(self) -> list[dict]:
        """Return the raw list of all entity state dicts from /api/states."""
        return await self._api_get("/states")

    async def get_current_on_states(self) -> dict[str, tuple[str, datetime]]:
        """Return {entity_id: (state, last_changed_dt)} for entities currently in an active state.

        Active states: on, heat, cool, heat_cool, fan_only, auto.
        """
        states = await self.get_states()
        active = {"on", "heat", "cool", "heat_cool", "fan_only", "auto"}
        out: dict[str, tuple[str, datetime]] = {}
        for s in states:
            if s.get("state") in active:
                last = s.get("last_changed")
                if last:
                    out[s["entity_id"]] = (
                        s["state"],
                        datetime.fromisoformat(last.replace("Z", "+00:00")),
                    )
        return out

    async def get_automated_entities(self) -> set[str]:
        """Return set of entity_ids that appear as targets in any active automation."""
        out: set[str] = set()
        try:
            config_resp = await self._api_get("/config/automation/config")
        except Exception:
            return out  # endpoint may not be available; be tolerant
        if isinstance(config_resp, list):
            for auto in config_resp:
                for action in auto.get("action") or []:
                    target = action.get("target") or {}
                    eid = target.get("entity_id")
                    if isinstance(eid, str):
                        out.add(eid)
                    elif isinstance(eid, list):
                        out.update(eid)
        return out

    async def write_suggestions(self, suggestions: list) -> None:
        """Alias for write_suggestions_state — used by the new mining pipeline."""
        await self.write_suggestions_state(suggestions)

    async def create_automation(self, config_dict: dict) -> dict:
        """Create a new HA automation via REST. Returns {success, automation_id} or {success, error}."""
        if not self._session:
            return {"success": False, "error": "No active session"}
        try:
            async with self._session.post(
                f"{self._base}/config/automation/config",
                json={"config": config_dict},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return {"success": True, "automation_id": data.get("automation_id", "")}
                else:
                    text = await resp.text()
                    _LOGGER.warning("create_automation HTTP %s: %s", resp.status, text)
                    return {"success": False, "error": f"HTTP {resp.status}: {text}"}
        except Exception as e:
            _LOGGER.error("create_automation failed: %s", e)
            return {"success": False, "error": str(e)}
