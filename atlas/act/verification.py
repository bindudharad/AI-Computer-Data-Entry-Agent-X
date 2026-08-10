"""Structured verification results and a status-aware verification engine.

The executor no longer treats verification as binary. Every read produces a
``VerificationResult`` carrying an explicit status:

- ``MATCH``          - evidence confirms the field holds the expected value.
- ``MISMATCH``       - evidence shows a *different* readable value.
- ``UNKNOWN``        - no usable read could be obtained (empty reads, read
                       failures, whole-window clipboard grabs, no verifier).
                       This is NOT a mismatch: retrying the action would just
                       repeat the write on a field whose value may be correct.
- ``NOT_APPLICABLE`` - the field/value does not require verification.
- ``PENDING``        - verification not yet attempted.

UNKNOWN is the core fix for the MPF regression: an empty UIA read or a
whole-window clipboard grab used to collapse into plain ``False`` and trigger
a full action-retry ladder (70-80s per field). Now the executor classifies it
as ``ACTION_SUCCESS_VERIFICATION_UNKNOWN`` and moves on.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from atlas.act.verify import (
    CompositeVerifier,
    FieldVerifier,
    normalize_for_compare,
)
from atlas.core.logging import verification_logger
from atlas.vision.models import BBox


class VerificationStatus(str, Enum):
    """Verification outcome states (never binary)."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"


#: Evidence phrases that describe an *inability to read* the field, not a
#: value that differs from the expectation. Any evidence containing one of
#: these classifies as UNKNOWN.
_UNKNOWN_EVIDENCE_PATTERNS = (
    "read failed",
    "read empty",
    "no value available",
    "no bbox",
    "no uia",
    "no verifier",
    "clipboard read-back is whole-window",
    "not field-scoped",
    "nothing to verify",
    "no textual value",
)

#: Evidence phrases that describe an explicit placeholder read-back (a combo
#: still showing "Select"). A readable placeholder is reliable evidence that
#: the expected value is NOT present -> MISMATCH (corrective retry warranted).
_MISMATCH_EVIDENCE_PATTERNS = (
    "mismatch: got",
    "placeholder",
)


def classify_evidence(evidence: str) -> VerificationStatus:
    """Classify raw verifier evidence into a structured status.

    Used to lift the legacy ``(bool, evidence)`` verifier output into the
    status-aware model without rewriting every strategy.
    """
    if not evidence:
        return VerificationStatus.UNKNOWN
    lowered = evidence.lower()
    if any(pattern in lowered for pattern in _MISMATCH_EVIDENCE_PATTERNS):
        return VerificationStatus.MISMATCH
    if any(pattern in lowered for pattern in _UNKNOWN_EVIDENCE_PATTERNS):
        return VerificationStatus.UNKNOWN
    return VerificationStatus.MISMATCH


@dataclass
class VerificationResult:
    """Structured outcome of verifying one field after an action."""

    status: VerificationStatus
    field_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    method: str = ""
    evidence: str = ""
    confidence: float = 0.0
    attempts: int = 1

    @property
    def is_match(self) -> bool:
        return self.status == VerificationStatus.MATCH

    @property
    def is_unknown(self) -> bool:
        return self.status == VerificationStatus.UNKNOWN

    @property
    def is_mismatch(self) -> bool:
        return self.status == VerificationStatus.MISMATCH

    def to_dict(self) -> dict:
        return {
            "field_id": self.field_id,
            "status": self.status.value,
            "expected": self.expected,
            "observed": self.observed,
            "method": self.method,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "attempts": self.attempts,
        }

    def __str__(self) -> str:
        return (
            f"{self.status.value}"
            f"{f' ({self.method})' if self.method else ''}"
            f"{f' observed={self.observed!r}' if self.observed is not None else ''}"
        )


