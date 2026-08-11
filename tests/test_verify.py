"""Tests for the verification strategies, especially file-upload read-back."""

from __future__ import annotations

from atlas.act.verify import (
    ClipboardVerifier,
    TargetFieldVerifier,
    UiaValueVerifier,
    VisionVerifier,
    _contains_token,
    _file_match,
    date_tokens,
    dates_match,
    is_placeholder,
    looks_like_whole_window,
)
from atlas.vision.models import BBox


def test_file_match_basename() -> None:
    assert _file_match(r"C:\fakepath\x.pdf", "C:/docs/x.pdf")
    assert _file_match(r"C:\fakepath\x.pdf", r"C:\docs\x.pdf")
    assert _file_match("x.pdf", "C:/docs/x.pdf")


def test_file_match_path_containment() -> None:
    assert _file_match("C:/docs/x.pdf", "docs/x.pdf")


def test_file_match_different_file() -> None:
    assert not _file_match(r"C:\fakepath\y.pdf", "C:/docs/x.pdf")
    assert not _file_match("", "C:/docs/x.pdf")
    assert not _file_match("x.pdf", "")


def test_file_match_case_insensitive() -> None:
    assert _file_match(r"C:\FakePath\X.PDF", "c:/docs/x.pdf")


def test_target_verifier_accepts_fakepath() -> None:
    verifier = TargetFieldVerifier(lambda _: r"C:\fakepath\resume.pdf")
    ok, _ = verifier.verify(None, "C:/docs/resume.pdf", "f0")
    assert ok is True


def test_target_verifier_rejects_wrong_file() -> None:
    verifier = TargetFieldVerifier(lambda _: r"C:\fakepath\other.pdf")
    ok, evidence = verifier.verify(None, "C:/docs/resume.pdf", "f0")
    assert ok is False
    assert "mismatch" in evidence


def test_target_verifier_rejects_placeholder_select() -> None:
    verifier = TargetFieldVerifier(lambda _: "Select")
    ok, evidence = verifier.verify(None, "Male", "f0")
    assert ok is False
    assert "placeholder" in evidence


def test_target_verifier_accepts_real_value_over_placeholder_shape() -> None:
    verifier = TargetFieldVerifier(lambda _: "Male")
    ok, _ = verifier.verify(None, "Male", "f0")
    assert ok is True


# -- is_placeholder ----------------------------------------------------------


def test_is_placeholder_common_combos() -> None:
    assert is_placeholder("Select")
    assert is_placeholder("Please select")
    assert is_placeholder("-- Select --")
    assert is_placeholder("Select an option")
    assert is_placeholder("DD")
    assert is_placeholder("MM")
    assert is_placeholder("YYYY")
    assert is_placeholder("None")


def test_is_placeholder_does_not_flag_real_values() -> None:
    assert not is_placeholder("Male")
    assert not is_placeholder("Karnataka")
    assert not is_placeholder("1996")
    assert not is_placeholder("02-02-1996")


# -- date_tokens / dates_match -----------------------------------------------


def test_date_tokens_variants() -> None:
    assert date_tokens("15-08-1990") == (15, 8, 1990)
    assert date_tokens("12/31/2000") == (31, 12, 2000)  # US MM/DD/YYYY
    assert date_tokens("1996-02-02") == (2, 2, 1996)
    assert date_tokens("02 February 1996") == (2, 2, 1996)
    assert date_tokens("2 Feb 1996") == (2, 2, 1996)


def test_date_tokens_non_dates() -> None:
    assert date_tokens("Male") is None
    assert date_tokens("Karnataka") is None
    assert date_tokens("9001234567") is None
    assert date_tokens("1996") is None
    assert date_tokens("") is None
    assert date_tokens(None) is None


def test_dates_match_across_spellings() -> None:
    assert dates_match("02 02 1996", "1996-02-02")
    assert dates_match("15-08-1990", "15 Aug 1990")
    assert not dates_match("15-08-1990", "16-08-1990")
    assert not dates_match("Male", "1996-02-02")


# -- UiaValueVerifier --------------------------------------------------------


def test_uia_verifier_matches_live_value() -> None:
    verifier = UiaValueVerifier(lambda _: "Karnataka")
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Karnataka", "f0")
    assert ok is True
    assert "uia matched" in evidence


