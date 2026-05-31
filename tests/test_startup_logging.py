import logging

import pytest

from obs_agent.startup_logging import StartupProfiler, _format_fields


def test_format_fields_includes_counts_not_secrets():
    rendered = _format_fields({
        "sender_bot_count": 5,
        "enabled": True,
        "empty": None,
    })

    assert "sender_bot_count=5" in rendered
    assert "enabled=true" in rendered
    assert "empty" not in rendered


def test_startup_profiler_logs_phase_completion(caplog):
    logger = logging.getLogger("tests.startup")
    profiler = StartupProfiler(logger, "test-daemon")

    with caplog.at_level(logging.INFO, logger="tests.startup"):
        with profiler.phase("load_config", sender_bot_count=5):
            pass
        profiler.complete(port=7832)

    messages = [record.getMessage() for record in caplog.records]
    assert any("startup phase_start component=test-daemon phase=load_config" in msg for msg in messages)
    assert any("startup phase_complete component=test-daemon phase=load_config" in msg for msg in messages)
    assert any("sender_bot_count=5" in msg for msg in messages)
    assert any("startup complete component=test-daemon phase=startup" in msg for msg in messages)


def test_startup_profiler_logs_failed_phase(caplog):
    logger = logging.getLogger("tests.startup.failure")
    profiler = StartupProfiler(logger, "test-daemon")

    with caplog.at_level(logging.INFO, logger="tests.startup.failure"):
        with pytest.raises(RuntimeError):
            with profiler.phase("validate_config"):
                raise RuntimeError("boom")

    messages = [record.getMessage() for record in caplog.records]
    assert any("startup phase_failed component=test-daemon phase=validate_config" in msg for msg in messages)