def _observed_from_evidence(evidence: str) -> str:
    """Best-effort extraction of the observed value embedded in evidence."""
    if not evidence:
        return ""
    match = re.search(
        r"(?:matched|contains|read|observed)[^\n]*?['\"]([^'\"]{1,120})['\"]",
        evidence,
        re.I,
    )
    if match:
        return match.group(1)
    match = re.search(r"'([^']{1,120})'", evidence)
    return match.group(1) if match else evidence.strip()[:120]


class VerificationEngine:
    """Runs verification strategies and returns structured results.

    Behaviour rules:
    - strategies run in priority order; the first MATCH wins immediately.
    - if no strategy matched, a *readable* MISMATCH outranks UNKNOWN (a
      different value is harder evidence than a failed read).
    - UNKNOWN results never trigger action retries (see ``ActionExecutor``).
    - when ``fallback_regions`` is given, a UNKNOWN outcome may re-read the
      field's expanded region before giving up (transient paint / moved bbox).
    """

    def __init__(
        self,
        verifier: CompositeVerifier | FieldVerifier,
    ) -> None:
        if isinstance(verifier, CompositeVerifier):
            self._verifier = verifier
        else:
            self._verifier = CompositeVerifier([verifier])

    @property
    def strategies(self) -> list[str]:
        return self._verifier.strategies

    def verify(
        self,
        bbox: BBox | None,
        expected: str,
        field_id: str | None = None,
    ) -> VerificationResult:
        """Verify a field and return a structured result."""
        if expected is None:
            return VerificationResult(
                status=VerificationStatus.NOT_APPLICABLE,
                field_id=field_id,
                expected=expected,
                method="none",
                evidence="no expected value to verify",
            )
        ok, evidence = self._verifier.verify(bbox, expected, field_id)
        return self._result(ok, evidence, field_id, expected, method="composite")

    # -- read-recovery ladder (read-only; never re-runs the action) ----------

    def verify_with_read_recovery(
        self,
        bbox: BBox | None,
        expected: str,
        field_id: str | None = None,
        max_attempts: int = 2,
        refocus: Callable[[BBox], None] | None = None,
    ) -> VerificationResult:
        """Re-read a UNKNOWN field without touching its value.

        Levels (read-only, in order):
        1. plain re-read (transient repaint / recapture),
        2. refocus + re-read (a stale bbox that pointed at blank space),
        3. expanded-region OCR re-read (bbox offset by a small scroll).

        Never repeats the action - used only to rescue reads, never writes.
        """
        result = self.verify(bbox, expected, field_id)
        if not result.is_unknown or max_attempts <= 1:
            return result
        for _level in range(1, max_attempts + 1):
            # Level 2: focus the field, wait for paint, re-read.
            if refocus is not None and bbox is not None:
                try:
                    refocus(bbox)
                except Exception as exc:
                    verification_logger.debug(
                        "verify recovery refocus failed for {}: {}", field_id, exc
                    )
                time.sleep(0.15)
            result = self.verify(bbox, expected, field_id)
            if not result.is_unknown:
                break
        return result

    # -- internals -----------------------------------------------------------

    def _result(
        self,
        ok: bool,
        evidence: str,
        field_id: str | None,
        expected: str | None,
        method: str = "",
        attempts: int = 1,
    ) -> VerificationResult:
        if ok:
            return VerificationResult(
                status=VerificationStatus.MATCH,
                field_id=field_id,
                expected=expected,
                observed=_observed_from_evidence(evidence) or expected,
                method=method,
                evidence=evidence,
                confidence=1.0,
                attempts=attempts,
            )
        status = classify_evidence(evidence)
        return VerificationResult(
            status=status,
            field_id=field_id,
            expected=expected,
            observed=_observed_from_evidence(evidence),
            method=method,
            evidence=evidence,
            attempts=attempts,
        )


__all__ = [
    "VerificationStatus",
    "VerificationResult",
    "VerificationEngine",
    "classify_evidence",
    "normalize_for_compare",
]
