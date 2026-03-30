"""Tests for structured judge verdict enforcement."""

from __future__ import annotations

from tests.evals import judge as judge_module
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


async def test_run_sdk_judge_defaults_to_haiku(monkeypatch) -> None:
    captured = {}

    class DummyClient:
        def __init__(self, options) -> None:
            captured["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def query(self, prompt: str) -> None:
            captured["prompt"] = prompt

        async def receive_response(self):
            if False:
                yield None

    monkeypatch.delenv("OBS_EVAL_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(judge_module, "ClaudeSDKClient", DummyClient)

    judgment = await judge_module._run_sdk_judge("prompt")

    assert judgment == ""
    assert captured["prompt"] == "prompt"
    assert captured["options"].model == "haiku"
