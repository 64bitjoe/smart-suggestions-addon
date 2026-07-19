"""Turn a confirmed ledger row into user-facing title + description.

LLM output is polish only — template_description() always works and is the
fallback (and the only path for waste alerts).
"""
from __future__ import annotations
import json
import logging
import re

from model_router import ModelRouter, RouterError

_LOGGER = logging.getLogger(__name__)

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _friendly(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def _weekdays_text(weekdays: list[int]) -> str:
    if sorted(weekdays) == [0, 1, 2, 3, 4]:
        return "weekdays"
    if sorted(weekdays) == [5, 6]:
        return "weekends"
    if len(weekdays) >= 7:
        return "every day"
    return "/".join(_WEEKDAY_NAMES[d] for d in sorted(weekdays))


def template_description(row: dict) -> tuple[str, str]:
    d = json.loads(row["details_json"])
    name = _friendly(row["entity_id"])
    verb = "Turn on" if "on" in row["action"] else "Turn off"
    pct = f"{row['conditional_prob']:.0%}"
    mt = row["miner_type"]
    if mt == "temporal":
        t = f"{d['hour']:02d}:{d['minute']:02d}"
        title = f"{verb} {name} at {t}"
        desc = (f"You do this around {t} on {_weekdays_text(d['weekdays'])} — "
                f"{pct} of the time ({row['occurrences']} times).")
    elif mt == "sequence":
        tgt = _friendly(d["target_entity"])
        title = f"{tgt} follows {name}"
        desc = (f"When {name} turns on, {tgt} usually turns on within "
                f"{d['delta_seconds']}s — {pct} of the time.")
    elif mt == "cross_area":
        trig = _friendly(d["trigger_entity"])
        title = f"{verb} {name} when {trig} arrives"
        desc = (f"After {trig} shows up, {name} usually changes within "
                f"{d['latency_bucket']} — {pct} of the time.")
    else:  # waste
        hours = d["duration_seconds"] / 3600.0
        base = d["baseline_seconds"] / 3600.0
        title = f"{name} left on?"
        desc = (f"{name} has been on for {hours:.1f} h — "
                f"it usually runs about {base:.1f} h.")
    return title, desc


def _build_prompt(row: dict) -> str:
    return f"""You write one Home Assistant suggestion for a statistically-validated pattern.
Pattern type: {row['miner_type']}
Entity: {row['entity_id']}
Action: {row['action']}
Details: {row['details_json']}
Occurrences: {row['occurrences']}, conditional probability: {row['conditional_prob']:.2f}

Respond with ONLY a JSON object: {{"title": "≤60 chars, friendly", "description": "one warm, concrete sentence"}}"""


def _strip_json_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


class Describer:
    def __init__(self, router: ModelRouter):
        self._router = router

    async def describe(self, row: dict) -> tuple[str, str, str]:
        """Return (title, description, described_by). Never raises."""
        try:
            text = await self._router.complete(_build_prompt(row), max_tokens=300)
            payload = json.loads(_strip_json_fences(text))
            return str(payload["title"]), str(payload["description"]), "llm"
        except (RouterError, KeyError, ValueError, TypeError) as e:
            _LOGGER.warning("describe fell back to template: %s", e)
            title, desc = template_description(row)
            return title, desc, "template"
