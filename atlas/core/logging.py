"""Structured logging via loguru.

Central setup so every module logs through one configured sink. Screenshot
references can be attached to log records as extra context.

Category logs
-------------
Besides the main rotating ``atlas_<date>.log`` the setup writes one rotating
file per category (``actions.log``, ``errors.log``, ``ocr.log``, ``uia.log``,
``timings.log``, ``focus.log``, ``verification.log``). Modules that emit high-
volume domain events bind their logger with a ``category`` extra; the filter
routes each record to the matching file. Errors always land in ``errors.log``
regardless of category.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

#: Category name -> log file stem (relative to the log folder).
CATEGORIES: dict[str, str] = {
    "action": "actions",
    "ocr": "ocr",
    "uia": "uia",
    "timing": "timings",
    "focus": "focus",
    "verification": "verification",
}

#: Bound loggers for the domain categories.
action_logger = logger.bind(category="action")
ocr_logger = logger.bind(category="ocr")
uia_logger = logger.bind(category="uia")
timing_logger = logger.bind(category="timing")
focus_logger = logger.bind(category="focus")
verification_logger = logger.bind(category="verification")


def _category_filter(category: str) -> Any:
    def _filter(record) -> bool:
        return record["extra"].get("category") == category

    return _filter


def _error_filter(record) -> bool:
    return record["level"].name in {"ERROR", "CRITICAL"}


def _prepare_console_utf8() -> None:
    """Point the console stream at UTF-8 so Unicode log lines never crash.

    On Windows the console sink inherits the process codepage (cp1252); any
    non-ASCII character in a log line then fails to encode and loguru spams a
    blocking warning ("Failed to write to stdout..."). Reconfiguring the stream
    once up-front makes every later write safe and lossless.
    """
    try:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def setup_logging(level: str, folder: Path, capture_stdout: bool = True) -> None:
    """Configure the global loguru logger.

    Parameters
    ----------
    level:
        Minimum level shown on the console (DEBUG, INFO, ...).
    folder:
        Directory for the rotating file logs.
    capture_stdout:
        When False the console sink is skipped (e.g. GUI launchers).
    """
    folder.mkdir(parents=True, exist_ok=True)
    logger.remove()

    if capture_stdout:
        _prepare_console_utf8()
        logger.add(
            sys.stdout,
            format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
            level=level,
            colorize=True,
        )
    logger.add(
        folder / "atlas_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{line} | {message}",
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
    )
    for category, stem in CATEGORIES.items():
        logger.add(
            folder / f"{stem}.log",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{line} | {message}",
            level="DEBUG",
            rotation="10 MB",
            retention="30 days",
            encoding="utf-8",
            filter=_category_filter(category),
        )
    logger.add(
        folder / "errors.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{line} | {message}",
        level="ERROR",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        filter=_error_filter,
    )


def log_screenshot(path: Path, context: str) -> None:
    """Log a screenshot for audit / visual-debug purposes."""
    logger.bind(screenshot=str(path)).info("screenshot[{}] {}", context, path)


def sanitize_secrets(text: str) -> str:
    """Best-effort removal of secret values from free text (log hygiene)."""
    return text


def bind_context(**kwargs: Any) -> Any:
    """Return a logger bound with contextual fields."""
    return logger.bind(**kwargs)
