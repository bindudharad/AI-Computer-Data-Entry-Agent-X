"""atlas.web - browser discovery, CDP, tab enumeration and form field helpers.

Pure, injectable primitives for the universal attach flow. None of these
modules import Playwright eagerly; browser interaction happens in
``atlas.target.web``.
"""

from atlas.web.browser_discovery import BrowserDiscovery, BrowserProcess, parse_browsers
from atlas.web.cdp import cdp_available, cdp_endpoint, cdp_version
from atlas.web.tabs import discover_tabs
from atlas.web.fields import build_locator, field_fingerprint_js, normalize_label, rank_methods_for

__all__ = [
    "BrowserDiscovery",
    "BrowserProcess",
    "parse_browsers",
    "cdp_available",
    "cdp_endpoint",
    "cdp_version",
    "discover_tabs",
    "build_locator",
    "field_fingerprint_js",
    "normalize_label",
    "rank_methods_for",
]
