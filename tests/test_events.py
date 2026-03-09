"""Tests for obs_agent.events - SSE status events.

Tests cover:
- StatusEvent.to_sse() produces correct SSE wire format
- StatusEvent.messages field serialization
- summarize_tool_use() structured format for each tool type
- _shorten_path() strips vault prefix
"""

import json

from obs_agent.config import _DEFAULT_VAULT
from obs_agent.events import StatusEvent, _shorten_path, summarize_tool_use


# --- StatusEvent ---


class TestStatusEvent:
    """StatusEvent dataclass and SSE serialization."""

    def test_to_sse_basic(self):
        """to_sse() returns event: status with JSON data."""
        event = StatusEvent(type="tool_use", summary="Read: file.md")
        sse = event.to_sse()
        assert sse.startswith("event: status\n")
        assert "data: " in sse
        assert sse.endswith("\n\n")

    def test_to_sse_json_payload(self):
        """to_sse() data line is valid JSON with type and summary."""
        event = StatusEvent(type="thinking", summary="thinking...")
        sse = event.to_sse()
        # Extract the data line
        lines = sse.strip().split("\n")
        data_line = [l for l in lines if l.startswith("data: ")][0]
        payload = json.loads(data_line[6:])
        assert payload["type"] == "thinking"
        assert payload["summary"] == "thinking..."

    def test_to_sse_without_count(self):
        """to_sse() omits count when None."""
        event = StatusEvent(type="tool_use", summary="Read: foo")
        sse = event.to_sse()
        data_line = [l for l in sse.strip().split("\n") if l.startswith("data: ")][0]
        payload = json.loads(data_line[6:])
        assert "count" not in payload

    def test_to_sse_with_count(self):
        """to_sse() includes count when set."""
        event = StatusEvent(type="queue_delivered", summary="queued message delivered", count=3)
        sse = event.to_sse()
        data_line = [l for l in sse.strip().split("\n") if l.startswith("data: ")][0]
        payload = json.loads(data_line[6:])
        assert payload["count"] == 3

    def test_to_sse_without_messages(self):
        """to_sse() omits messages when None."""
        event = StatusEvent(type="tool_use", summary="Read: foo")
        sse = event.to_sse()
        data_line = [l for l in sse.strip().split("\n") if l.startswith("data: ")][0]
        payload = json.loads(data_line[6:])
        assert "messages" not in payload

    def test_to_sse_with_messages(self):
        """to_sse() includes messages when set."""
        event = StatusEvent(
            type="queue_delivered",
            summary="queued message delivered",
            count=2,
            messages=["hello", "world"],
        )
        sse = event.to_sse()
        data_line = [l for l in sse.strip().split("\n") if l.startswith("data: ")][0]
        payload = json.loads(data_line[6:])
        assert payload["messages"] == ["hello", "world"]

    def test_frozen_dataclass(self):
        """StatusEvent is frozen (immutable)."""
        event = StatusEvent(type="tool_use", summary="test")
        import dataclasses
        assert dataclasses.is_dataclass(event)
        try:
            event.type = "changed"
            assert False, "Should not allow mutation"
        except (AttributeError, dataclasses.FrozenInstanceError):
            pass


# --- summarize_tool_use ---


