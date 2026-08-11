# CURRENT BOTTLENECK MAP

**Audit date:** 2026-08-10 · **Baseline:** 504/504 tests passing
**Scope:** Field-driven MPF fill path (`atlas/workflow/field_engine.py`,
`atlas/workflow/loop.py`), executor (`atlas/act/executor.py`), UIA
(`atlas/observe/uia.py`), map builder (`atlas/mapping/uia_map.py`), verification
(`atlas/act/verify.py`, `atlas/act/verification.py`, `atlas/assistant/assistant.py`).

Every finding below was traced from the real execution path (no assumptions).
Line numbers refer to the current tree.

## Fix status

| Phase | Scope | Status |
|---|---|---|
| 1 | UIA snapshot caching (single walk + scroll-container cache) | **DONE** — 493/493 green |
| 2 | ValuePattern-first text entry (+ dead read path fixed) | **DONE** — 501/501 green |
| 3 | No-op detection + UNKNOWN-not-PASS | **DONE** — 504/504 green |
| 4 | Dropdown cache / direct selection | **DONE** — 540/540 green |
| 5 | Watchdog two-level + logging | **DONE** — 553/553 green |
| 6 | Mapping coverage >=95% + verification hierarchy | **DONE** — 553/553 green |
| 7 | Final UNIVERSAL_TEST_REPORT.md | **DONE** |

---

## 1. UIA tree is NEVER cached — every operation re-walks ~260 nodes

**Phase 1 fix:** the map builder now does ONE `descendants()` walk and reuses it
for `editable_fields`/`text_nodes`/`buttons` (`uia_map.py build()`), and
`scroll_containers()` memoizes its result per `(handle, client_rect)` with a
3 s TTL (`uia.py` `_scroll_container_cache`). This kills the 3-5 walks per map
build and the re-walk per poll/scroll tick.

Remaining walks (from the original audit; values still true for paths not yet
refactored): every entry point expands the full Chrome accessibility tree, each
node costing multiple COM calls in `_flatten()` (`uia.py:1463-1561`):

| Walk | Function | Cost |
|---|---|---|
| flat | `descendants()` `uia.py:487-508` | 1 full tree |
| recursive | `walk_descendants()` `uia.py:551-592` | 1 full tree |
| recursive (typed) | `inspectable_nodes()` `uia.py:1320-1353` | 1 full tree |
| recursive (dicts) | `dump_tree()` / `_tree_dict` `uia.py:1308-1318` | 1 full tree |

**A single field-map build = 3–5 full walks** (`uia_map.py:148-222`):
`editable_fields` (153) → `text_nodes` (166) → `buttons` (187) →
`scroll_containers` (194, which itself walks again + `dump_tree`).

**Field-map rebuilds happen dozens of times per record** (all via
`_refresh_field_map_once` → `assistant.py:570-582` → `builder.build`):

| Trigger | Location | Rebuilds |
|---|---|---|
| record start | `loop.py:504` | 1 |
| failed fill retry | `loop.py:654` | 1 |
| dependent-field enable wait | `loop.py:712` | **every poll tick** (up to ~10-20 × per dependent field) |
| each scroll attempt | `loop.py:769`, `:797` | 1 each (up to 6) |
| each observed screen change | `loop.py:2111-2113` | 1 |

Worst case per below-fold field: **~30 full tree walks**.

**Verification also re-walks**: `control_text()` (`uia.py:760-788`) runs a full
DFS `_collect_texts` (`uia.py:790-829`) per read, never reusing the field node
already held in the queue (`field_engine.py:157`, `_node_match_key` at 124-134).

---

## 2. OCR runs on every read — no inference caching, lazy init only

- Engine load IS lazy (`ocr.py:83-126`, first-use in `_ensure_engine`) — good.
- **Inference results are never cached** (no `ocr_cache` anywhere). Every
  `_read_region_ocr` (`assistant.py:1038-1040`) does a fresh `grab_rect` +
  `read_image` → full PaddleOCR inference.
- `VisionVerifier` calls it **1-2× per field** (`verify.py:332, 344`), with a
  `time.sleep(0.35)` blank recapture (`verify.py:342`).
- Plus **1 whole-left-panel OCR per record** (`loop.py:2190`, `loop.py:2266-2269`).
- Worst case per field with read-recovery (default 2): **up to 6 OCR calls**.

---

## 3. Focus costs a double humanized mouse click

- Field engine emits `CLICK` as the focus step (`field_engine.py:689-695`),
  dispatched to `click_field` (`controls.py:119-125`) → `HumanMouse.click`
  (`mouse.py:138-145`).
