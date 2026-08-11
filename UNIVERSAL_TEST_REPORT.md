# UNIVERSAL TEST REPORT

**Project:** ATLAS AI Computer Agent (v5 bbox-refresh)
**Report date:** 2026-08-11
**Suite result:** 553/553 tests passing
**Duration:** ~2 min 22 s (`pytest -q`)
**Suite root:** `DataEntry/`

This report is the Phase 7 deliverable closing the CURRENT_BOTTLENECK_MAP.md
performance plan (Phases 1-7). It documents what was built, what the 553 tests
lock in, and the end-to-end performance outcome.

---

## 1. Test suite at a glance

| Area | File | Tests |
|---|---|---|
| Workflow loop | `test_workflow.py` | 18 |
| MPF end-to-end | `test_mpf_integration.py` | 5 |
| MPF diagnostic / mapping | `test_mpf_diagnostic.py` | 11 |
| UIA read + map flow | `test_uia_read.py`, `test_uia_flow.py` | 22 + 23 |
| UIA direct selection (Ph.4) | `test_uia_select.py` | 13 |
| Watchdog two-level (Ph.5) | `test_watchdog.py` | 9 |
| Sandbox / focus confinement | `test_sandbox.py` | 15 |
| Executor + verification | `test_executor.py`, `test_verification.py` | 18 + 19 |
| Verification strategies | `test_verify.py`, `test_verify_and_dropdown_recovery.py` | 27 + 4 |
| Field engine + scroll | `test_field_engine.py`, `test_scroll*.py`, `test_scroller.py`, `test_viewport.py`, `test_overflow_scroll.py` | 33 + 34 + 18 + 10 + 6 |
| Controls | `test_controls.py` | 18 |
| Planner / mapping / states | `test_planner.py`, `test_mapping.py`, `test_states.py`, `test_record_builder.py` | 9 + 17 + 9 + 10 |
| Universal engine | `test_universal_*.py` | 67 |
| Web automation | `test_web*.py` | 34 |
| Logging / events / config | `test_logging.py`, `test_events.py`, `test_config.py` | 6 + 4 + 7 |
| Misc (attach, clipboard, hotkeys, models, memory, preprocess, sections, controller, launch, value-shape) | — | 53 |
| **Total** | 53 files | **553** |

---

## 2. Phase deliverables (Phases 1-7 of CURRENT_BOTTLENECK_MAP.md)

| Phase | Scope | Status | Verified by |
|---|---|---|---|
| 1 | UIA snapshot caching (single `descendants()` walk + scroll-container cache, 3 s TTL) | DONE | 493/493 |
| 2 | ValuePattern-first text entry (dead `value_pattern()` read path fixed via `get_elem_interface(raw, "Value")`) | DONE | 501/501 |
| 3 | No-op `ALREADY_CORRECT` detection + UNKNOWN-not-PASS + `unverified_fields` | DONE | 504/504 |
| 4 | Dropdown cache / direct selection (`SelectionItemPattern`/`ExpandCollapsePattern` + per-field option cache) | DONE | 540/540 |
| 5 | Watchdog two-level + logging (sandbox focus watchdog + state-budget overrun escalation, `watchdog.log` / `watchdog.json`) | DONE | 553/553 |
| 6 | Mapping coverage >=95% + verification hierarchy locked | DONE | 553/553 |
| 7 | Final UNIVERSAL_TEST_REPORT.md | DONE | 553/553 |

---

## 3. What Phase 4 added (dropdown cost elimination)

The audit measured a 1.7-2.0 s per-field cost to open a dropdown before typing
an arrow-select. `ControlEngine.select_option` now tries an injected
`option_setter` FIRST (`atlas/act/controls.py`):

1. **Direct SelectionItem** — deepest selection-capable element under the field
   bbox (fallback: window root) is matched by normalized name and fired with
   `SelectionItemPattern.Select()`.
2. **ExpandCollapse** — if no item matched, `ExpandCollapsePattern.Expand()` →
   0.15 s settle → match-and-select → `Collapse()` so the popup never covers
   the next field.
3. **Option cache** — the live option list is read while the dropdown is open
   and cached per `(handle, bbox)` with a 3 s TTL; the map builder
   (`uia_map._attach_declared`) falls back to the cache for declared-but-empty
   option lists (Sub Caste, Nakshatra, Rashi, City, State, District, Taluk, ...).
4. **Never raises** — failure returns `False` and the existing arrow/type path
   takes over, so behavior is preserved everywhere else.

Key files: `atlas/observe/uia.py`, `atlas/act/controls.py`,
`atlas/assistant/assistant.py`, `atlas/mapping/uia_map.py`.

---

## 4. What Phase 5 added (two-level watchdog + logging)

- **Level 1** — `ExecutionSandbox._watchdog_loop` (target alive + foreground
  focus, auto-refocus, pause/resume, bounded re-attach). Already tested.
- **Level 2** — `AgentLoop._check_state_budget` now counts overruns per state,
  re-warns every 30 s while the state persists (instead of once and silent),
  and publishes structured `RECOVERY` events (`elapsed` / `budget` / `overruns`).
  `_set` resets the counter on state entry.
- **Logging** — new `watchdog` category → `watchdog.log`; `run()` writes
  `debug/watchdog.json` consolidating level-1 focus events and level-2 overruns.

Key files: `atlas/core/logging.py`, `atlas/workflow/loop.py`.

---

## 5. What Phase 6 added (mapping coverage + verification hierarchy)

- **Coverage >=95%** — `test_mpf_mapping_coverage_at_least_95_percent`
  exercises every field in `plugins/mpf/field_mapping.json` (full family form)
  and asserts `MappingResult.coverage >= 0.95`.
- **Verification hierarchy** — UIA value read → Vision (OCR) → TargetField
  (focused clipboard) → Clipboard last. `test_verification.py` proves UIA
  short-circuits, falls through when UIA reads empty, and clipboard is the last
  resort.

---

## 6. Performance outcome

The per-field performance chain is now:

1. **One cached UIA walk** — the map builder reuses a single `descendants()`
   result instead of 3-5 full walks per build (~30 walks/field worst case
   eliminated).
2. **ValuePattern-first writes** — `SetValue` skips focus click + clear +
   keystrokes for ValuePattern-capable controls; no-op detection skips the
   write when the field already holds the value.
3. **Direct dropdown selection** — SelectionItem/ExpandCollapse + option cache
   replaces the 1.7-2.0 s dropdown-open cost for combos.
4. **Verification hierarchy** — UIA value read (occluded-safe, zero clicks)
   before OCR/clipboard; UNKNOWN never escalates to corrective retries.

Timing data is written to `debug/performance/run_metrics.json` per run.

---

## 7. Test suite command

```
python -m pytest -q        # 553 passed in ~142 s
```

All phases of CURRENT_BOTTLENECK_MAP.md are **DONE**.
