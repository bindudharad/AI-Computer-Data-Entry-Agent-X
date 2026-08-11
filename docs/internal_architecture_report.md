# Internal Architecture Report (short)

Audit date: 2026-08-12 · Baseline: `pytest` 562/562 passed (note: `CURRENT_BOTTLENECK_MAP.md` documents 553/553; suite has grown since).

## Pipeline stages → files/functions

The pipeline is `observe → understand → reason → plan → execute → verify → (loop)`. Two execution paths exist: the **field-driven engine** (`atlas/workflow/field_engine.py`, the intended modern path) and the **legacy viewport pass** (`atlas/workflow/viewport.py` + `scroll.py`). The debug artifacts under `debug/mpf/` are produced by the **legacy path**.

| # | Stage | Module / key symbols | Role |
|---|-------|----------------------|------|
| 1 | Observe | `atlas/observe/uia.py` (`UiaBackend`, `UiaNode`), `atlas/vision/scene.py` | UIA window attach, ~260-node descendant walk, editable/combo/button discovery, client origin/size, `scroll_into_view` (ScrollItemPattern), combo Collapse in `finally`. |
| 2 | Understand | `atlas/understanding/source.py` (`SourceRecord`), `fields.py` (`EditableField`), `value_shape.py` (`repair_value`, marker groups) | Extract source label/value pairs; wrap editable fields with screen bbox; conservative value repair for dob/phone/pincode/numeric/name. |
| 3 | Reason / Plan | `atlas/reason/planner.py` (`ActionPlanner`, `FillPlan`), `atlas/mapping/mapper.py` (`SemanticMapper`, `MappingResult`), `uia_map.py` (`UiaFieldMap`) | Build one global label→field mapping, then a per-record fill plan ordered top-to-bottom (then left-to-right), regardless of control type. |
| 4 | Execute | `atlas/act/executor.py` (`ActionExecutor`), `atlas/act/controls.py` (`ControlInterface`, `ValueSetter`, `OptionSetter`, `ControlOutcome`) | Click/clear/type/select per action; max_retries=3, retry_delay=0.8; honors recovery decisions. |
| 5 | Verify | `atlas/act/verify.py` (`FieldVerifier`, `CompositeVerifier` = Clipboard+Target+UiaValue+Vision), `atlas/act/verification.py` (`VerificationStatus`: MATCH/MISMATCH/UNKNOWN/NOT_APPLICABLE/PENDING) | Read back per-field; `normalize_ocr_text` strips `{v,|}` caret markers; `looks_like_whole_window` detects global clipboard grabs. UNKNOWN is a deliberate "no false failure" state. |
| 6 | Recovery | `atlas/reason/recovery.py` (`RecoveryPlanner` → `RecoveryDecision`) | Decide retry/repair/skip after a failed verification; never silent-accept, never endless retry. |
| 7 | Loop / reporting | `atlas/workflow/loop.py` (`_execute_plan` ~L1170; field-driven perf log ~L1135-1168 → `debug/mpf/field_driven_perf.json`) | Orchestrate stages per record; write coverage (total/mapped/mapped_pct), queue statuses, skipped, failed, blockers. |
| 8 | Scrolling | `atlas/workflow/scroller.py` (`SmartScroller`: `scroll_to_y`, `best_scroll_method` cached pattern→dom→wheel→keyboard, `scroll_into_view`) | Right-panel-only, adaptive distance clamped [120,700] px, scroll-capability cache in field engine. |
| 9 | MPF specifics | `plugins/mpf/mpf_detector.py` (`MpfDetector`), `plugin.py`, `mpf_workflow.py` | Normalize field-map keys, split source/form/action sections, tag Upload button, UPLOAD_COMPLETED/RECORD_FAILED tracking. |

## Evidence from `debug/mpf/` (the reproduced problem)

