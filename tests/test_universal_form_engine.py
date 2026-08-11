"""End-to-end tests for the WEB_DOM :class:`WebFormEngine`.

Uses the real universal-form web app served over HTTP (tests/web_apps/universal_form)
and real Playwright chromium in headless mode. Exercises discovery, semantic
mapping, DOM-first filling, dependent selects, dynamic fields, custom comboboxes,
radios, file uploads, verification read-back and submit.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
from pathlib import Path

import pytest

from atlas.understanding.source import SourceRecord
from atlas.universal.learning import MethodLearner
from atlas.universal.smart_wait import SmartWait
from atlas.vision.models import ElementType
from atlas.web.form_engine import WebFieldAction, WebFormEngine

try:
    import playwright  # noqa: F401

    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

_WEB_APP = Path(__file__).parent / "web_apps" / "universal_form"


def _load_server():
    spec = importlib.util.spec_from_file_location("univ_form_server", _WEB_APP / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server() -> str:
    server_mod = _load_server()
    port = _free_port()
    httpd = server_mod.ThreadingHTTPServer(("127.0.0.1", port), server_mod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def browser(server: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


def _engine_for(browser, url: str) -> WebFormEngine:
    page = browser.new_page()
    page.goto(url)
    page.wait_for_load_state("load")
    return WebFormEngine(page=page, learner=MethodLearner(enabled=True), wait=SmartWait(default_timeout=5.0))


def _generic_record() -> SourceRecord:
    pairs = {
        "Full Name": "Ravi Kumar",
        "Email Address": "ravi.kumar@example.com",
        "Phone Number": "9876543210",
        "Age": "34",
        "Date of Birth": "1990-05-15",
        "Gender": "Male",
        "Country": "India",
        "State": "Maharashtra",
        "City": "Mumbai",
        "Declaration": "Yes",
        "Extra Details": "prefers email",
        "Address": "12 MG Road",
        "Remarks": "fast delivery",
        "Attachment": "resume.txt",
    }
    return SourceRecord(pairs=pairs, ordered_labels=list(pairs), title="universal form")


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_discover_generic_form(browser, server: str) -> None:
    engine = _engine_for(browser, f"{server}/")
    form = engine.discover()
    labels = {f.label for f in form.fields}
    for expected in ("Full Name", "Email Address", "Phone Number", "Age", "Date of Birth",
                     "Gender", "Country", "State", "City", "Declaration", "Address", "Remarks", "Attachment"):
        assert expected in labels, f"missing field {expected!r} in {sorted(labels)}"
    assert form.submit_selector is not None
    assert form.field_count >= 13
    assert engine.form_unchanged()
    engine._page.close()


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_fill_record_full_verification(browser, server: str, tmp_path) -> None:
    engine = _engine_for(browser, f"{server}/")
    engine.discover()
    result = engine.fill_record(_generic_record(), upload_dir=str(tmp_path))
    assert result.ok, result.failed
    assert result.filled >= 13
    assert result.verified >= 13
    assert result.avg_field_ms < 5000, f"avg {result.avg_field_ms}ms too slow"
    page = engine._page
    assert page.locator("#f-name").input_value() == "Ravi Kumar"
    assert page.locator("#f-email").input_value() == "ravi.kumar@example.com"
    assert page.locator("#f-gender").input_value() == "M"
    assert page.locator("#f-country").input_value() == "IN"
    assert page.locator("#f-state").input_value() == "Maharashtra"
    assert page.locator("#f-city").input_value() == "Mumbai"
    assert page.locator("#f-decl").is_checked()
    assert page.locator("#f-address").input_value() == "12 MG Road"
    page.close()


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_dynamic_field_revealed_by_checkbox(browser, server: str) -> None:
    engine = _engine_for(browser, f"{server}/")
    form = engine.discover()
    extra = next(f for f in form.fields if f.element_id and f.id == "f-extra")
    assert not extra.visible
    page = engine._page
    page.locator("#f-decl").check()
    assert page.locator("#extra-row").is_visible()
    engine.refresh()
    extra_after = next(f for f in engine._form.fields if f.id == "f-extra")
    assert extra_after.visible
    page.close()


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_radio_group_check_and_verify(browser, server: str) -> None:
    engine = _engine_for(browser, f"{server}/")
    form = engine.discover()
    radio = next(f for f in form.fields if f.element_type == ElementType.RADIO)
    action = WebFieldAction(source_label="Preferred Contact", source_value="Phone", field=radio, confidence=1.0)
    method = engine._fill_field(action, upload_dir=None, source_label="web")
    assert method == "radio"
    read = engine._read_value(radio)
    assert engine._value_matches(action, read)
    engine._page.close()


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_custom_combobox_and_react_page(browser, server: str, tmp_path) -> None:
    engine = _engine_for(browser, f"{server}/react_page.html")
    form = engine.discover()
    labels = {f.label for f in form.fields}
    assert "Department" in labels
    assert "Employee Name" in labels
    record = SourceRecord(
        pairs={
            "Employee Name": "Anita Desai",
            "Department": "Finance",
            "Country": "India",
            "State": "Karnataka",
            "City": "Bengaluru",
            "Remarks": "via react page",
        },
        ordered_labels=["Employee Name", "Department", "Country", "State", "City", "Remarks"],
        title="universal form react",
    )
    result = engine.fill_record(record, upload_dir=str(tmp_path))
    assert result.ok, result.failed
    page = engine._page
    assert page.locator("#f-dept").input_value() == "Finance"
    assert page.locator("#f-country").input_value() == "IN"
    assert page.locator("#f-state").input_value() == "Karnataka"
    assert page.locator("#f-city").input_value() == "Bengaluru"
    page.close()


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_submit_button(browser, server: str) -> None:
    engine = _engine_for(browser, f"{server}/")
    engine.discover()
    engine._page.locator("#f-name").fill("Ravi Kumar")
    assert engine.submit()
    assert engine._page.locator("#f-submit").inner_text() == "Submitted"
    engine._page.close()


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not installed")
def test_learner_drives_method_selection(browser, server: str) -> None:
    engine = _engine_for(browser, f"{server}/")
    form = engine.discover()
    name_field = next(f for f in form.fields if f.id == "f-name")
    action = WebFieldAction(source_label="Full Name", source_value="A", field=name_field, confidence=1.0)
    engine._learner.record(application="web", field="full name", method="dom", ok=True, elapsed_ms=12.0)
    engine._learner.record(application="web", field="full name", method="dom", ok=True, elapsed_ms=15.0)
    assert engine._learner.preferred("web", "full name") == "dom"
    method = engine._fill_field(action, upload_dir=None, source_label="web")
    assert method == "dom"
    assert engine._page.locator("#f-name").input_value() == "A"
    engine._page.close()
