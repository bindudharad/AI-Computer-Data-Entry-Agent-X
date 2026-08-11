# Universal Agent — Automation Report

Status: **IMPLEMENTED + TESTED** (2026-08-10)

This report documents the universal, attach-first upgrade of the ATLAS AI
agent: it can automate ANY window-based or browser-based data-entry app, uses an
existing browser/target, and never spawns a duplicate process.

## 1. Attach-first loop (the core fix)

The old `WebTarget.attach()` was **launch-first**: it always started a brand-new
browser and opened a new tab, so a user who already had the site open got a
duplicate window and a duplicated workflow. That is replaced by:

```
DISCOVER -> CLASSIFY -> ATTACH -> VERIFY -> AUTOMATE
```

| Case | Condition | Action | Launch? |
|------|-----------|--------|---------|
| A | Existing browser window/tab for the target | `ATTACH_EXISTING` (CDP / DOM) | **never** |
| B | Browser present, target tab exists (CDP) | `ATTACH_EXISTING` | **never** |
| C/D | Target tab exists but not active / on another tab | tab inspection → `ATTACH_EXISTING` | **never** |
| E | Browser process alive but CDP unavailable | `BROWSER_UIA` (disconnected ≠ missing) | **never** |
| G | Existing desktop window | `ATTACH_EXISTING` (Win32/UIA) | **never** |
| H | Electron / Chromium desktop app | `ATTACH_EXISTING` | **never** |
| F | Nothing exists anywhere | `LAUNCH` only if `AUTO_LAUNCH_TARGET=true`, else `WAIT` | **policy-gated** |

- `launch` is only ever `True` for case F, and only when the restart policy
  allows it (`RestartMode.AUTO` needs `AUTO_LAUNCH_TARGET=true` +
  `ALLOW_BROWSER_LAUNCH=true`; the default is `ON_CRASH_ONLY`).
- A missing CDP connection, a missing tab, or a failed attach is treated as
  **disconnected**, never as "target missing" — so it can never relaunch.
