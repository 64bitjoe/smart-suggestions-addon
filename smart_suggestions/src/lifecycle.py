"""Pure lifecycle/threshold rules for the pattern ledger.

Lifecycle states: emerging -> confirmed -> automated | dismissed.
Thresholds adapt to how much recorder history actually exists so a
young install still confirms patterns (the v3 filter never did).
"""
from __future__ import annotations

EMERGING_MIN_OCCURRENCES = 2
EMERGING_MIN_PROB = 0.4
CONFIRM_BASE_PROB = 0.6
MAX_CONFIRM_PROB = 0.9
DISMISSAL_BUMP = 0.05
RESURFACE_PROB_DELTA = 0.15
RESURFACE_AFTER_DAYS = 30

LIFECYCLE_EMERGING = "emerging"
LIFECYCLE_CONFIRMED = "confirmed"
LIFECYCLE_AUTOMATED = "automated"
LIFECYCLE_DISMISSED = "dismissed"


def passes_emerging(occurrences: int, prob: float) -> bool:
    return occurrences >= EMERGING_MIN_OCCURRENCES and prob >= EMERGING_MIN_PROB


def confirm_gate(history_days: float, dismissals_7d: int) -> tuple[int, float]:
    """Return (min_occurrences, min_conditional_prob) for confirmation."""
    h = max(1.0, min(history_days, 30.0))
    min_occ = max(3, round(h / 6))
    min_prob = min(
        MAX_CONFIRM_PROB, CONFIRM_BASE_PROB + (dismissals_7d // 3) * DISMISSAL_BUMP
    )
    return min_occ, min_prob


def passes_confirmed(
    occurrences: int, prob: float, history_days: float, dismissals_7d: int
) -> bool:
    min_occ, min_prob = confirm_gate(history_days, dismissals_7d)
    return occurrences >= min_occ and prob >= min_prob


def should_resurface(
    prob: float, prob_at_dismissal: float | None, days_since_dismissal: float
) -> bool:
    if days_since_dismissal >= RESURFACE_AFTER_DAYS:
        return True
    return prob >= (prob_at_dismissal or 0.0) + RESURFACE_PROB_DELTA


LIFECYCLE_AUTOPILOT = "autopilot"
PROMOTE_MIN_RUNS = 3
NEVER_AUTOPILOT_DOMAINS = {"lock"}


def can_promote(row: dict, act_entity: str) -> bool:
    """Eligible for the auto-run promote chip. Promotion itself is always an
    explicit user action — this only gates the offer."""
    return (
        row.get("lifecycle") == LIFECYCLE_CONFIRMED
        and row.get("accepted_runs", 0) >= PROMOTE_MIN_RUNS
        and row.get("dismiss_count", 0) == 0
        and act_entity.split(".", 1)[0] not in NEVER_AUTOPILOT_DOMAINS
        and row.get("miner_type") != "waste"
    )
