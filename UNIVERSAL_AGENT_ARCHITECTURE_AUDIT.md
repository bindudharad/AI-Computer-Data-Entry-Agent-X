# UNIVERSAL AGENT ARCHITECTURE AUDIT

Audit of the ATLAS AI Computer Agent (v5 bbox-refresh) performed as Phase 1 of
the universal-agentry upgrade. Baseline: `python -m pytest -q` → **376 passed**.

---

## 1. Current Architecture (as-is)

```
main.py / run_mpf_test.py
        │  (CLI entry points)
        ▼
atlas.assistant.Assistant            wiring layer; owns every component
        │ attach_desktop / attach_desktop_by_click / attach_web
        ▼
atlas.target.base.TargetAdapter      ABC: attach / detach / observe / is_alive / read_field_value
   ├── DesktopTarget   (atlas/target/desktop.py)   Win32 window + UIA field map
   └── WebTarget       (atlas/target/web.py)       Playwright page (vision-first, DOM channel)
        │
        ▼
atlas.workflow.loop.AgentLoop        Observe→Understand→Reason→Plan→Execute→Verify→Next
   ├── viewport-round path  (_run_record + _scan_fill_revealed)     legacy
   └── field-driven path    (_run_record_field_driven + field_engine) performance
        │
        ├── atlas.mapping.mapper.SemanticMapper    label→field (exact/alias/fuzzy)
        ├── atlas.reason.planner.ActionPlanner      actions (CLICK/TYPE/SELECT/...)
        ├── atlas.act.executor.ActionExecutor        execute + verify + recovery
        ├── atlas.act.sandbox.ExecutionSandbox       focus guard / watchdog (no launching)
        ├── atlas.workflow.field_engine             queue, stable id, scroll cache, navigator
        ├── atlas.mapping.uia_map.UiaFieldMapBuilder desktop form geometry from UIA
        └── atlas.plugins.manager.PluginManager      hooks (MPF plugin)
```

### Key components and files

| Concern | File(s) | Status |
|---------|---------|--------|
| Window discovery (Win32) | `atlas/observe/window.py` (`WindowAttacher`) | mature (A/C/D/E strategy chain) |
| Desktop attachment | `atlas/target/desktop.py`, `atlas/assistant/assistant.py` | mature, MPF-proven |
| UIA backend | `atlas/observe/uia.py` (`UiaBackend`, field map builder, scroll containers) | mature |
| Web target | `atlas/target/web.py` (`WebTarget`, `DomControlEngine`) | launches a NEW browser |
| Browser launch | `atlas/target/web.py:306-325` | **LAUNCH-FIRST** (the defect) |
| Watchdog / focus guard | `atlas/act/sandbox.py` (`ExecutionSandbox._watchdog_loop`) | never launches, pauses on focus loss |
| Recovery | `atlas/reason/recovery.py`, `atlas/act/executor.py` | never restarts applications |
| Field engine | `atlas/workflow/field_engine.py` | queue, stable id, ScrollCapabilityCache, TargetNavigator, ProgressGuard, PerfTracker |
| OCR | `atlas/vision/ocr.py`, `atlas/vision/preprocess.py` | fallback-only |
| VLM scene | `atlas/vision/scene.py`, `providers.py`, `models.py` | primary perception |
| Plugins | `atlas/plugins/manager.py`, `plugins/mpf/` | MPF-specific only |
| Config | `atlas/config.py` | no universal/launch settings |
| Controller | `atlas/assistant/controller.py` | JSON server |
| Overlay/Dashboard | `atlas/overlay.py`, `atlas/dashboard.py` | status UI |

---

## 2. Existing browser flow (why a fresh browser is launched)

`Assistant.attach_web()` → `WebTarget.attach(url, browser, headless)`:

```
playwright.sync_api.sync_playwright().start()
  └─ chromium.launch(headless=...)      ← ALWAYS launches a brand-new browser
      └─ new_context(viewport=...)
          └─ new_page() + goto(url)     ← opens a NEW tab, even if the site
                                          is already open in the user's browser
```

- **No detection** of an existing Chrome/Edge/Firefox.
- **No CDP connect** to an already-running browser.
- **No tab inspection** / target ranking.
- A user with the target site already open gets a *duplicate* window + tab.