- `WebTarget.attach_existing()` connects via CDP and reuses an open page; it
  never opens a window/tab and `detach()` only disconnects (never closes the
  user's browser). Raises `ExistingTabNotFound` otherwise.

## 2. Target detection & classification

- `atlas/universal/detector.py` — enumerates windows, browser processes and CDP
  tabs; ranks candidates by score/confidence (foreground, title/URL/app hints,
  expected form, browser compatibility, UIA/DOM availability).
- `atlas/universal/classifier.py` — classifies a candidate into
  `CHROME_BROWSER / EDGE_BROWSER / FIREFOX_BROWSER / WEB_BROWSER / DESKTOP_UIA /
  ELECTRON` + capabilities (`CDP / DOM / UIA / VISION`). `Chrome_WidgetWin_1`
  + non-browser exe → Electron; empty/unknown exe → Chrome browser.
- `atlas/web/tabs.py` — enumerates page tabs over the configured `CDP_PORTS`.
- `atlas/web/browser_discovery.py` — maps a CDP port to its browser process.

## 3. WEB_DOM engine (`atlas/web/form_engine.py`)

The fast web path. On a connected page it:

1. **discovers** every DOM control into `WebFieldDescriptor`s (label from
   `<label>`/`aria-label`/`placeholder`/`name`, type, options, required,
   visible, bbox, stable fingerprint),
2. **maps** a source record onto them via the existing `SemanticMapper`
   (semantic, not CSS),
3. **fills** with the fastest reliable method — `fill()` for text/date/textarea,
   `select_option(label)` for `<select>`, `check()/uncheck()` for checkboxes,
   option-matching for radios, `set_input_files()` for uploads,
4. **verifies** with the authoritative DOM read-back (values are read back from
   the DOM, never guessed),
5. **times** every phase per field and **learns** the best method per
   (application, field) via `MethodLearner`,
6. **submits** by locating the real submit/save button.

Supported field kinds covered by the test app (`tests/web_apps/universal_form/`):
text, email, phone, number, date, dependent Country→State→City selects, custom
React-style combobox, checkbox with a revealed **dynamic field**, radio group,
textarea in a scrollable section, file upload, and submit.

## 4. Restart & resilience policy

- `atlas/universal/restart_policy.py` — `NEVER / ON_CRASH_ONLY / AUTO /
  ON_USER_REQUEST`. Default `ON_CRASH_ONLY`. Only a real crash may relaunch
  (`crash_detected=true`); a disconnect never does.
- `atlas/universal/smart_wait.py` — state-observed waits (DOM change, visibility)
  instead of fixed sleeps.
- `atlas/universal/learning.py` — per-(application, field) method stats;
  a method becomes "best" only after ≥2 attempts with ≥60% success, then it is
  preferred (fastest first). `record()` is a no-op when disabled.
- `atlas/universal/performance.py` — enforces the invariant
  `EXISTING_* ⇒ launch_count == 0`.

## 5. Configuration (`atlas/config.py`, `UniversalConfig`)

| Key | Default | Meaning |
|-----|---------|---------|
| `TARGET_MODE` | `auto` | attach-first strategy |
| `AUTO_LAUNCH_TARGET` | `false` | allow a launch when nothing exists |
| `PREFER_EXISTING_TARGET` | `true` | never relaunch an existing target |
| `ALLOW_BROWSER_LAUNCH` | `false` | browser relaunch requires explicit opt-in |
| `STRICT_FOCUS_GUARD` | `true` | pause/stop when focus is lost |
| `SMART_WAIT` | `true` | state-observed waits |
| `LEARN_METHODS` | `true` | per-field method learning |
| `OCR_FALLBACK` | `true` | OCR as last resort |
| `CDP_PORTS` | `9222,9229,9230` | ports probed for existing tabs |

## 6. Test coverage

| File | What it proves |
|------|----------------|
| `tests/test_universal_detector.py` | window/process/tab discovery, ranking |
| `tests/test_universal_classifier.py` | env + capability classification |
| `tests/test_universal_attach.py` | decision cases A–H |
| `tests/test_no_unnecessary_launch.py` | **mandatory regression**: a launch is only ever produced when nothing exists — attach failures, missing CDP, disconnects and desktop/Electron targets never relaunch |
| `tests/test_universal_restart_policy.py` | policy gates every launch |
| `tests/test_universal_smart_wait.py` | state-observed waits |
| `tests/test_universal_learning.py` | per-method best-method learning |
| `tests/test_universal_performance.py` | `EXISTING ⇒ launch_count == 0` |
| `tests/test_universal_form_engine.py` | real browser WEB_DOM: discover, map, fill, dependent selects, dynamic field, radio, custom combobox, submit, learning |
| `tests/test_web.py` / `test_web_*.py` | existing web target + tabs/fields/CDP |

## 7. How to run

```powershell
# Universal attach-first on the MPF desktop target (never relaunches it)
python run_mpf_test.py --records 3 --field-driven --auto

# Universal attach-first via the main CLI
python main.py run --mode auto --title "MPF" --max-records 3
python main.py run --auto --url "http://localhost:5173" --max-records 3

# WEB_DOM benchmark against the bundled test app (attach-existing + timing)
python run_universal_web.py --records 3
python run_universal_web.py --records 3 --react
# writes debug/performance/universal_run.json

# Tests
python -m pytest tests/test_no_unnecessary_launch.py tests/test_universal_form_engine.py -q
python -m pytest -q
```

## 8. Delivered artifacts

- `debug/performance/universal_run.json` — per-record and per-field timing from
  the WEB_DOM run (attach-existing, zero launches).
- `tests/web_apps/universal_form/` — the generic + React-style demo apps
  (`index.html`, `react_page.html`, `server.py`).
- `PERFORMANCE_BEFORE_AFTER.md` — before/after comparison.
