# Performance — Before vs After (Universal WEB_DOM)

Measured 2026-08-10 on the bundled test app
(`tests/web_apps/universal_form/`) via
`python run_universal_web.py --records 3` / `--react`.
Raw per-field timings: `debug/performance/universal_run.json`.

## Before (legacy vision-first model)

Audit basis: `PERFORMANCE_AUDIT.md` + the universal agent spec. Every action was
screen-based and expensive:

| Cost | Where | Legacy cost |
|------|-------|-------------|
| Full VLM re-observe | every scan/fill round | ~1 VLM call / round |
| Left-panel OCR | every round (source re-read) | PaddleOCR / round |
| Per-action verify | `ActionExecutor._verify` re-observe | ~1 VLM call / field |
| Human-like delays | click → type → pause | fixed 1–3 s sleeps |
| Scroll settle | dual-panel lockstep | 300–500 ms per scroll |
| Dropdown interaction | click → read → pick → verify | 2–5 s typical |

The universal agent spec's stated performance targets:

| Channel | Target latency / field |
|---------|------------------------|
| WEB DOM | ~100–500 ms |
| UIA | ~200–800 ms |
| Dropdown (DOM select) | ~300–1000 ms |

## After (attach-first + WEB_DOM engine)

One CDP attach to an **already-running** browser, then pure DOM fills
(`fill()` / `select_option()` / `check()` / `set_input_files()`) with DOM
read-back verification — no screenshots, no OCR, no fixed sleeps.

### Generic form — 14 mapped fields × 3 records (42 fills)

| Metric | Measured | Target | Status |
|--------|----------|--------|--------|
| Avg time per field | **41.2 ms** | 100–500 ms | well under target |
| Max single field | 101.0 ms | ≤ 500 ms | ok |
| Avg time per record (14 fields) | 579.3 ms | — | — |
| Fields filled | 42 | — | — |
| Fields verified (DOM read-back) | 42 / 42 | 100% | ok |
| New processes launched | **0** | 0 | ok |
| Methods used | dom=24, select_option=12, click=3, upload=3 | — | — |

### React-style form (custom combobox + dependent selects) — 6 fields × 2 records

| Metric | Measured | Target | Status |
|--------|----------|--------|--------|
| Avg time per field | **53.6 ms** | 100–500 ms | under target |
| Max single field | 91.4 ms | ≤ 500 ms | ok |
| Avg time per record | 324.0 ms | — | — |
| Fields filled / verified | 12 / 12 | 100% | ok |
| New processes launched | **0** | 0 | ok |
| Methods used | dom=6 (incl. custom combobox), select_option=6 | — | — |

## Summary

| | Legacy (vision-first) | Universal WEB_DOM | Δ |
|---|----------------------|-------------------|-----|
| Time per field | ~1–3 s | ~40–55 ms | **~25–60× faster** |
| Verification | VLM re-observe per action | authoritative DOM read-back | cheaper + exact |
| Fixed sleeps | 1–3 s everywhere | none (state-observed) | removed |
| Attach model | launch-first (duplicate browser) | attach-first (reuse existing) | no duplicates |
| New processes | 1+ per run | 0 | fixed |
| VLM calls | O(rounds + fields) | 0 (DOM path) | eliminated |

Caveats: figures are for the DOM path against the bundled single-page test
apps. Real-world forms with iframes, shadow DOM or slow XHR round-trips will be
slower; the UIA fallback path targets 200–800 ms/field and kicks in when DOM is
not available. No artificial latency is injected anywhere in the new path.