- `type_value` then calls `_ensure_focus` (`controls.py:251-262`) which **clicks
  the same spot again** (`controls.py:261`). Two full ~35-step Bezier clicks per
  text field (~70 mouse sleeps + ~0.4-1.6 s each).
- `clear_field()` runs on **every** `type_value` (`controls.py:129`) even when
  the field was just focused and is empty.

---

## 4. Typing is per-character humanized with no ValuePattern write

**Phase 2 fix:** `ControlEngine` now takes an optional `value_setter` callable
(`controls.py`). `type_value`/`clear`/`choose_date` try it FIRST — writing via
UIA `ValuePattern.SetValue` through the new `UiaBackend.set_control_value`
(`uia.py`), which resolves the live element under the bbox and calls
`get_elem_interface(element.element, "Value").SetValue(...)`. Success = zero
focus click, zero clearing, zero keystrokes. Fallback path is unchanged.

**Critical discovery:** pywinauto 0.6.9's `UIAElementInfo` has NO
`value_pattern()` method. The old `hasattr(info, "value_pattern")` guards in
`_control_display_text`/`_flatten` were dead code in production — UIA reads
**silently returned nothing** (`control_text` returned `None` on live fields),
forcing OCR/clipboard for every verification. Both reads now fall back to
`get_elem_interface(raw_element, "Value").CurrentValue` (verified live:
Edit value `"Ask anything"` now readable; SetValue write verified live too).

Remaining: short values still go to per-char keyboard typing at
**0.05–0.25 s/char** when the field is not ValuePattern-writable (native
Win32 controls, read-only, or the pattern is absent); only values ≥ 25 chars
use the clipboard (`controls.py:131-133`).

---

## 5. Verification chain is expensive and partly redundant

Composite chain (`assistant.py:1031-1036`): `UiaValueVerifier` → `VisionVerifier`
→ `TargetFieldVerifier` → `ClipboardVerifier`.

Per round:
1. UIA `control_text` = **1 full subtree walk** (`uia.py:790-829`).
2. If empty → OCR 1-2× (`verify.py:332/344`).
3. `TargetFieldVerifier` `_desktop_read` (`assistant.py:1001-1006`) = Ctrl+A/Ctrl+C clipboard.
4. `ClipboardVerifier` = **another** Ctrl+A/Ctrl+C (`verify.py:283-293`).

Read-recovery ladder (`verification.py:194-227`) re-runs the **whole chain**
after a refocus click + `time.sleep(0.15)` — worst case 3 rounds =
3 UIA walks + up to 6 OCR + 6 clipboard grabs per field.

---

## 6. UNKNOWN is accepted as success (spec violation)

**Phase 3 fix:** UNKNOWN is now NEVER a verified pass. `executor.py` no longer
sets `verified=True` on an UNKNOWN read-back — the field is accepted as
written (`ok=True`, so it is never re-filled and never fails) but `verified`
stays `False`. The spec demands Sub Caste / Nakshatra UNKNOWN must NOT be
reported as success; it is now tracked and surfaced:

- `ActionResult.verification_state` → `ACTION_SUCCESS_VERIFICATION_UNKNOWN`
  (verified stays False) — see `models.py`.
- `RecordResult.unverified_fields` lists the written-but-unconfirmed fields;
  `WorkflowSummary.unverified` / `unverified_fields` count them; they appear
  in `timeline.json`, `failure.json`, `run_metrics.json` and
  `verification_debug.json`.
- No-op ALREADY_CORRECT detection (`executor._check_already_correct`,
  config `WORKFLOW_NOOP_DETECT`): a pre-write MATCH skips the write entirely
  and reports `ACTION_SUCCESS_VERIFICATION_ALREADY_CORRECT`. `set_control_value`
  (`uia.py`) also skips `SetValue` when the control already holds the value.
- A standalone VERIFY action keeps its honest status (no longer force-set to
  `verified=True`).

Suite: **504/504 green** (3 new tests: no-op skip, no-op off, unverified surfacing).

---

## 7. Fixed sleeps dominate the per-field budget

| Location | Sleep | Notes |
|---|---|---|
| `controls.py:262` | 0.12 s | `_ensure_focus` (×2 per text field) |
| `controls.py:130` | 0.1 s | after clear |
| `controls.py:157/163` | 0.35 s | dropdown_wait |
| `keyboard.py:60` | 0.05–0.25 s | per char |
| `mouse.py:130/136/141/143/145` | ~38 sleeps | per Bezier click |
| `executor.py:473` | 0.8 s | per scroll nudge |
| `executor.py:257` | 0.8 s | per recovery |
| `loop.py:768/796` | 0.3–0.5 s | per scroll settle |
| `verify.py:342` | 0.35 s | blank OCR recapture |
| `verification.py:223` | 0.15 s | refocus re-read |
| `loop.py:710` | 0.15–0.9 s | dependent-field poll ladder |