def test_uia_verifier_matches_date_iso_vs_triplet() -> None:
    verifier = UiaValueVerifier(lambda _: "02 02 1996")
    ok, evidence = verifier.verify(BBox(0, 0, 200, 10), "1996-02-02", "dob")
    assert ok is True
    assert "date matched" in evidence


def test_uia_verifier_placeholder_read_is_mismatch() -> None:
    verifier = UiaValueVerifier(lambda _: "Select")
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Male", "f0")
    assert ok is False
    assert "placeholder" in evidence


def test_uia_verifier_empty_read_is_mismatch() -> None:
    verifier = UiaValueVerifier(lambda _: None)
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Male", "f0")
    assert ok is False
    assert "empty" in evidence


def test_uia_verifier_mismatch_reports_both() -> None:
    verifier = UiaValueVerifier(lambda _: "Kerala")
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Karnataka", "f0")
    assert ok is False
    assert "Kerala" in evidence and "Karnataka" in evidence


def test_uia_verifier_requires_bbox() -> None:
    verifier = UiaValueVerifier(lambda _: "Karnataka")
    ok, evidence = verifier.verify(None, "Karnataka", "f0")
    assert ok is False


def test_uia_verifier_read_exception_is_mismatch() -> None:
    def boom(_: BBox) -> str:
        raise RuntimeError("uia down")

    verifier = UiaValueVerifier(boom)
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Karnataka", "f0")
    assert ok is False
    assert "uia read failed" in evidence


# -- vision date-aware + placeholder -----------------------------------------


def test_vision_verifier_matches_date_triplet() -> None:
    verifier = VisionVerifier(lambda _: [type("L", (), {"text": "02 02 1996"})()])
    ok, evidence = verifier.verify(BBox(0, 0, 200, 10), "1996-02-02", "dob")
    assert ok is True
    assert "date matched" in evidence


def test_vision_verifier_placeholder_read_is_mismatch() -> None:
    verifier = VisionVerifier(lambda _: [type("L", (), {"text": "Select"})()])
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "Male", "f0")
    assert ok is False
    assert "placeholder" in evidence


# -- clipboard whole-window sanitization --------------------------------------


def test_clipboard_whole_window_evidence_does_not_leak_text() -> None:
    class FakeClip:
        def read_focused(self) -> str:
            return "MP\r\nMPF\r\nMember Name\tJohn Doe\r\n" + "x" * 500

    class FakeKeys:
        def press(self, key): pass

    verifier = ClipboardVerifier(FakeKeys(), FakeClip())
    ok, evidence = verifier.verify(BBox(0, 0, 10, 10), "John", "f0")
    assert ok is False
    assert "whole-window" in evidence
    assert "John" not in evidence
    assert "MP" not in evidence
    assert "500" in evidence or "chars" in evidence


def test_looks_like_whole_window_still_detects() -> None:
    assert looks_like_whole_window("line1\nline2")
    assert looks_like_whole_window("x" * 300)
    assert not looks_like_whole_window("John")


# -- _contains_token separator tolerance -------------------------------------


def test_contains_token_separator_drop_around_slash() -> None:
    # Vision/OCR read back "Kataka /Cancer V" while the source holds
    # "Kataka / Cancer" - the spaces around the separator differ. Both must
    # collapse to the same whole-token sequence "kataka cancer".
    assert _contains_token("kataka /cancer v", "kataka / cancer")
    assert _contains_token("kataka/cancer v", "kataka / cancer")
    assert _contains_token("kataka / cancer v", "kataka / cancer")


def test_contains_token_separator_tolerant_keeps_word_boundaries() -> None:
    # Whole-token boundaries must survive the separator collapse: the expected
    # phrase cannot match inside a different word or across an extra token.
    assert not _contains_token("johnny smith", "john smith")
    assert not _contains_token("kataka v cancer", "kataka / cancer")
    assert not _contains_token("12", "1 / 2")
    assert not _contains_token("fullname", "full name")


def test_contains_token_separator_via_vision_verifier() -> None:
    verifier = VisionVerifier(lambda _: [type("L", (), {"text": "Kataka /Cancer V"})()])
    ok, evidence = verifier.verify(BBox(0, 0, 200, 10), "Kataka / Cancer", "rashi")
    assert ok is True
    assert "contains expected" in evidence

