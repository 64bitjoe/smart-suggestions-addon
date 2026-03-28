# smart_suggestions/src/pattern_store.py
"""Persistent pattern store with TTL/decay. Evaluated at read time."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

_DEFAULT_PATH = "/data/patterns.json"
_SEED_PATH = "/opt/smart_suggestions/seed_patterns.json"
_TTL_STATISTICAL_HOURS = 24
_TTL_ANTHROPIC_DAYS = 7
_TTL_ANOMALY_HOURS = 4


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_expired(entry: dict) -> bool:
    raw = entry.get("expires_at", "")
    if not raw:
        return False  # No expiry set — treat as live
    expires_at = _parse_dt(raw)
    if expires_at is None:
        _LOGGER.warning("PatternStore: unparseable expires_at %r — treating as expired", raw)
        return True
    return _now_utc() > expires_at


def _add_defaults(entry: dict, default_ttl_hours: int, default_source: str) -> dict:
    """Add missing expires_at and source fields (migration path)."""
    out = dict(entry)
    if "source" not in out:
        out["source"] = default_source
    if "expires_at" not in out:
        out["expires_at"] = (_now_utc() + timedelta(hours=default_ttl_hours)).isoformat()
    return out


class PatternStore:
    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._data: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return self._load_seed()
            return self._migrate(raw)
        except FileNotFoundError:
            return self._load_seed()
        except Exception as e:
            _LOGGER.warning("PatternStore: could not read %s: %s — trying seed", self._path, e)
            return self._load_seed()

    def _load_seed(self) -> dict:
        """Load seed patterns for first-run bootstrap."""
        try:
            with open(_SEED_PATH) as f:
                raw = json.load(f)
            if isinstance(raw, dict) and any(raw.get(k) for k in ("routines", "correlations", "anomalies")):
                _LOGGER.info("PatternStore: loaded seed patterns (%d routines, %d correlations, %d anomalies)",
                             len(raw.get("routines", [])), len(raw.get("correlations", [])), len(raw.get("anomalies", [])))
                migrated = self._migrate(raw)
                self._data = migrated
                self._save()
                return migrated
        except FileNotFoundError:
            pass
        except Exception as e:
            _LOGGER.warning("PatternStore: could not read seed %s: %s", _SEED_PATH, e)
        return self._empty()

    def _empty(self) -> dict:
        return {"routines": [], "correlations": [], "anomalies": []}

    def _migrate(self, data: dict) -> dict:
        """Normalise old-format files: add missing fields, drop right_now."""
        data.pop("right_now", None)
        data.setdefault("routines", [])
        data.setdefault("correlations", [])
        data.setdefault("anomalies", [])
        data["routines"] = [
            _add_defaults(r, _TTL_STATISTICAL_HOURS, "statistical")
            for r in data["routines"]
            if isinstance(r, dict)
        ]
        data["correlations"] = [
            _add_defaults(c, _TTL_STATISTICAL_HOURS, "statistical")
            for c in data["correlations"]
            if isinstance(c, dict)
        ]
        data["anomalies"] = [
            _add_defaults(a, _TTL_ANOMALY_HOURS, "statistical")
            for a in data["anomalies"]
            if isinstance(a, dict)
        ]
        return data

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            _LOGGER.error("PatternStore: could not save %s: %s", self._path, e)

    def get_routines(self) -> list[dict]:
        return [r for r in self._data.get("routines", []) if not _is_expired(r)]

    def get_correlations(self) -> list[dict]:
        return [c for c in self._data.get("correlations", []) if not _is_expired(c)]

    def get_active_anomalies(self) -> list[dict]:
        return [a for a in self._data.get("anomalies", []) if not _is_expired(a)]

    def merge(self, patterns: dict) -> None:
        """Merge new patterns into the store. Overwrites by entity_id for routines/correlations."""
        now = _now_utc()
        if "routines" in patterns:
            existing = {r["entity_id"]: r for r in self._data.get("routines", []) if "entity_id" in r}
            for r in patterns["routines"]:
                if not isinstance(r, dict):
                    continue
                r = dict(r)
                if "source" not in r:
                    r["source"] = "statistical"
                if "expires_at" not in r:
                    ttl_hours = _TTL_ANTHROPIC_DAYS * 24 if r.get("source") == "anthropic" else _TTL_STATISTICAL_HOURS
                    r["expires_at"] = (now + timedelta(hours=ttl_hours)).isoformat()
                eid = r.get("entity_id")
                if not eid:
                    _LOGGER.warning("PatternStore: skipping routine with missing entity_id")
                    continue
                existing[eid] = r
            self._data["routines"] = list(existing.values())
        if "correlations" in patterns:
            existing = {(c.get("entity_a"), c.get("entity_b")): c for c in self._data.get("correlations", []) if c.get("entity_a") and c.get("entity_b")}
            for c in patterns["correlations"]:
                if not isinstance(c, dict):
                    continue
                c = dict(c)
                if "source" not in c:
                    c["source"] = "statistical"
                ea = c.get("entity_a")
                eb = c.get("entity_b")
                if not ea or not eb:
                    _LOGGER.warning("PatternStore: skipping correlation with missing entity_a/entity_b")
                    continue
                ttl_hours = _TTL_ANTHROPIC_DAYS * 24 if c.get("source") == "anthropic" else _TTL_STATISTICAL_HOURS
                c["expires_at"] = (now + timedelta(hours=ttl_hours)).isoformat()
                existing[(ea, eb)] = c
            self._data["correlations"] = list(existing.values())
        if "anomalies" in patterns:
            new_anomalies = []
            for a in patterns["anomalies"]:
                if not isinstance(a, dict):
                    continue
                a = dict(a)
                if "source" not in a:
                    a["source"] = "statistical"
                if "expires_at" not in a or _parse_dt(a["expires_at"]) is None:
                    a["expires_at"] = (now + timedelta(hours=_TTL_ANOMALY_HOURS)).isoformat()
                new_anomalies.append(a)
            self._data["anomalies"] = new_anomalies
        self._data["updated_at"] = now.isoformat()
        self._save()

    def needs_fresh_analysis(self, analysis_depth_days: int = 14) -> bool:
        """Return True if no updated_at or it's older than analysis_depth_days."""
        updated_at = _parse_dt(self._data.get("updated_at", ""))
        if updated_at is None:
            return True
        return (_now_utc() - updated_at).total_seconds() > analysis_depth_days * 86400
