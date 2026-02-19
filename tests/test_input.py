"""Tests for obs_agent.input - InputChannel abstraction.

Tests cover:
- SimpleChannel implements the InputChannel protocol
- PromptToolkitChannel has correct methods
- MAX_INPUT_LENGTH constant
- Output methods produce correct formatting
"""

from obs_agent.input import MAX_INPUT_LENGTH, SimpleChannel


class TestMaxInputLength:
    """MAX_INPUT_LENGTH constant."""

    def test_value(self):
        """MAX_INPUT_LENGTH is 100,000."""
        assert MAX_INPUT_LENGTH == 100_000


class TestSimpleChannel:
    """SimpleChannel implements InputChannel protocol."""

    def test_has_read_input(self):
        """SimpleChannel has read_input method."""
        ch = SimpleChannel()
        assert hasattr(ch, "read_input")
        assert callable(ch.read_input)

    def test_has_print_output(self):
        """SimpleChannel has print_output method."""
        ch = SimpleChannel()
        assert hasattr(ch, "print_output")
        assert callable(ch.print_output)

    def test_has_print_status(self):
        """SimpleChannel has print_status method."""
        ch = SimpleChannel()
        assert hasattr(ch, "print_status")
        assert callable(ch.print_status)

    def test_has_print_queued(self):
        """SimpleChannel has print_queued method."""
        ch = SimpleChannel()
        assert hasattr(ch, "print_queued")
        assert callable(ch.print_queued)

    def test_has_close(self):
        """SimpleChannel has close method."""
        ch = SimpleChannel()
        assert hasattr(ch, "close")
        assert callable(ch.close)

    def test_print_output(self, capsys):
        """print_output prints text without trailing newline."""
        ch = SimpleChannel()
        ch.print_output("hello")
        captured = capsys.readouterr()
        assert captured.out == "hello"

    def test_print_status(self, capsys):
        """print_status prints dim ANSI text with parentheses."""
        ch = SimpleChannel()
        ch.print_status("Read: CLAUDE.md")
        captured = capsys.readouterr()
        assert "(Read: CLAUDE.md)" in captured.out
        assert "\033[2m" in captured.out
        assert "\033[0m" in captured.out

    def test_print_queued_single(self, capsys):
        """print_queued prints dim queued message preview."""
        ch = SimpleChannel()
        ch.print_queued(["hello world"])
        captured = capsys.readouterr()
        assert "(queued: hello world)" in captured.out
        assert "\033[2m" in captured.out

    def test_print_queued_truncates(self, capsys):
        """print_queued truncates long messages at 120 chars."""
        ch = SimpleChannel()
        long_msg = "x" * 200
        ch.print_queued([long_msg])
        captured = capsys.readouterr()
        assert "..." in captured.out
        # Should not contain the full 200 chars
        assert "x" * 200 not in captured.out

    def test_print_queued_multiple(self, capsys):
        """print_queued prints each message on its own line."""
        ch = SimpleChannel()
        ch.print_queued(["first", "second"])
        captured = capsys.readouterr()
        assert "(queued: first)" in captured.out
        assert "(queued: second)" in captured.out

    def test_close_is_noop(self):
        """close() doesn't raise."""
        ch = SimpleChannel()
        ch.close()  # Should not raise


class TestPromptToolkitChannel:
    """PromptToolkitChannel has correct methods."""

    def test_has_correct_methods(self):
        """PromptToolkitChannel class has all required protocol methods."""
        from obs_agent.input import PromptToolkitChannel
        assert hasattr(PromptToolkitChannel, "read_input")
        assert hasattr(PromptToolkitChannel, "print_output")
        assert hasattr(PromptToolkitChannel, "print_status")
        assert hasattr(PromptToolkitChannel, "print_queued")
        assert hasattr(PromptToolkitChannel, "close")