class TestSummarizeToolUse:
    """summarize_tool_use() generates structured tool summaries."""

    def test_read_with_vault_path(self):
        """Read tool with vault path gets shortened."""
        vault = str(_DEFAULT_VAULT)
        result = summarize_tool_use("Read", {"file_path": f"{vault}/CLAUDE.md"})
        assert result == "Read: CLAUDE.md"

    def test_read_with_non_vault_path(self):
        """Read tool with non-vault path preserves full path."""
        result = summarize_tool_use("Read", {"file_path": "/tmp/test.md"})
        assert result == "Read: /tmp/test.md"

    def test_read_empty_path(self):
        """Read tool with empty file_path returns fallback."""
        result = summarize_tool_use("Read", {"file_path": ""})
        assert result == "Read: file"

    def test_read_no_file_path(self):
        """Read tool with missing file_path returns fallback."""
        result = summarize_tool_use("Read", {})
        assert result == "Read: file"

    def test_grep_with_pattern(self):
        """Grep tool includes the search pattern."""
        result = summarize_tool_use("Grep", {"pattern": "hello"})
        assert result == "Grep: pattern='hello'"

    def test_grep_with_pattern_and_path(self):
        """Grep tool includes pattern and path."""
        vault = str(_DEFAULT_VAULT)
        result = summarize_tool_use("Grep", {"pattern": "skills", "path": f"{vault}/.claude/"})
        assert result == "Grep: pattern='skills' path=.claude/"

    def test_grep_empty_pattern(self):
        """Grep tool with empty pattern returns fallback."""
        result = summarize_tool_use("Grep", {"pattern": ""})
        assert result == "Grep"

    def test_glob_with_pattern(self):
        """Glob tool includes the glob pattern."""
        result = summarize_tool_use("Glob", {"pattern": "**/*.md"})
        assert result == "Glob: '**/*.md'"

    def test_glob_with_pattern_and_path(self):
        """Glob tool includes pattern and path."""
        vault = str(_DEFAULT_VAULT)
        result = summarize_tool_use("Glob", {"pattern": "**/*.md", "path": f"{vault}/.claude/"})
        assert result == "Glob: '**/*.md' in .claude/"

    def test_glob_empty_pattern(self):
        """Glob tool with empty pattern returns fallback."""
        result = summarize_tool_use("Glob", {"pattern": ""})
        assert result == "Glob"

    def test_bash_with_command(self):
        """Bash tool shows truncated command."""
        result = summarize_tool_use("Bash", {"command": "ls -la .claude/"})
        assert result == "Bash: ls -la .claude/"

    def test_bash_truncates_long_command(self):
        """Bash tool truncates commands longer than 200 chars."""
        long_cmd = "a" * 300
        result = summarize_tool_use("Bash", {"command": long_cmd})
        assert result.startswith("Bash: ")
        assert result.endswith("...")
        assert len(result) == 200

    def test_bash_empty_command(self):
        """Bash tool with empty command returns fallback."""
        result = summarize_tool_use("Bash", {"command": ""})
        assert result == "Bash"

    def test_bash_no_command(self):
        """Bash tool with missing command returns tool name."""
        result = summarize_tool_use("Bash", {})
        assert result == "Bash"

    def test_websearch_with_query(self):
        """WebSearch tool includes the query."""
        result = summarize_tool_use("WebSearch", {"query": "test"})
        assert result == "WebSearch: 'test'"

    def test_websearch_empty_query(self):
        """WebSearch tool with empty query returns fallback."""
        result = summarize_tool_use("WebSearch", {"query": ""})
        assert result == "WebSearch"

    def test_unknown_tool_with_args(self):
        """Unknown tool emits structured payload for visibility."""
        result = summarize_tool_use("SomeTool", {"arg1": "val1", "arg2": "val2", "arg3": "val3", "arg4": "val4"})
        payload = json.loads(result)
        assert payload["tool"] == "SomeTool"
        assert payload["input"]["arg1"] == "val1"
        assert payload["input"]["arg2"] == "val2"
        assert payload["input"]["arg3"] == "val3"
        assert payload["input"]["arg4"] == "val4"

    def test_unknown_tool_no_args(self):
        """Unknown tool with no args still emits structured payload."""
        result = summarize_tool_use("UnknownTool", {})
        assert json.loads(result) == {"tool": "UnknownTool"}

    def test_edit_tool_dumps_args(self):
        """Edit tool uses unknown-tool structured fallback."""
        result = summarize_tool_use("Edit", {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"})
        payload = json.loads(result)
        assert payload["tool"] == "Edit"
        assert payload["input"]["file_path"] == "/tmp/x"

    def test_fork_task_uses_description(self):
        """ForkTask prefers the short user-facing description."""
        result = summarize_tool_use(
            "ForkTask",
            {"prompt": "Summarize the codebase", "description": "Code summary"},
        )
        assert result == "ForkTask: Code summary"

    def test_fork_task_falls_back_to_prompt(self):
        """ForkTask falls back to prompt text when no description is present."""
        result = summarize_tool_use("ForkTask", {"prompt": "Research topic"})
        assert result == "ForkTask: Research topic"

    def test_fork_task_includes_timeout(self):
        """ForkTask shows timeout when the caller provided one."""
        result = summarize_tool_use(
            "ForkTask",
            {"description": "Research topic", "timeout_ms": 5000},
        )
        assert result == "ForkTask: Research topic timeout=5000ms"

    def test_fork_task_includes_max_turns(self):
        result = summarize_tool_use(
            "ForkTask",
            {"description": "Research topic", "max_turns": 25},
        )
        assert result == "ForkTask: Research topic max_turns=25"

    def test_fork_task_truncates_long_prompt(self):
        """ForkTask truncates long fallback prompt summaries."""
        long_prompt = "a" * 300
        result = summarize_tool_use("ForkTask", {"prompt": long_prompt})
        assert result.startswith("ForkTask: ")
        assert result.endswith("...")
        assert len(result) == 200

    def test_fork_task_empty(self):
        """ForkTask with no prompt or description returns just the tool name."""
        result = summarize_tool_use("ForkTask", {})
        assert result == "ForkTask"

    def test_fork_task_resume_summary(self):
        result = summarize_tool_use("ForkTask", {"description": "Research topic", "resume": "agent-123"})
        assert result == "ForkTask: Research topic resume=agent-123"

    def test_fork_task_output_summary(self):
        result = summarize_tool_use("ForkTaskOutput", {"task_id": "agent-123", "block": True})
        assert result == "ForkTaskOutput: agent-123 block=True"

    def test_fork_task_stop_summary(self):
        result = summarize_tool_use("ForkTaskStop", {"task_id": "agent-123"})
        assert result == "ForkTaskStop: agent-123"

    def test_task_tool_with_prompt(self):
        """Task tool shows the prompt."""
        result = summarize_tool_use("Task", {"prompt": "Explore the codebase"})
        assert result == "Task: Explore the codebase"

    def test_task_tool_truncates_long_prompt(self):
        """Task tool truncates prompts longer than 200 chars."""
        long_prompt = "x" * 300
        result = summarize_tool_use("Task", {"prompt": long_prompt})
        assert result.startswith("Task: ")
        assert result.endswith("...")
        assert len(result) == 200

    def test_task_tool_empty_prompt(self):
        """Task tool with empty prompt returns just 'Task'."""
        result = summarize_tool_use("Task", {"prompt": ""})
        assert result == "Task"


# --- _shorten_path ---


class TestShortenPath:
    """_shorten_path() strips the vault prefix."""

    def test_strips_vault_prefix(self):
        """Strips the default vault path prefix."""
        vault = str(_DEFAULT_VAULT)
        result = _shorten_path(f"{vault}/CLAUDE.md")
        assert result == "CLAUDE.md"

    def test_strips_trailing_slash(self):
        """Handles paths immediately under vault root."""
        vault = str(_DEFAULT_VAULT)
        result = _shorten_path(f"{vault}/file.md")
        assert result == "file.md"

    def test_preserves_non_vault_path(self):
        """Returns non-vault paths unchanged."""
        result = _shorten_path("/tmp/something.md")
        assert result == "/tmp/something.md"

    def test_empty_string(self):
        """Returns empty string for empty input."""
        result = _shorten_path("")
        assert result == ""