---

## 3. Desktop flow (MPF - the current working path)

```
run_mpf_test.py --records N --field-driven
  └─ assistant.attach_desktop(title="MPF") / attach_desktop_by_click()
      └─ WindowAttacher (title match → A/C/D/E UIA discovery → editable-controls check)
          └─ UiaFieldMapBuilder.build(handle)  → ordered right-panel field map
              └─ AgentLoop._run_record_field_driven
                  └─ PendingFieldQueue (stable ids) + UIA-only position refresh
                      └─ right-panel-only adaptive scroll (capability cache)
                          └─ submit → single VLM verify → next record
```

This path is correct, fast (when `WORKFLOW_FIELD_DRIVEN=1`) and MUST NOT be
broken. It is already "attach-first" in spirit (it attaches to an existing
window, never launches MPF). It becomes the **generic desktop/UIA engine**.

---

## 4. Watchdog flow

`ExecutionSandbox` runs a daemon thread (`_watchdog_loop`, 0.25 s poll) that:

1. Checks the target window is still alive (`assert_target_alive`).
2. Checks the foreground window still belongs to the target
   (`_window_belongs_to_target` → HWND / GA_ROOT / GA_ROOTOWNER).
3. Auto-refocuses after ≥3 focus-loss ticks (cooldown 5 s), pauses after 10.
4. Bounded recovery (`max_recovery_attempts=5`) → FAILED, never loops forever.

**The current watchdog never launches or restarts anything.** It only pauses
and refocuses. The launch-loop behaviour described in the brief does not exist
in this codebase yet; it is a *future risk* of the LAUNCH-FIRST web path, and
this upgrade must guarantee it can never appear.

---

## 5. Recovery flow

- `atlas/reason/recovery.py` — per-field corrective decisions
  (retry / refocus / re-acquire / scroll / alternate method / user intervention).
- `atlas/act/executor.py` — bounded retries (`WORKFLOW_MAX_RETRIES_PER_ACTION=3`),
  read-recovery ladder for UNKNOWN verification (never re-fills), UNKNOWN ≠ FAIL.
- No code path restarts the application. `RestartPolicy` (new) will formalise this.

---

## 6. Why the unwanted "browser relaunch loop" could happen

| Trigger | Current behaviour | Correct behaviour |
|---------|-------------------|-------------------|
| User already has the site open; user runs `--web` | `WebTarget.attach()` launches a second browser, opens a duplicate tab | Detect existing browser + tab, connect via CDP, or attach the browser window via UIA |
| Browser open but CDP unavailable | `attach()` would still launch a fresh copy | BrowserHealthState = DISCONNECTED → BROWSER_UIA fallback → never launch |
| Target tab not active | n/a (always a new tab) | Inspect all tabs, score, select the target tab |
| No browser at all | n/a | MISSING → only launch if `AUTO_LAUNCH_TARGET=true` |

---

## 7. Performance bottlenecks (field-driven MPF path)

| # | Bottleneck | Location | Cost |
|---|------------|----------|------|
| 1 | Per-action verification chain (UIA + OCR + clipboard + vision) | `atlas/act/verification.py`, `_desktop_verifier` | several composite reads per value action |
| 2 | `verify_after_action` always on, even when UIA read is authoritative | `assistant._desktop_verifier` | unnecessary OCR fallbacks |
| 3 | Per-field click focus before every fill | `field_engine._actions_for` (include_focus_click) | ~1 click per field |
| 4 | Dropdown opens via click + keyboard even when the value is directly settable | `ControlEngine.select_option` | 1.7–2.0 s observed |
| 5 | No method learning (no MethodProfile cache) | n/a (absent) | every field re-discovers its own best path |
| 6 | No form fingerprint reuse across records | `UiaFieldMapBuilder.build` re-queried per record refresh | UIA re-walk per record |
| 7 | Human-like typing delays on DOM-capable targets | `HumanKeyboard` | not an issue for desktop; must be DOM-first on web |

Observed MPF field cost: `Mother Tongue select ≈ 3948 ms` vs. raw OCR ≈ 50–100 ms.

---

## 8. Files to CHANGE

