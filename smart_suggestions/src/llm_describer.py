from __future__ import annotations
import aiosqlite
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from candidate import Candidate


DEFAULT_TTL = timedelta(days=7)
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Description:
    title: str
    description: str
    automation_yaml: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strip_json_fences(text: str) -> str:
    """Strip optional ```json ... ``` markdown fences from an LLM response."""
    s = text.strip()
    if s.startswith("```"):
        # Remove opening fence (with optional language tag)
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        # Remove trailing fence
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _build_prompt(c: Candidate, user_confirmed: bool = False) -> str:
    prefix = ""
    if user_confirmed:
        prefix = "This is a USER-CONFIRMED pattern (the user explicitly told us about it). Generate the description and YAML accordingly.\n"
    return f"""{prefix}You generate Home Assistant automation suggestions from already-validated patterns.
Pattern: {c.miner_type.value}
Entity: {c.entity_id}
Action: {c.action}
Details: {json.dumps(c.details, sort_keys=True)}
Occurrences: {c.occurrences}
Conditional probability: {c.conditional_prob:.2f}

Respond with a single JSON object with keys: title (string, ≤60 chars), description (string, one sentence), automation_yaml (string, valid HA automation YAML — alias, trigger, action). No prose, no markdown fences. Just JSON.
"""


class LlmDescriber:
    def __init__(
        self,
        client,
        cache_path: str | Path,
        model: str = DEFAULT_MODEL,
        ttl: timedelta = DEFAULT_TTL,
    ):
        self.client = client
        self.cache_path = Path(cache_path)
        self.model = model
        self.ttl = ttl
        self._initialized = False

    async def init(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.cache_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS llm_cache (
                    signature TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    automation_yaml TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            await db.commit()
        self._initialized = True

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.init()

    async def describe(self, candidate: Candidate, user_confirmed: bool = False) -> Description:
        await self._ensure_initialized()
        # Cache key includes the user_confirmed flag so confirmed and unconfirmed
        # variants of the same signature get separate cache entries (the prompt
        # differs). Both ARE cached — repeat cycles never re-bill Claude.
        sig = candidate.signature() + (":uc" if user_confirmed else "")
        cached = await self._get_cached(sig)
        if cached:
            return cached

        prompt = _build_prompt(candidate, user_confirmed=user_confirmed)
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        stripped = _strip_json_fences(text)
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"LLM returned non-JSON: {text!r}") from e

        desc = Description(
            title=str(payload["title"]),
            description=str(payload["description"]),
            automation_yaml=str(payload["automation_yaml"]),
        )
        await self._store(sig, desc)
        return desc

    async def _get_cached(self, sig: str) -> Description | None:
        cutoff = (_now() - self.ttl).timestamp()
        async with aiosqlite.connect(self.cache_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM llm_cache WHERE signature = ? AND created_at >= ?",
                (sig, cutoff),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return Description(
            title=row["title"],
            description=row["description"],
            automation_yaml=row["automation_yaml"],
        )

    async def _store(self, sig: str, desc: Description):
        async with aiosqlite.connect(self.cache_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO llm_cache
                   (signature, title, description, automation_yaml, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (sig, desc.title, desc.description, desc.automation_yaml, _now().timestamp()),
            )
            await db.commit()
