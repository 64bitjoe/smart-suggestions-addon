# Smart Suggestions v4 — Phase 1: Pattern Ledger + Context Matcher

**Date:** 2026-07-18
**Status:** Approved design, pending implementation plan
**Branch:** feature/pattern-mining-rewrite → v4.0.0

## Problem

The v3 add-on burned AI tokens without delivering user value:

1. The legacy realtime path called the Anthropic narrator (~2,048 max output
   tokens, large context prompt, no caching) on every state-refresh cycle —
   roughly every 30 seconds, ~2,880 calls/day — just to reword heuristic
   suggestion "reasons".
2. The new mining pipeline never surfaced anything: the four-criteria filter
   (≥5 occurrences AND ≥0.7 conditional probability) passes zero candidates on
   young installs, its output schema doesn't match the UI card, and it is never
   broadcast over WebSocket. Several config knobs (`mining_interval_hours`,
   `min_pattern_occurrences`, …) were never wired up.

## Goals (Phase 1: foundation + visible value)

- Delete the legacy LLM treadmill entirely.
- Make mined patterns actually reach the user, from day one of an install.
- One-tap contextual actions ("you usually do this now — tap to do it").
- One-tap accept → create a real HA automation or scene.
- Waste/anomaly watchdog ("noticed" zone).
- LLM usage: event-driven and rare (describe a pattern once when confirmed;
  build YAML on accept). Pluggable second local backend (Ollama/MLX).
- Surfaces: a Lovelace dashboard card (day-to-day) + the ingress panel (hub).

Deferred to later phases: graduated-trust auto-pilot (designed for, not built),
daily/weekly digest, energy/cost insights, presence-aware scenes, helper
creation, native HA notifications.

## Architecture

Three loops share one source of truth, the **pattern ledger**:

1. **Hourly learning loop** — DbReader reads the recorder SQLite → the three
   existing pattern miners (Temporal, Sequence, CrossArea) → upsert evidence
   into the ledger → LifecycleEngine applies transitions.
   LLM is called only when a pattern first becomes `confirmed` (result stored
   in the ledger, never re-generated).
2. **60-second relevance loop** — ContextMatcher compares `confirmed` patterns
   against current context (time, weekday, presence, recent events) → top
   matches published as one-tap actions. Pure Python, zero tokens.
3. **5-minute waste loop** — WasteDetector alerts bypass the maturity
   lifecycle (urgency) and publish to the `noticed` zone, deduped/snoozeable
   via ledger signatures.

User actions flow back via the HA event bus: the card fires
`smart_suggestions_action` events (`run` / `accept` / `dismiss` / `snooze`);
the add-on listens on the HA WebSocket and updates the ledger.

### Deleted

`statistical_engine.py`, `scene_engine.py`, `narrator.py`, `pattern_store.py`,
`seed_patterns.json`, `dismissal_store.py`, `usage_log.py`, the
`_run_refresh_cycle` treadmill in `main.py`, and their tests. The ledger
absorbs dismissals and feedback. Expected LLM volume drops from ~2,880
calls/day to ~2–10 calls/week.

## Components

### PatternLedger (`pattern_ledger.py`, new — SQLite `/data/patterns.db`)

One row per unique pattern signature:

| column | purpose |
|---|---|
| `signature` (PK) | stable hash from `Candidate.signature()` (existing) |
| `miner_type, entity_id, action, details_json` | what the pattern is |
| `occurrences, conditional_prob, first_seen, last_seen` | evidence, refreshed each mining run |
| `lifecycle` | `emerging` → `confirmed` → `automated` \| `dismissed` |
| `title, description, automation_yaml` | LLM output, written once at confirmation (replaces `llm_cache.db`) |
| `dismiss_count, snoozed_until, accepted_runs` | user feedback; `accepted_runs` feeds phase-2 graduated trust |
| `prob_at_dismissal, dismissed_at` | resurface rule inputs |
| `describe_attempts` | LLM retry cap |

### LifecycleEngine (adaptive thresholds)

With `H` = days of recorder history available (capped at 30):

- **emerging**: occurrences ≥ 2 AND conditional_prob ≥ 0.4. Visible in the
  ingress panel only, labeled "still learning".
- **confirmed**: occurrences ≥ `max(3, round(H/6))` AND conditional_prob
  ≥ 0.6 + per-miner-type dismissal bump (+0.05 per 3 dismissals in the last
  7 days, capped at 0.9). Confirmed patterns get described and surface on
  the card.
