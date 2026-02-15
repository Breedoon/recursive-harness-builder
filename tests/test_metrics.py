"""Tests for obs_agent.metrics."""

import logging
from unittest.mock import MagicMock

from obs_agent.metrics import log_result


class TestLogResult:
    """log_result() extracts and logs metrics from SDK messages."""

    def test_logs_cost_and_duration(self, caplog):
        """Logs cost and duration when present."""
        msg = MagicMock()
        msg.total_cost_usd = 0.0042
        msg.duration_ms = 1500
        msg.usage = None

        with caplog.at_level(logging.INFO, logger="obs_agent.metrics"):
            log_result(msg, label="test")

        assert "cost=$0.0042" in caplog.text
        assert "duration=1500ms" in caplog.text
        assert "[test]" in caplog.text

    def test_logs_usage_tokens(self, caplog):
        """Logs token counts from usage object."""
        msg = MagicMock()
        msg.total_cost_usd = 0.01
        msg.duration_ms = 2000
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_creation_input_tokens = 80
        usage.cache_read_input_tokens = 20
        msg.usage = usage

        with caplog.at_level(logging.INFO, logger="obs_agent.metrics"):
            log_result(msg, label="chat")

        assert "input_tokens=100" in caplog.text
        assert "output_tokens=50" in caplog.text
        assert "cache_creation=80" in caplog.text
        assert "cache_read=20" in caplog.text

    def test_skips_message_without_metrics(self, caplog):
        """Does not log when message has no metrics fields."""
        msg = MagicMock(spec=[])  # empty spec = no attributes

        with caplog.at_level(logging.INFO, logger="obs_agent.metrics"):
            log_result(msg, label="empty")

        assert caplog.text == "" or "[empty]" not in caplog.text

    def test_handles_partial_fields(self, caplog):
        """Logs available fields even when some are missing."""
        msg = MagicMock()
        msg.total_cost_usd = 0.005
        msg.duration_ms = None
        msg.usage = None

        with caplog.at_level(logging.INFO, logger="obs_agent.metrics"):
            log_result(msg, label="partial")

        assert "cost=$0.0050" in caplog.text
        assert "duration" not in caplog.text

    def test_default_label_is_query(self, caplog):
        """Default label is 'query'."""
        msg = MagicMock()
        msg.total_cost_usd = 0.001
        msg.duration_ms = 500
        msg.usage = None

        with caplog.at_level(logging.INFO, logger="obs_agent.metrics"):
            log_result(msg)

        assert "[query]" in caplog.text
