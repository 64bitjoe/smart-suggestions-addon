# Smart Suggestions v4.1 — UI Polish + Graduated-Trust Auto-Pilot

**Date:** 2026-07-19
**Status:** Approved design, pending implementation plan
**Baseline:** v4.0.7 (pattern ledger live and verified; 17 clean discoveries)

## Goals

1. **UI round**: polish both surfaces — the Lovelace card (daily driver) and the
   ingress panel (hub) — to the dark iOS-style standard of the v3 panel.
2. **Auto-pilot**: graduated-trust auto-run. Patterns the user repeatedly runs
   get offered for promotion; promoted patterns execute automatically with a
   visible, undoable activity trail; any veto demotes and resets trust.

## Auto-Pilot

### Lifecycle addition

`emerging → confirmed → automated | dismissed` gains a sibling of
`automated`: **`autopilot`**. Transitions:

- `confirmed → autopilot`: user taps the promote chip (explicit, never
  automatic).
- `autopilot → confirmed`: user taps Undo on an activity entry, or Demote in
  the panel. Both reset `accepted_runs` to 0 — trust is re-earned.
- `autopilot` rows keep receiving evidence upserts like confirmed rows.

### Eligibility (promote chip shown)

All of: lifecycle `confirmed`; `accepted_runs >= 3`; `dismiss_count == 0`;
the acted-on entity's domain is not in `NEVER_AUTOPILOT_DOMAINS = {"lock"}`
(constant, extendable).

### Execution

The 60-second match loop partitions matched patterns by lifecycle:

- `confirmed` matches → the "now" suggestion zone (unchanged).
- `autopilot` matches → **execute**: `call_service` on the pattern's
  act-entity/action, insert an `activity` row, `record_run`, suppress the
  pattern for its remaining window (same suppression as a manual Run).

Safety throttle: at most `MAX_AUTORUNS_PER_HOUR = 10` executions globally
per rolling hour; excess matches fall back to the "now" zone as ordinary
suggestions and a warning is logged.

### Activity log

New ledger table `activity`:

| column | purpose |
|---|---|
| `id` INTEGER PK AUTOINCREMENT | |
| `ts` REAL | UTC epoch of execution |
| `signature` TEXT | pattern that fired |
| `act_entity`, `act_action` TEXT | what was done |
| `undone` INTEGER DEFAULT 0 | set by undo |

Publisher exposes the most recent 15 non-stale entries (last 24 h) as a new
`activity` zone; each payload item: `{activity_id, ts, title, act_entity,
act_action, undone, signature}`.

### Actions (event bus + panel `/action`, same dispatch path)

- `promote {signature}` — eligibility re-checked server-side; lifecycle →
  `autopilot`.
- `demote {signature}` — lifecycle → `confirmed`, `accepted_runs = 0`.
- `undo {activity_id}` — reverse the service call (`turn_on ↔ turn_off`),
  mark the activity row undone, and demote the pattern (as above).
  Undo is idempotent (already-undone rows are a no-op).

## UI

### Payload contract additions

`build_payload` gains `lifecycle`, `accepted_runs`, `can_promote` (computed
via the eligibility rule). Zones dict gains `"activity"`. The contract test
is updated to the new exact key set.

### Lovelace card v2 (`smart-suggestions-card.js`)

- Zone order: **Right now** → **Auto-pilot** (activity entries, newest
  first, Undo button on non-undone entries, subtle "already undone" state)
  → **Noticed** → **Discovered patterns**.
- Each suggestion row: miner-type icon via native `<ha-icon>`
  (temporal `mdi:clock-outline`, sequence `mdi:arrow-decision`,
  cross_area `mdi:motion-sensor`, waste `mdi:alert-circle-outline`);
  title; thin confidence bar (width = confidence, accent color); an
  occurrences chip ("11×"); description revealed by tapping the row
  (collapsed by default to keep the card compact).
- Promote chip on eligible rows: "⚡ Auto-run?" → fires `promote`.
- Autopilot-lifecycle patterns show a small ⚡ badge in Discoveries.
- Buttons unchanged otherwise (Run / Automate / Snooze / Dismiss), all
  HTML-escaped content, graceful empty state.

### Ingress panel v2 (`ws_server.py`)

Dark iOS-style hub (visual language of the v3 panel: #111418 background,
rounded #1b1f24 cards, #2f6fed accent):

- **Stats header**: counts per lifecycle (emerging / confirmed / autopilot /
  automated / dismissed), last mining summary line, add-on version.
  Served from a new `set_stats(dict)` on WSServer, filled by main after
  each mining pass.
- **Sections**: Right now, Auto-pilot (activity + list of promoted patterns
  with Demote buttons), Noticed, Discoveries, Emerging, Live logs.
- **Filter chips** above Discoveries/Emerging: All / temporal / sequence /
  cross_area (client-side filter).
- Same actions as the card plus Demote; all interpolation escaped.

## Error handling

- Auto-run service-call failure: activity row is still written with
  `undone = 0` but the failure is logged; no retry (next window fires
  again naturally). Undo of a failed run simply demotes.
- Promote on a no-longer-eligible row: rejected server-side, logged, no
  state change.
- Throttle trip: warning log + graceful fallback to suggestion.

## Testing (minimal — core only)

- Eligibility rule (`can_promote`) truth table incl. lock exclusion.
- Match-loop partition + throttle (pure logic extracted for testability).
- Undo reversal mapping and idempotency; demote resets `accepted_runs`.
- Updated payload contract test (exact key set + activity zone shape).

## Deferred (phase 3+)

- Trial→graduation: offer converting a proven autopilot pattern into a
  permanent HA automation after N clean auto-runs.
- Daily digest, presence scenes, energy insights.
- switch_as_x mirror dedup when wrapper/source names differ.