- `field_map.json`: 40 right fields, 78 left labels, 37 mappings; `left_rect` (529,54,480,802), `right_rect` (972,325,257,1227); upload `"Upload Details"`.
- `planner.json` / `execution_plan.json` (legacy path): 60 actions for App No = click(972,325,257x26, conf 0.98, scroll_amount 3) → clear → type → verify. Mapping coverage **0.4565 → 21/46**.
- Unmapped last run: Date Of Birth, Physical Status, Body Type, Father Status, Father Name, Mother Status, Mother Name, Sister, Brother, Children Boy/Girl, ECI Code, Emp Status, Annual Income, Blood Group, Complexion, Education.
- All MPF ComboBoxes report `options=0` (DOB split = 3 unnamed combos at y≈440).
- `verification_debug.json`: 41 checks → MATCH 29, NOT_APPLICABLE 9, UNKNOWN 2, MISMATCH 1.
  - `uia-rashi` MISMATCH: uia read placeholder `-- Select --` | vision empty | no value | **clipboard read-back is whole-window (2449 chars)**.
  - `uia-height`, `uia-weight` UNKNOWN: uia read empty | vision empty | **clipboard read-back is whole-window (2467 chars)**.
- `logs/atlas_2026-08-11.log`: "uia map built: 78 left labels, 46 right fields, 0 scroll containers, upload=True" (11:29:21) and "40 right fields" (11:29:35) — field count fluctuates 40↔46 with snapshot timing.

## Root causes per documented bug class

| # | Bug | Root cause (code) |
|---|-----|-------------------|
| 1 | Only 21/46 mapped | `SemanticMapper` exact/alias pass too narrow; unnamed DOB-split combos have no label; source keys missing aliases (e.g. ECI Code, Physical Status). One global mapping is right but coverage is low. |
| 2 | "no value for combobox X - skipping dropdown" | Combo fill skipped when options=0 / no resolved source value → field silently skipped. |
| 3 | Low-confidence target loss | Targets pruned instead of held as LOW_CONFIDENCE/PENDING_REVIEW (must stay in queue per spec). |
| 4 | Rashi/Height/Weight options=0 OCR loops | No fallback ladder for 0-option combos; 10–20s OCR re-read loops. |
| 5 | Whole-window clipboard verification | `ClipboardVerifier` read-back collapses to whole-window when field-scoped read is empty; `looks_like_whole_window` detects it but the outcome is still UNKNOWN/MISMATCH. |
| 6 | Repeated full-screen OCR | VisionVerifier/observe re-OCR whole window per step instead of field region only. |
| 7 | UIA map rebuild per action | Legacy path re-walks ~260 nodes each action (Phase 1 mostly fixed via cache; still fluctuates). |
| 8 | Order/field loss after scroll | Legacy viewport path has no stable field identity; bbox-only targets rotate after scroll. |
| 9 | Unrelated-field failure aborts others | No per-field isolation in old path; one failure retries/aborts unrelated fields. |
| 10 | UNKNOWN verification loops | Old code collapsed UNKNOWN→False causing 70–80s retry ladders; fixed in `verification.py` (UNKNOWN now passes). |
| 11 | Source re-read per record | `SourceRecord` rebuilt each record; no read-once cache. |
| 12 | Schema rediscovery per record | field_map rebuilt every record; stable schema/ids not cached. |
| 13 | DOB split combos unfilled | 3 unnamed combos (day/month/year) never mapped to DOB source value. |
| 14 | Dependency ordering missing | No dependency graph (State→District→Taluk, Caste→Sub Caste) → cascading failures. |
| 15 | No completeness gate | Submission allowed with unmapped/failed fields; no final VERIFIED/FAILED audit summary. |

## Redesign components (not yet implemented)

- `TargetField` registry: `field_id`, `stable_id`, `label`, `control_type`, `section`, `bbox`, `scroll_container_id`, `document_order`, `source_key/value`, mapping/execution/verification status, `confidence`, `retry_count`. States UNMAPPED / NO_SOURCE / LOW_CONFIDENCE / PENDING_REVIEW remain in queue.
- `SourceRecord` cache (read once per app session).
- One-to-one bipartite mapping built once (no per-field remapping).
- `FieldPlanBuilder` + `PlanValidator` (visual top-to-bottom order + dependency graph).
- Sequential cursor `current_index` (no random jumps).
- Adapters: `TextFieldAdapter` / `ComboBoxAdapter` / `DateFieldAdapter` with options=0 fallback ladder (direct UIA select → type+Enter → keyboard nav — all OCR-free).
- Dependency graph: State→District→Taluk; Caste→Sub Caste.
- Smart scroll: right-panel container only, min delta, stable IDs across scroll.
- Bounded retry queue (per-field, then explicit non-silent mark).
- `CompletenessEngine` final audit: VERIFIED / ALREADY_CORRECT / NO_SOURCE / NOT_APPLICABLE / FAILED; submit blocked unless PASS.