---

## 8. Browser launch — already fixed (attach-first)

`attach_auto` (`assistant.py:217-268`) uses `AttachFirstManager`
(`atlas/universal/attach.py`) with restart policy; LAUNCH is reached only when
`AUTO_LAUNCH_TARGET=true` and only fires ONE `attach_web`. No launch loop.

---

## Priority order (matches spec phase order)

1. **UIA snapshot caching** — kills the ~30 walks/field (biggest win).
2. **ValuePattern-first text entry + no-op detection** — skip the write when
   the field already holds the value (Sub Caste / Nakshatra reset no-op).
3. **Verification hierarchy** — UIA value reuse from the queue node, OCR only
   when UIA can't read, clipboard last; UNKNOWN must not become success.
4. **Focus cost** — one click per field, `_ensure_focus` skip when already focused.
5. **Fixed sleeps** — poll conditions instead of blind sleeps.
6. **Field mapping coverage** — Sub Caste / Nakshatra / Rashi / Pada declared.

Next: Phase 4 — dropdown cache / direct selection (the 1.7–2.0 s dropdown
open cost from the audit, section 7 row 4).

**Phase 4 fix (DONE):** `ControlEngine.select_option` now tries a
`option_setter` FIRST — `UiaBackend.select_option` (`uia.py`) selects combo
options without focus clicks / arrow keys / dropdown-open waits:

1. **Direct SelectionItem**: deepest selection-capable element under the field's
   bbox (fallback: whole-window root) is matched by normalized name and fired via
   `SelectionItemPattern.Select()`.
2. **ExpandCollapse**: if no item matched, `ExpandCollapsePattern.Expand()` →
   0.15 s popup settle → match-and-select → `Collapse()` so the popup never
   covers the next field.
3. **Option cache**: the live option list is read while the dropdown is open and
   cached per `(handle, bbox)` with a 3 s TTL (`_OPTION_CACHE_TTL`), so later
   selections on the same field — and the field-map builder
   (`uia_map._attach_declared`, which falls back to `cached_options` when the
   declared `options: []`) — reuse it without re-opening the dropdown.
4. **Failure = keyboard fallback**: `select_option` returns `False` (never
   raises) and the existing arrow/type path takes over, preserving behavior.

New tests: direct SelectionItem match, normalized-name match, no-match fallback,
ExpandCollapse path, option-cache store/expire/overwrite, and option_setter
first-then-fallback in `test_uia_select.py` (13 tests). Suite: **540/540 green**.

**Phase 5 fix (DONE):** two-level watchdog + dedicated logging.

- **Level 1** is the sandbox focus watchdog (`ExecutionSandbox._watchdog_loop`):
  target aliveness + foreground focus, pauses/refocuses/re-attaches. Already
  tested in `test_sandbox.py`.
- **Level 2** is the workflow state-budget watchdog (`AgentLoop._check_state_budget`):
  each state has a budget (defaults 10 s; WATCHING/WAITING 60 s, OBSERVING 45 s,
  THINKING 30 s; `WAITING_FOR_START_FIELD` never times out). An overrun now
  counts per state (`_state_overruns`), re-warns every `_overrun_repeat_log_seconds`
  (30 s) while it persists instead of logging once and going silent, and the
  RECOVERY event carries `elapsed` / `budget` / `overruns`. `_set` resets the
  tick counter on state entry so healthy progress never resurfaces.
- **Logging:** a new `watchdog` category writes `watchdog.log`; the loop logs
  overruns through `watchdog_logger` and `run()` dumps `debug/watchdog.json`
  consolidating level-1 focus events + level-2 state-overrun totals.
- New tests: budget normalization, no-overrun/zero-budget, single-fire,
  repeat-after-interval, reset-on-enter, watchdog.json dump in `test_watchdog.py`.

**Phase 6 fix (DONE):** mapping coverage >=95% + verification hierarchy locked.

- **Coverage:** `test_mpf_mapping_coverage_at_least_95_percent` exercises every
  field declared in `plugins/mpf/field_mapping.json` (full family form: Sub
  Caste / Nakshatra / Rashi / Pada / parent statuses / siblings / children /
  income) as source pair + form control and asserts `MappingResult.coverage
  >= 0.95` (`mapper.coverage` = mapped sources / total sources).
- **Verification hierarchy** (cheapest, occluded-safe strategy first): UIA value
  read → Vision (OCR) → TargetField (focused clipboard) → Clipboard last.
  `test_verification.py` now proves UIA short-circuits the chain, the chain
  falls through when UIA reads empty, and the clipboard is only consulted after
  both UIA and vision fail.
- Suite: **553/553 green**.