| File | Change |
|------|--------|
| `atlas/config.py` | add `UniversalConfig` (TARGET_MODE, AUTO_LAUNCH_TARGET, PREFER_EXISTING_TARGET, ALLOW_BROWSER_LAUNCH, MAX_RECOVERY_ATTEMPTS, TARGET_DISCOVERY_TIMEOUT, SMART_WAIT, LEARN_METHODS, STRICT_FOCUS_GUARD, CDP ports) |
| `atlas/target/web.py` | `WebTarget.attach_existing()` (CDP connect), attach-first gate on `attach()`, richer DOM field discovery |
| `atlas/assistant/assistant.py` | `attach_auto()` universal flow, attach-first web, browser-UIA fallback, launch guard |
| `main.py` | `--auto`, universal `--attach`, attach-first web; concise logging |
| `run_mpf_test.py` | keep; add universal entry (`--auto`) without changing MPF defaults |

## 9. Files to CREATE (new modules)

| New module | Purpose |
|------------|---------|
| `atlas/universal/models.py` | `TargetEnvironment`, `CandidateTarget`, `TargetSession`, `TargetLock`, `BrowserHealthState` |
| `atlas/universal/detector.py` | `UniversalTargetDetector` — windows + browser processes + tabs + ranking |
| `atlas/universal/classifier.py` | `ApplicationClassifier` — environment/framework/capabilities |
| `atlas/universal/attach.py` | `AttachFirstManager` — DISCOVER→CLASSIFY→ATTACH→VERIFY→AUTOMATE, cases A–H |
| `atlas/universal/restart_policy.py` | `RestartPolicy` (NEVER / ON_USER_REQUEST / ON_CRASH_ONLY / AUTO) |
| `atlas/universal/smart_wait.py` | `SmartWait` — adaptive polling, state-change waits |
| `atlas/universal/learning.py` | `MethodProfile`, `MethodLearner` — per-field method memory |
| `atlas/universal/performance.py` | universal run performance report (`debug/performance/universal_run.json`) |
| `atlas/web/__init__.py` | web package marker |
| `atlas/web/browser_discovery.py` | enumerate browser processes, CDP endpoints, command lines |
| `atlas/web/cdp.py` | CDP connect + target/tab listing helpers |
| `atlas/web/tabs.py` | tab scoring / selection |
| `atlas/web/fields.py` | `WebFieldDescriptor` + enhanced DOM inspector |
| `tests/web_apps/universal_form/` | generic web test application (+ React-style page + server) |

## 10. Files to PRESERVE (do not touch)

`atlas/workflow/field_engine.py`, `atlas/workflow/scroller.py`, `atlas/workflow/scroll.py`,
`atlas/workflow/viewport.py`, `atlas/observe/uia.py`, `atlas/observe/window.py`,
`atlas/act/sandbox.py`, `atlas/act/executor.py`, `atlas/act/verification.py`,
`atlas/mapping/mapper.py`, `atlas/mapping/uia_map.py`, `atlas/memory/store.py`,
`atlas/reason/*`, `atlas/vision/*`, `atlas/plugins/*`, `plugins/mpf/*`, `tests/*`.

The MPF adapter lives behind the plugin layer; the universal engine never
references MPF. New functionality is additive.

---

## 11. Target architecture (final)

```
User
 │
 ├── ATLAS Controller (assistant + CLI + JSON server)
 │
 ├── Universal Target Detector ── windows / browser processes / tabs / CDP
 ├── Target Ranking ──────────── score + confidence per candidate
 ├── Attach-First Manager ────── cases A–H; launch only if allowed
 ├── Environment Classifier ──── WEB_DOM / BROWSER_UIA / ELECTRON / DESKTOP_UIA / ...
 │
 ├── Automation backends
 │   ├── WEB_DOM   (Playwright + existing-browser CDP connect)
 │   ├── UIA       (desktop field map — current MPF engine)
 │   ├── ELECTRON/CDP, WIN32, KEYBOARD/MOUSE, VISION/OCR
 │
 ├── Universal Field Model + Semantic Mapper + Action Planner
 ├── Fastest Reliable Action ── method learning (MethodProfile)
 ├── Smart Verification ────── DOM authoritative, UIA authoritative, OCR secondary
 ├── Recovery if necessary ─── never restart
 ├── Submit → Audit/Excel
 └── Learning Memory → next record (form fingerprint reuse)
```
