#!/usr/bin/env python3
"""Universal web automation benchmark runner (attach-first + WEB_DOM).

Demonstrates the universal agent's fast web path end-to-end:

    1. A Chromium browser is launched ONCE with a CDP port (this stands in for
       the user's already-running browser - the agent itself never launches it).
    2. The agent ATTACHES to that existing browser via ``WebTarget.attach_existing``
       (never a second process, never a new tab).
    3. ``WebFormEngine`` discovers, maps and fills records through the DOM
       (fill()/select_option()/check() - no clicks, no OCR, no human delays),
       verifying each field by authoritative DOM read-back.
    4. Every field is timed; per-field averages are written to
       ``debug/performance/universal_run.json`` alongside the WEB_DOM targets.

Usage:
    python run_universal_web.py [--records N] [--react] [--cdp-port P] [--json]

Examples:
    python run_universal_web.py --records 3
    python run_universal_web.py --records 3 --react
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from atlas.core.logging import logger, setup_logging
from atlas.target.web import ExistingTabNotFound, WebTarget
from atlas.understanding.source import SourceRecord
from atlas.universal.learning import MethodLearner
from atlas.universal.smart_wait import SmartWait
from atlas.vision.providers import MockVisionProvider
from atlas.vision.scene import SceneAnalyzer
from atlas.web.form_engine import WebFormEngine

ROOT = Path(__file__).resolve().parent
WEB_APP = ROOT / "tests" / "web_apps" / "universal_form"

#: WEB_DOM performance targets (from the universal agent spec).
TARGETS = {
    "web_dom_field_ms": {"min": 100, "max": 500},
    "web_dom_avg_ms": {"min": 100, "max": 500},
}


def _load_server():
    spec = importlib.util.spec_from_file_location("univ_form_server", WEB_APP / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _generic_record(i: int) -> SourceRecord:
    gender = ["Male", "Female", "Other"][i % 3]
    state = ["Maharashtra", "Karnataka", "Tamil Nadu"][i % 3]
    city = {"Maharashtra": "Mumbai", "Karnataka": "Bengaluru", "Tamil Nadu": "Chennai"}[state]
    pairs = {
        "Full Name": f"Person {i + 1}",
        "Email Address": f"person{i + 1}@example.com",
        "Phone Number": f"98765{i + 1:05d}",
        "Age": str(21 + i),
        "Date of Birth": f"199{1 + (i % 9)}-0{1 + (i % 9)}-15",
        "Gender": gender,
        "Country": "India",
        "State": state,
        "City": city,
        "Declaration": "Yes",
        "Extra Details": f"note {i + 1}",
        "Address": f"{i + 1} MG Road",
        "Remarks": "universal web run",
        "Attachment": f"resume{i + 1}.txt",
    }
    return SourceRecord(pairs=pairs, ordered_labels=list(pairs), title="universal form")


def _react_record(i: int) -> SourceRecord:
    state = ["Karnataka", "Maharashtra", "Delhi"][i % 3]
    city = {"Karnataka": "Bengaluru", "Maharashtra": "Mumbai", "Delhi": "New Delhi"}[state]
    pairs = {
        "Employee Name": f"Emp {i + 1}",
        "Department": ["Engineering", "Finance", "Support"][i % 3],
        "Country": "India",
        "State": state,
        "City": city,
        "Remarks": "react universal run",
    }
    return SourceRecord(pairs=pairs, ordered_labels=list(pairs), title="universal form react")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=int, default=3, help="records to fill (default 3)")
    parser.add_argument("--react", action="store_true", help="run against the react-style page")
    parser.add_argument("--cdp-port", type=int, default=0, help="CDP port for the existing browser (0 = auto)")
    parser.add_argument("--json", action="store_true", help="also dump the full JSON to stdout")
    parser.add_argument("--out", default="debug/performance", help="output directory")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level, ROOT / "logs")
    server_mod = _load_server()
    app_port = _free_port()
    cdp_port = args.cdp_port or _free_port()
    base_url = f"http://127.0.0.1:{app_port}"
    page_path = "/react_page.html" if args.react else "/"
    target_url = f"{base_url}{page_path}"
    httpd = server_mod.ThreadingHTTPServer(("127.0.0.1", app_port), server_mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    logger.info("[RUN] universal form server on {}", base_url)

    # 1) The "user's" browser: a SEPARATE OS process launched once (by the
    #    user, not the agent) with a CDP port so the agent can attach to it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as _pw:
        chrome_exe = _pw.chromium.executable_path
    profile_dir = tempfile.mkdtemp(prefix="atlas_univ_")
    chrome_proc = subprocess.Popen(
        [chrome_exe,
         "--headless=new",
         f"--remote-debugging-port={cdp_port}",
         f"--user-data-dir={profile_dir}",
         target_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    target = None
    try:
        import urllib.request

        endpoint = f"http://127.0.0.1:{cdp_port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{endpoint}/json/list", timeout=1.5) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.4)
        else:
            print("ERROR: existing browser did not expose its CDP endpoint", file=sys.stderr)
            return 1
        logger.info("[RUN] existing browser process open on {} (cdp port {})", target_url, cdp_port)

        # 2) ATTACH to the existing browser - the agent never launches it.
        target = WebTarget(analyzer=SceneAnalyzer(MockVisionProvider()),
                           browser_type="chromium", headless=True)
        try:
            target.attach_existing(endpoint, url=target_url)
        except ExistingTabNotFound as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        engine = WebFormEngine(
            page=target.page,
            learner=MethodLearner(enabled=True),
            wait=SmartWait(default_timeout=5.0),
        )
        form = engine.discover()
        logger.info("[RUN] discovered {} DOM fields on {}", form.field_count, form.url)

        # 3) Fill records, timing every field.
        record_factory = _react_record if args.react else _generic_record
        records_out: list[dict] = []
        method_counts: dict[str, int] = {}
        all_timings: list[dict] = []
        total_fill_ms = 0.0
        verified_total = 0
        filled_total = 0

        for i in range(args.records):
            t0 = time.perf_counter()
            result = engine.fill_record(record_factory(i), upload_dir=str(ROOT / "debug" / "uploads"))
            elapsed_ms = (time.perf_counter() - t0) * 1000
            total_fill_ms += elapsed_ms
            filled_total += result.filled
            verified_total += result.verified
            for timing in result.timings:
                method_counts[timing.method] = method_counts.get(timing.method, 0) + 1
                all_timings.append(timing.to_dict())
            records_out.append({
                "record_index": i + 1,
                "fields_filled": result.filled,
                "fields_verified": result.verified,
                "failed": list(result.failed),
                "ok": result.ok,
                "record_ms": round(elapsed_ms, 1),
                "avg_field_ms": result.avg_field_ms,
            })
            logger.info("[RUN] record {}: {} fields, {} verified in {:.0f}ms (ok={})",
                        i + 1, result.filled, result.verified, elapsed_ms, result.ok)
            target.page.reload(wait_until="load")

        # 4) Aggregate and write debug/performance/universal_run.json.
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        field_ms = [t["total_ms"] for t in all_timings if t["total_ms"] > 0]
        avg_field_ms = round(sum(field_ms) / len(field_ms), 1) if field_ms else 0.0
        avg_record_ms = round(total_fill_ms / max(1, args.records), 1)

        report = {
            "app": "tests/web_apps/universal_form" + page_path,
            "mode": "attach-existing + WEB_DOM",
            "launch_count": 0,
            "attach_count": 1,
            "records": len(records_out),
            "per_record": records_out,
            "totals": {
                "fields_filled": filled_total,
                "fields_verified": verified_total,
                "method_counts": method_counts,
                "avg_field_ms": avg_field_ms,
                "avg_record_ms": avg_record_ms,
                "max_field_ms": round(max(field_ms), 1) if field_ms else 0.0,
            },
            "targets": TARGETS,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_file = out_dir / "universal_run.json"
        out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        target.detach()

        print("-" * 64)
        print("UNIVERSAL WEB RUN (attach-existing + WEB_DOM)")
        print(f"  page:           {page_path or '/'}  ({form.field_count} fields)")
        print(f"  records:        {len(records_out)}")
        print(f"  fields filled:  {filled_total}   verified: {verified_total}")
        print(f"  launch count:   0 (attached to existing browser, no relaunch)")
        print(f"  avg field:      {avg_field_ms:.1f} ms   (target 100-500 ms)")
        print(f"  avg record:     {avg_record_ms:.1f} ms")
        print(f"  methods:        {method_counts}")
        print(f"  saved:          {out_file}")
        print("-" * 64)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    finally:
        if target is not None:
            try:
                target.detach()
            except Exception:
                pass
        if chrome_proc is not None and chrome_proc.poll() is None:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except Exception:
                chrome_proc.kill()
        import shutil

        shutil.rmtree(profile_dir, ignore_errors=True)
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
