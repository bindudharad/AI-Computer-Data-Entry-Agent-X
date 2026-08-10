# Performance Audit — AI Computer Agent (v5, bbox-refresh baseline)

Audited against the live MPF desktop form (Chrome-hosted entry form + left
source-data panel). Numbers are observed from `logs/` + `debug/mpf/` artifacts
of the v2-v5 runs plus the 2026-08 baseline test suite (`262 passed`).

## 1. Loop model under audit

The v5 workflow (`atlas/workflow/loop.py`, `scan_reveal_fields=True`) uses the
**fill-visible -> verify -> viewport gate -> dual-panel scroll -> full re-observe**
model. Per record the flow is:

1. Observe once (VLM) at record start.
2. `_scan_fill_revealed_rounds` loops: full VLM re-observe every round.
3. Re-OCR the left source panel every round (`_refresh_source_record`).
4. Fill visible unhandled fields via the executor (which itself verifies each
   TYPE/SELECT/TOGGLE/CHOOSE_DATE with another re-observe).
5. Gate on `can_reveal_scroll` (ViewportModel NO SCROLL RULE minus verify gate).
6. Scroll **both** panels in lockstep, then settle 300-500 ms and re-observe.

## 2. Time sinks (ranked)

| # | Sink | Where | Cost |
|---|------|-------|------|
| 1 | Full VLM re-observe **every round** | `_scan_fill_revealed_rounds` → `_observe()` | ~1 VLM call/round |
| 2 | Left-panel OCR **every round** | `_refresh_source_record` → `pair_source_pairs` | PaddleOCR on left rect/round |
| 3 | Executor per-action verification | `ActionExecutor._verify` re-observes | ~1 VLM call/value field |
| 4 | Dual-panel lockstep scroll + settle | `_scroll_panels` scrolls LEFT **and** RIGHT | 2x panels, 300-500ms settle each |
| 5 | Scroll verification re-observe | `_scroll_containers` `_verify` closure | 1 VLM call/panel scroll |
| 6 | No per-field hard timeout | `ProgressGuard` absent | a stuck field stalls the record |

## 3. Root causes

### 3.1 Loop must finish ALL visible fields before any scroll
`can_reveal_scroll` requires every visible field to be handled (and the
viewport gate to pass) before the FIRST scroll. On a long MPF form this means
most scrolling happens only after the whole first viewport is done, and each
round re-observes the entire screen to rediscover what is now visible.

### 3.2 BBox == identity
A field's identity is its `element_id` derived from the bbox. After any scroll
the bbox changes, so the same physical field looks "new", forcing re-scan and
re-verify of the same control (`handled_ids` in `_scan_fill_revealed_rounds`).

### 3.3 Source re-read every round
`_refresh_source_record` re-OCRs the left panel each round. Values never
change within a record; the OCR is pure waste once the source is cached.

### 3.4 Both panels scrolled in lockstep
`DualPanelScroll` keeps LEFT and RIGHT synchronized. When the source is cached
(labels don't change), scrolling LEFT is unnecessary work and, worse, can
desync progress detection.

### 3.5 Container mis-selection blocks bottom detection
`pick_left_right_containers` (`atlas/workflow/scroller.py`) can pick the **web
root** (`RootWebArea`, `vertical_scroll_percent: -1.0`, covers the whole client
area) as the LEFT panel. Its percent is `-1.0`, so scroll verification never
sees "bottom" and the reveal pass keeps scrolling or stalls.

Observed on the real MPF form:

- RIGHT entry form container: `has_scroll_pattern: true`, percent `24.8`.
- LEFT root mis-pick: `vertical_scroll_percent: -1.0`.

### 3.6 No capability cache
Every scroll runs the full escalation ladder (DOM -> pattern -> wheel -> drag
-> keyboard) with 3 attempt sizes each and a verify re-observe per attempt,
even when the winning method was already discovered on the previous scroll.

## 4. Recommended redesign (implemented as the field-driven path)

Replace the viewport-round model with a **field queue driven** engine
(`atlas/workflow/field_engine.py`) while keeping the legacy path intact:

1. **Build the queue once per record** from the full UIA field map
   (`right_fields`, which already includes below-fold controls), ordered by
   (top, left). Source values come from the cached `SourceRecord` via the
   existing `mappings`.
2. **Stable identity = node handle -> automation_id+name -> name+visual order.**
   BBox is position only; a scroll updates the bbox without losing the field.
3. **Position refresh = UIA only.** `field_map_refresh()` re-queries UIA
   (no VLM). A full VLM observe is reserved for the post-submit success check.
4. **Scroll RIGHT only.** LEFT is never scrolled when the source is cached.
5. **Scroll method capability cache** per container
   (pattern -> dom -> wheel -> keys). Pattern is tried first on a container
   that exposes it (`has_scroll_pattern: true`).
6. **Adaptive scroll distance** = estimated gap clamped to [120, 700] px
   (never the fixed 250-350 px band).
7. **Scroll progress** verified via container percent increase or target-y
   change (no full re-observe).
8. **No explicit VERIFY actions** in fast mode; the executor still
   self-verifies TYPE/SELECT/TOGGLE/CHOOSE_DATE.
9. **Per-field hard timeout** (20 s) via `ProgressGuard`; a failing field is
   refreshed + retried once, then marked failed and skipped — one optional
   field never aborts the record.
10. **Submit only when the queue is empty**, followed by ONE re-observe to
    confirm the success indicator / record-key change.

## 5. Expected impact

- VLM observes per record drop from `O(rounds + fields)` to `O(1)` (post-submit
  only), cutting the dominant cost on multi-field forms.
- Left-panel OCR eliminated per round (cached source).
- Right-panel-only scrolling halves scroll work and removes the lockstep desync.
- Container mis-picks filtered so bottom detection terminates reliably.
- Scroll escalation replays stop after the first success per container.
