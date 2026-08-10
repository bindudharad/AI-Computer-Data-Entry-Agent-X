# BEST++ Architecture Audit

Status of the migration toward the BEST++ architecture (trace-driven, value-safe
data entry). Updated during the current session.

## Phase 0 - Architecture audit (trace of the real call chain)

### Field-driven path (preferred, uses UIA accessibility tree)
`loop.run` -> `_run_field_driven` -> `build_field_queue(field_map, record)` -> fill queue
ordered by target geometry (top-to-bottom form order) -> `_fill_from_queue` -> submit.

- Field list, labels and bboxes come from the UIA map (`atlas/mapping/uia_map.py`),
  not from vision geometry.
- Mapping is per-source-pair, one target per source, `unmapped_source` /
  `unmatched_fields` are tracked and reported (never silently dropped).

### Viewport/text-driven path (fallback + reveal-fill)
`loop.run` -> `_run_record` -> `mapper.map(record, fields)` -> `planner.plan_fill(...)`
-> executor. Also used by `_scan_fill_revealed` for fields revealed by scrolling.

### Mapper (`atlas/mapping/mapper.py`)
`SemanticMapper.map`:
1. Pass 1: exact/alias matches (canonical via `AliasResolver`).
2. Pass 2: fuzzy for the remainder with a known-vs-known safety
   (`_similarity` returns 0.0 for two distinct known concepts) and a threshold
   (default 0.55).

## Confirmed root causes for the value-shift bugs

1. **No value-type validation.** The mapper scored labels only; a date value
   could be typed into a non-date field and a phone number into a name field.
2. **Pass 2 was source-greedy.** The first source in record order grabbed the
   best-scoring remaining target; a weaker source could steal a target from a
   stronger one, and two sources could be assigned by coin-flip order.
3. **No confidence floor at execution.** `plan_fill` executed every mapping the
   mapper proposed, including sub-0.7 containment/fuzzy matches.
4. **Weak type gating.** `_type_ok` collapsed to a no-op for text targets because
   every text-receiving type list contained `UNKNOWN`.

## Phase 1 - Mapping integrity (implemented this session)

- **Value-type validation** (`_value_ok`): a date value only maps into a date
  field; email/phone/pincode/numeric values never map into name or date fields.
  Rejected pairings surface as `MappingResult.blocked` (source, target, reason)
  and are logged instead of being typed into the wrong field.
- **Target-aware pass 2**: each remaining target, in form (spatial) order, takes
  its best remaining source only when (a) that source has no stronger remaining
  target (mutual best) and (b) the target's runner-up source is clearly weaker
  (`_SCORE_GAP = 0.05`).
- **Execution confidence floor**: `ActionPlanner.plan_fill(..., min_confidence=0.85)`
  skips (and logs) sub-floor mappings, so weak proposals are never executed.
- **Surfacing**: `MappingResult.blocked` added to the dataclass and `to_dict`.

### Verified scenarios (regression tests added)
- DOB value -> District only target: blocked (`value-type`), surfaced unmapped.
- Mobile No -> Name only target: blocked.
- DOB -> Date of Birth: maps cleanly.
- Name vs Full Name with both targets present: exact binding preserved.
- Caste -> Sub Caste (0.72 containment): proposed by mapper, rejected by the
  0.85 execution floor.
- Application No -> PAN Number: rejected (known-vs-known safety).
- Pincode -> Pincode: maps cleanly.

Test suite: 512 passed (was 504; +8 new mapping/planner tests).

## Phase 2 - Value-shape repair (implemented this session)

- New module `atlas/understanding/value_shape.py` houses kind inference
  (`label_kind` / `value_kind`), value-type gating (`value_ok`) and repair
  (`repair_value`) so the mapper, planner and field engine share one source of
  truth. The mapper now imports its helpers from there instead of duplicating
  them.
- `repair_value(target_label, value)` normalises a value to the target field's
  expected format before it is typed: pincode/phone spacing stripped, ISO
  dates re-formatted to `DD/MM/YYYY`, numeric fields de-spaced. Text values and
  unknown field kinds are returned unchanged; repair never blocks a mapping.
- Applied at execution time only for **text fields** (`ActionPlanner.plan_fill`
  and `field_engine._actions_for` TYPE actions). Dropdowns/radios/date pickers
  keep the exact option/control value so option matching is never broken.
  The repaired value is also the VERIFY `expected`, so read-back compares
  against what was actually typed.

## Phase 3 - Deterministic verification (verified + one gap fixed)

- The verification stack already satisfied this phase: `CompositeVerifier`
  with target (DOM/UIA/clipboard/vision) strategies, placeholder detection,
  token-safe containment, file matching, currency/number normalisation and
  date-aware comparison (`date_tokens` / `dates_match`) that lets
  ``"1996-02-02"`` verify against a ``02 02 1996`` read-back.
- Fixed an asymmetry: `TargetFieldVerifier` (web DOM read-back) was the only
  verifier without `dates_match`, so an ISO source date could fail against a
  correctly-filled DOM field even though vision/UIA accepted it. Added it.

## Phase 4 - Recovery + sequential per-field fallback (verified present)

- `RecoveryPlanner` (retry -> refocus -> re-analyse -> skip/stop) is wired into
  the executor and surfaced via `RECOVERY` events.
- The field-driven path is bounded per field: `ProgressGuard` hard deadline,
  one retry then `mark_failed` (surfaced as a STOP result), capped scroll
  attempts, and a single-pass queue that can only mark done/failed - so a
  stuck field terminates the record instead of cycling.

## Phase 5 - Reporting (implemented this session)

- `MappingResult.to_dict()` already carried `blocked` / `unmapped_source` /
  `unmatched_fields` / `score` / `coverage`; `RecordResult` and
  `WorkflowSummary` carried the per-record and run-level aggregates.
- The live dashboard now surfaces them: the MAPPING handler reports
  ``mapped / blocked / unmapped / low-conf`` counts, and RECORD_COMPLETED /
  RECORD_FAILED log incomplete and blocked detail instead of just a status.
- `WorkflowSummary.blocked_fields` aggregates every value-type/ambiguity
  rejection across the run into the `WORKFLOW_COMPLETE` payload.

### Tests added (Phase 2/3/5)
- `tests/test_value_shape.py`: kind inference, value gating, repair behaviour,
  planner repairs TYPE values but not dropdown options, TargetFieldVerifier
  date parity, WorkflowSummary blocked aggregate.

Test suite: 512 + 14 = 526 passed.

## Remaining phases (not yet implemented)
- None - all five phases are now implemented.