- **dismissed**: set on user dismissal. Resurfaces only if conditional_prob
  rises ≥ 0.15 above `prob_at_dismissal`, or after 30 days.
- **automated**: set when an automation/scene is created from the pattern.
  Terminal for phase 1.
- Entities already automated in HA are skipped at mining time (existing
  check, kept).

### ContextMatcher (`context_matcher.py`, new)

Runs every 60 s off HAClient's in-memory states plus a cached ledger read:

- **Temporal**: now within ±30 min of the learned time window on a matching
  weekday, and the entity is not already in the target state.
- **Sequence**: trigger entity changed to its trigger state within the
  learned gap window.
- **CrossArea**: presence/motion event in the learned area within the last
  few minutes.
- Output: top `max_now_suggestions` (default 5) by conditional_prob →
  `sensor.smart_suggestions_now`. A suggestion that was run or dismissed is
  suppressed for the remainder of its current window.

### ModelRouter (`model_router.py`, new)

- Primary: Anthropic SDK (existing client).
- Secondary: one OpenAI-compatible client — covers both Ollama and MLX
  (`mlx_lm.server` / LM Studio).
- `describe_backend: primary|secondary` selects the describer; on failure the
  other backend is tried; if both fail, deterministic template text is used
  (e.g. "You usually turn on *Porch Light* around 8:00 PM on weekdays — 87%
  of the time over 24 days"). LLM output is polish, never a dependency.
- Failed describes don't block confirmation; retried up to 3 attempts, then
  template text stands until the pattern's evidence changes.

### ActionHandler

Listens on the HA WebSocket for `smart_suggestions_action` events:

- `run` → call the service now, increment `accepted_runs`.
- `accept` → AutomationBuilder creates a real HA automation (or scene for
  scene-shaped patterns) grounded in the pattern's actual trigger/time data;
  lifecycle → `automated`. Idempotent: lifecycle is checked before building,
  so a double-tap can't create two automations.
- `dismiss` / `snooze` → update ledger.

### Publisher + UI

- Three pushed HA sensors, re-pushed on every change and on HA restart:
  `sensor.smart_suggestions_now`, `sensor.smart_suggestions_discoveries`,
  `sensor.smart_suggestions_noticed` (payload in attributes).
- **Lovelace card** `smart-suggestions-card.js`: written to `/config/www/`
  (add-on `/config` mapping flipped to `rw`; `www/` created if missing),
  registered as a dashboard resource. Renders the three zones with
  Run / Automate / Dismiss buttons; re-renders reactively via the `hass`
  object; sends actions with `hass.callApi('POST', 'events/…')`. If resource
  registration fails, the ingress panel shows one-time setup instructions.
- **Ingress panel**: remains the deep hub — emerging patterns, ledger
  browser, deny list, live logs.

## Config surface (all wired, dead options removed — v4.0.0 breaking change)

`ai_api_key`, `ai_model` (primary), `secondary_base_url`, `secondary_model`,
`describe_backend`, `mining_interval_hours`, `waste_check_interval_minutes`,
`history_days`, `domains` allow-list, `max_now_suggestions`.

Removed: `ollama_url`, `ollama_model`, `narrator_provider`,
`refresh_interval`, `pattern_confidence_threshold`, `max_suggestions`,
`max_entities`, `history_hours`, `min_pattern_occurrences`,
`min_pattern_confidence`.

## Error handling

- **Recorder DB**: opened read-only; on lock/schema errors skip the run, log,
  retry next cycle. Each miner wrapped in its own try/except.
- **LLM**: fallback chain primary → secondary → template text (above).
- **HA WebSocket listener**: reconnect with exponential backoff (1 s → 60 s
  cap). Failed sensor pushes retried next tick — all state is
  reconstructable from the ledger.
- **Actions**: idempotent via ledger lifecycle checks.

## Testing (minimal — core only; personal local project)

- `LifecycleEngine`: transition rules and adaptive thresholds at several
  history depths — including the "day-5 install confirms something" case.
- `ContextMatcher`: match windows per miner type; suppression after
  run/dismiss.
- **Sensor-payload contract test**: synthetic recorder fixture → pipeline →
  assert the sensors' payload shape matches what the card expects (the exact
  silent failure that broke v3).
- Existing miner tests kept untouched. Everything else verified live against
  the user's HA instance.

## Phase roadmap (context, not in scope here)

1. **This spec**: foundation + visible value.
2. Graduated-trust auto-pilot (suggest-only → offer auto-run after N accepted
   runs; undo demotes), helper creation, native notifications.
3. Daily/weekly digest, energy/cost insights, presence-aware scene learning.
