# ATLAS Architecture Audit

Date: 2026-08-09
Scope: generic Windows desktop data-entry automation (primary regression target: MPF Download and Upload Form).

## Current Architecture

Layered pipeline, wired by `atlas/assistant/assistant.py`:

| Layer | Module | Status |
| --- | --- | --- |
| 1. Window attachment | `atlas/observe/window.py` (`WindowAttacher`) | WORKING (recently hardened: pid=0 recovery, strategy chain A/C/D/E, `[ATTACH]`/`[WINDOW]`/`[UIA]`/`[TARGET]` traces) |
| 2. UIA discovery | `atlas/observe/uia.py` (`UiaBackend`, `UiaNode`) | WORKING (flat walk + recursive fallback, `probe_editable_roots`, `control_text`, focused-element chain) |
| 3. Form model | `atlas/mapping/uia_map.py` (`UiaFieldMap`, `UiaFieldMapBuilder`) | WORKING (left labels / right fields / scroll containers / upload button) |
| 4. Source model | `atlas/understanding/source.py` (`SourceReader`, `SourceRecord`); `atlas/core/record_builder.py` | WORKING |
| 5. Field mapping | `atlas/mapping/mapper.py` (`SemanticMapper`), `atlas/mapping/uia_map.py` (`build_hybrid_mappings`) | WORKING |
| 6. Action planner | `atlas/reason/planner.py` (`ActionPlanner`); `atlas/workflow/field_engine.py` (`build_field_actions`) | WORKING |
| 7. Control interaction | `atlas/act/controls.py` (`ControlEngine`/`ControlInterface`), `keyboard.py`, `mouse.py` | WORKING |
| 8. Verification | `atlas/act/verify.py` (`CompositeVerifier` + 5 strategies) | **BROKEN** - see below |
| 9. Recovery | `atlas/reason/recovery.py` (`RecoveryPlanner`), `atlas/act/executor.py` (`_execute_with_recovery`) | **BROKEN** - see below |
| 10. Scroll | `atlas/workflow/scroller.py`, `field_engine.py` (`ScrollCapabilityCache`, `TargetNavigator`) | WORKING |
| 11. Submission | `atlas/workflow/loop.py` (`_submit_field_driven`) | WORKING |
| 12. Audit/Excel | memory store + debug JSON; `reports/` | PARTIAL (JSON audit, no Excel yet) |

## Working Components

- Attachment resolves HWND/PID/process/class/title for `Chrome_WidgetWin_1` and resolves the real form root for Chromium/Electron.
- UIA discovery finds 260 controls / 41 editable fields on the live MPF form.
- Text entry and many dropdown selections verified end-to-end (`MATCH` for App No, MBI Code, Full Name, Pincode, RAI Code, PHI Code, Gender, State, District, ...).
- UIA field map: 77 left labels / 40 right fields / 3+ scroll containers / upload button.
- Field-driven engine (`--field-driven`) walks an ordered queue, scrolls the right panel only, refreshes bboxes via UIA (no VLM per field).
- OCR already cached per `(engine, lang)` (`atlas/vision/ocr.py::create_ocr_reader`), and local region crops are supported (`OcrReader.read_region`).
- Pause/resume hotkeys, state machine, event bus, sandbox, plugins intact.

## Broken Components (root causes of the observed bugs)

