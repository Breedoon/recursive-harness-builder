"""Tests for structured judge verdict enforcement."""

from tests.evals.judge import _enforce_judgment_structure


def test_enforce_judgment_structure_accepts_well_formed_output() -> None:
    judgment = """CRITERIA CHECK:
- criterion 1: PASS - reason
INTENT CHECK:
- matched intent
NOTES: none
VERDICT: PASS
"""
    ok, normalized = _enforce_judgment_structure(judgment)
    assert ok
    assert "HARNESS FORMAT CHECK" not in normalized


def test_enforce_judgment_structure_accepts_bold_markdown_headings() -> None:
    judgment = """**CRITERIA CHECK:**
- criterion 1: PASS - reason
**INTENT CHECK:**
- matched intent
**NOTES:** none
**VERDICT:** PASS
"""
    ok, normalized = _enforce_judgment_structure(judgment)
    assert ok
    assert "HARNESS FORMAT CHECK" not in normalized


def test_enforce_judgment_structure_rejects_missing_sections() -> None:
    judgment = """CRITERIA CHECK:
- criterion 1: PASS - reason
VERDICT: PASS
"""
    ok, normalized = _enforce_judgment_structure(judgment)
    assert not ok
    assert "HARNESS FORMAT CHECK" in normalized
    assert "missing required section: INTENT CHECK:" in normalized
    assert "missing required section: NOTES:" in normalized