1. **Verification is binary.** `CompositeVerifier.verify()` returns `(bool, evidence)`. There is no `UNKNOWN` status. An unreadable field (`uia read empty`, `vision read empty`, whole-window clipboard) is returned as `False` and treated exactly like a genuine mismatch. This is the root of bug #1/#2.
2. **Action retry on UNKNOWN.** `ActionExecutor._execute_with_recovery` retries the *action* whenever verification returns `False`, up to `max_retries+1`. When the problem is only read-back, this repeats the action needlessly and consumes 70-80 s per field (observed).
3. **Full map rebuild on local failure.** `field_engine._fill_from_queue` calls `_refresh_field_map_once()` (full UIA re-walk) after any failed fill, then re-runs OCR, then retries.
4. **Whole-window clipboard accepted as evidence channel.** It is currently *rejected* (good) but the rejection is collapsed into plain `False` (→ treated as MISMATCH instead of UNKNOWN). The executor then retries the action.
5. **OCR escalation not layered.** `VisionVerifier` is the 2nd strategy after UIA; combined with retries it runs OCR 2-3x per field. No per-control-type reader selection, no confidence gating.
6. **Recovery is retry-count based, not status based.** `RecoveryPlanner.decide` counts failures; it cannot distinguish `MISMATCH` from `UNKNOWN`, so it burns its ladder on unreadable fields.
7. **No control-type-aware verification.** Text, dropdown, date, checkbox, radio all go through the same generic pipeline; `read_focused` clipboard and `control_text` are the only real readers.
8. **No dependency awareness.** State→District→Taluk, Religion→Caste→Sub Caste, DOB→Nakshatra→Rashi→Pada are filled without waiting for the child control to become enabled/populated.
9. **No per-stage timing/audit.** A single watchdog budget per state; no field-level `discovery/action/verify/ocr/recovery` split; no `run_metrics.json`.

## Proposed Changes

1. **New `atlas/act/verification.py`** - `VerificationStatus` (MATCH/MISMATCH/UNKNOWN/NOT_APPLICABLE/PENDING), `VerificationResult`, evidence→status classifier, control-type-aware `VerificationEngine`, and field-result synthesis (`ACTION_SUCCESS_VERIFICATION_UNKNOWN`, etc.).
2. **Evolve `atlas/act/verify.py`** - keep the strategy classes (tests depend on them), add OCR normalization (`normalize_ocr_text`: strip trailing `V`, cursor/selection artifacts), keep whole-window clipboard rejection but expose a status classifier.
3. **`atlas/act/models.py`** - `ActionResult` gains `verification_status` + `verification_result`.
4. **`atlas/act/executor.py`** - `_verify` returns `VerificationResult`. On `UNKNOWN` do a **read-only recovery ladder** (re-read → refocus+re-read → local OCR) instead of re-running the action; classify as `ACTION_SUCCESS_VERIFICATION_UNKNOWN` when read-back stays unknown; only genuinely `MISMATCH` triggers corrective action retries (bounded). Structured `[VERIFY]` / `[VERIFY-FALLBACK]` / `[FIELD]` logging + per-field timing.
5. **`atlas/reason/recovery.py`** - status-aware `decide(verification_status, level)`: level-based escalation; UNKNOWN never escalates to a full map rebuild; bounded.
6. **`atlas/workflow/field_engine.py`** - gate `_refresh_field_map_once` to structural events (mismatch after refocus / scroll / target disappeared), never for UNKNOWN.
7. **`atlas/assistant/assistant.py`** - `_desktop_verifier` returns a `VerificationEngine` (control-type-aware, priority UIA→focused→local OCR→scoped clipboard).
8. **`atlas/act/verification.py::DependencyWait`** - adaptive polling for dependent dropdown readiness (enabled/option-count/subtree stabilization).
9. **Per-field timing** accumulated by the executor and flushed to `debug/performance/run_metrics.json`.
10. **Tests** for verification statuses, OCR normalization, read-only recovery, dependency waits, clipboard rejection classification.

## Files to Modify

- `atlas/act/verification.py` (new)
- `atlas/act/verify.py`
- `atlas/act/models.py`
- `atlas/act/executor.py`
- `atlas/reason/recovery.py`
- `atlas/workflow/field_engine.py`
- `atlas/assistant/assistant.py`
- `tests/test_verification.py` (new)

## Files to Preserve

- `atlas/observe/window.py`, `atlas/observe/uia.py` (working attachment/UIA)
- `atlas/mapping/*`, `atlas/understanding/*`, `atlas/reason/planner.py` (working mapping)
- `atlas/workflow/scroller.py`, `atlas/workflow/viewport.py` (working scroll)
- `atlas/act/hotkeys.py` (ESC/Ctrl+Shift+S/R/Q), `atlas/core/states.py`, `atlas/core/events.py`
- `plugins/mpf/*` (MPF regression target)
- `run_mpf_test.py`, `main.py`, `diagnose_mpf_attach.py`
