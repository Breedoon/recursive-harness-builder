"""Unit tests for cache proxy normalization functions.

Pure unit tests — no network calls, no Anthropic API.
Tests each normalization rule individually, edge cases, idempotency,
and the combined normalize_request function.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

# Add src/ to path so we can import cache_proxy
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_stats():
    """Reset global stats before each test."""
    for key in cache_proxy.stats:
        cache_proxy.stats[key] = 0


def _make_body(
    *,
    system=None,
    messages=None,
    tools=None,
    metadata=None,
    stream=False,
):
    """Build a minimal request body for testing."""
    body = {}
    if system is not None:
        body["system"] = system
    if messages is not None:
        body["messages"] = messages
    if tools is not None:
        body["tools"] = tools
    if metadata is not None:
        body["metadata"] = metadata
    if stream:
        body["stream"] = True
    return body


def _user_msg(content):
    """Create a user message."""
    return {"role": "user", "content": content}


def _assistant_msg(content):
    """Create an assistant message."""
    return {"role": "assistant", "content": content}


def _text_block(text, **kwargs):
    """Create a text content block."""
    block = {"type": "text", "text": text}
    block.update(kwargs)
    return block


def _skill_block(extra_text=""):
    """Create a skill listing block."""
    return _text_block(
        f"<system-reminder>\n{cache_proxy.SKILL_MARKER}\n- skill-a\n- skill-b\n{extra_text}</system-reminder>"
    )


def _claudemd_block():
    """Create a CLAUDE.md context block."""
    return _text_block(
        f"<system-reminder>\n{cache_proxy.CLAUDEMD_MARKER}\n# Project\nSome instructions\n</system-reminder>"
    )


def _dynamic_reminder_block(reminder_type="changed_files"):
    """Create a dynamic system-reminder block (should be stripped)."""
    return _text_block(
        f"<system-reminder>\n{reminder_type}: file1.py, file2.py\n</system-reminder>"
    )


def _billing_block(cch="abc123", version="2.1.59"):
    """Create a billing header block."""
    return _text_block(
        f"x-anthropic-billing-header: cc_version={version}; cc_entrypoint=sdk-py; cch={cch};"
    )


# ── Rule 1: Billing Header ───────────────────────────────────────────────


class TestNormalizeBillingHeader:
    def test_replaces_billing_header(self):
        body = _make_body(system=[_billing_block()])
        count = cache_proxy.normalize_billing_header(body)
        assert count == 1
        assert body["system"][0]["text"] == cache_proxy.FIXED_BILLING_HEADER

    def test_idempotent(self):
        body = _make_body(system=[_billing_block()])
        cache_proxy.normalize_billing_header(body)
        body_copy = copy.deepcopy(body)
        count = cache_proxy.normalize_billing_header(body)
        # Already fixed — the fixed header starts with "x-anthropic-billing-header:"
        # so it matches the startswith check and gets replaced again (to same value)
        assert body == body_copy

    def test_no_system(self):
        body = _make_body()
        count = cache_proxy.normalize_billing_header(body)
        assert count == 0

    def test_empty_system(self):
        body = _make_body(system=[])
        count = cache_proxy.normalize_billing_header(body)
        assert count == 0

    def test_system_without_billing(self):
        body = _make_body(system=[_text_block("some other system text")])
        count = cache_proxy.normalize_billing_header(body)
        assert count == 0
        assert body["system"][0]["text"] == "some other system text"

    def test_non_text_block_ignored(self):
        body = _make_body(system=[{"type": "image", "source": {}}])
        count = cache_proxy.normalize_billing_header(body)
        assert count == 0

    def test_different_billing_values_normalized(self):
        """Different cch and version values should all normalize to the same fixed header."""
        body1 = _make_body(system=[_billing_block(cch="aaa", version="2.1.59")])
        body2 = _make_body(system=[_billing_block(cch="bbb", version="2.1.81")])
        cache_proxy.normalize_billing_header(body1)
        cache_proxy.normalize_billing_header(body2)
        assert body1["system"][0]["text"] == body2["system"][0]["text"]

    def test_stats_updated(self):
        body = _make_body(system=[_billing_block()])
        cache_proxy.normalize_billing_header(body)
        assert cache_proxy.stats["billing_normalized"] == 1


# ── Rule 2: String→List Content ──────────────────────────────────────────


class TestNormalizeUserContentStructure:
    def test_converts_string_to_list(self):
        body = _make_body(messages=[_user_msg("hello")])
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 1
        assert body["messages"][0]["content"] == [_text_block("hello")]

    def test_already_list_unchanged(self):
        body = _make_body(messages=[_user_msg([_text_block("hello")])])
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 0

    def test_idempotent(self):
        body = _make_body(messages=[_user_msg("hello")])
        cache_proxy.normalize_user_content_structure(body)
        body_copy = copy.deepcopy(body)
        cache_proxy.normalize_user_content_structure(body)
        assert body == body_copy

    def test_skips_assistant_messages(self):
        body = _make_body(messages=[_assistant_msg("hello")])
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 0

    def test_multiple_messages(self):
        body = _make_body(messages=[
            _user_msg("first"),
            _assistant_msg("reply"),
            _user_msg([_text_block("already list")]),
            _user_msg("third"),
        ])
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 2  # first and third

    def test_empty_string(self):
        body = _make_body(messages=[_user_msg("")])
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 1
        assert body["messages"][0]["content"] == [_text_block("")]

    def test_no_messages(self):
        body = _make_body(messages=[])
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 0

    def test_no_messages_key(self):
        body = _make_body()
        count = cache_proxy.normalize_user_content_structure(body)
        assert count == 0

    def test_stats_updated(self):
        body = _make_body(messages=[_user_msg("a"), _user_msg("b")])
        cache_proxy.normalize_user_content_structure(body)
        assert cache_proxy.stats["strings_converted"] == 2


# ── Rule 3: Skill Listing Stripping ─────────────────────────────────────


class TestNormalizeSkillListing:
    def test_strips_skill_from_last_message(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
            _user_msg([_text_block("question"), _skill_block()]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "stripped"
        # Skill should be gone, "question" remains
        assert len(body["messages"]) == 2
        assert len(body["messages"][1]["content"]) == 1
        assert body["messages"][1]["content"][0]["text"] == "question"

    def test_strips_skill_from_first_message(self):
        body = _make_body(messages=[
            _user_msg([_skill_block(), _text_block("hello")]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "stripped"
        # Skill removed, "hello" remains
        assert len(body["messages"][0]["content"]) == 1
        assert body["messages"][0]["content"][0]["text"] == "hello"

    def test_idempotent(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
            _user_msg([_skill_block()]),
        ])
        cache_proxy.normalize_skill_listing(body)
        body_copy = copy.deepcopy(body)
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "not_found"
        assert body == body_copy

    def test_no_skill_listing(self):
        body = _make_body(messages=[
            _user_msg([_text_block("no skill here")]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "not_found"

    def test_empty_messages(self):
        body = _make_body(messages=[])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "no_messages"

    def test_strips_skill_in_middle_message(self):
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _user_msg([_skill_block()]),
            _user_msg([_text_block("third")]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "stripped"
        # msg[1] had only skill_block → empty after extraction → removed
        assert len(body["messages"]) == 2
        assert body["messages"][0]["content"][0]["text"] == "first"
        assert body["messages"][1]["content"][0]["text"] == "third"

    def test_removes_empty_message_after_stripping(self):
        """If stripping the skill block leaves an empty content array, remove the message."""
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _user_msg([_skill_block()]),  # Only has skill block
        ])
        cache_proxy.normalize_skill_listing(body)
        # The second message should be removed (was empty after stripping)
        assert len(body["messages"]) == 1
        assert body["messages"][0]["content"][0]["text"] == "first"

    def test_preserves_other_blocks_in_source_message(self):
        """If source message had other blocks, they remain after skill stripping."""
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _user_msg([_text_block("other"), _skill_block(), _text_block("more")]),
        ])
        cache_proxy.normalize_skill_listing(body)
        # Skill stripped from msg[1], "other" and "more" remain
        assert len(body["messages"]) == 2
        src_content = body["messages"][1]["content"]
        assert len(src_content) == 2
        assert src_content[0]["text"] == "other"
        assert src_content[1]["text"] == "more"

    def test_skill_in_assistant_message_ignored(self):
        """Skill listing in assistant messages should not be stripped."""
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
            _assistant_msg([_skill_block()]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "not_found"

    def test_stats_updated_stripped(self):
        body = _make_body(messages=[
            _user_msg([_text_block("a")]),
            _user_msg([_skill_block()]),
        ])
        cache_proxy.normalize_skill_listing(body)
        assert cache_proxy.stats["skill_stripped"] == 1

    def test_stats_updated_missing(self):
        body = _make_body(messages=[_user_msg([_text_block("no skill")])])
        cache_proxy.normalize_skill_listing(body)
        assert cache_proxy.stats["skill_missing"] == 1


# ── _is_strippable_system_reminder ────────────────────────────────────────


class TestIsStrippableSystemReminder:
    def test_dynamic_reminder_is_strippable(self):
        block = _dynamic_reminder_block("changed_files")
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_skill_listing_is_strippable(self):
        """Skill blocks are strippable — Rule 3 strips them first, but if any
        remain (edge case), Rule 4 should catch them too."""
        block = _skill_block()
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_claudemd_is_strippable(self):
        block = _claudemd_block()
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_non_text_block_not_strippable(self):
        block = {"type": "image", "source": {}}
        assert cache_proxy._is_strippable_system_reminder(block) is False

    def test_text_without_system_reminder_not_strippable(self):
        block = _text_block("just regular text")
        assert cache_proxy._is_strippable_system_reminder(block) is False

    def test_non_dict_not_strippable(self):
        assert cache_proxy._is_strippable_system_reminder("string") is False
        assert cache_proxy._is_strippable_system_reminder(42) is False
        assert cache_proxy._is_strippable_system_reminder(None) is False

    def test_various_dynamic_reminder_types(self):
        """All dynamic reminder types should be strippable."""
        types = [
            "changed_files", "todo_reminders", "token_usage",
            "budget_usd", "diagnostics", "lsp_diagnostics",
            "nested_memory",
        ]
        for rtype in types:
            block = _dynamic_reminder_block(rtype)
            assert cache_proxy._is_strippable_system_reminder(block) is True, f"Failed for {rtype}"

    def test_empty_text_with_system_reminder_tag(self):
        block = _text_block("<system-reminder></system-reminder>")
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_block_with_both_skill_and_reminder(self):
        """A block with both skill marker AND system-reminder SHOULD be stripped
        (skill blocks are no longer preserved)."""
        block = _text_block(
            f"<system-reminder>\n{cache_proxy.SKILL_MARKER}\nsome other stuff\n</system-reminder>"
        )
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_block_with_both_claudemd_and_reminder_is_strippable(self):
        block = _text_block(
            f"<system-reminder>\n{cache_proxy.CLAUDEMD_MARKER}\nsome stuff\n</system-reminder>"
        )
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_changed_files_containing_claudemd_marker_is_strippable(self):
        """Bug 1: A changed_files reminder whose diff contains the CLAUDEMD_MARKER
        string should still be stripped. The marker check should only look at the
        beginning of the text (where CC places it), not deep in diff content."""
        diff_content = (
            "<system-reminder>\n"
            "changed_files: CLAUDE.md was modified:\n"
            "--- a/CLAUDE.md\n"
            "+++ b/CLAUDE.md\n"
            "@@ -1,5 +1,6 @@\n"
            f" {cache_proxy.CLAUDEMD_MARKER}\n"
            " # Some heading\n"
            "+New line added\n"
            "</system-reminder>"
        )
        block = _text_block(diff_content)
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_real_claudemd_block_is_strippable(self):
        block = _text_block(
            f"<system-reminder>\n{cache_proxy.CLAUDEMD_MARKER}\n"
            "# Project Instructions\nLong vault content here...\n"
            "</system-reminder>"
        )
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_claudemd_marker_deep_in_large_diff_is_strippable(self):
        """Bug 1 regression: A large changed_files diff with the CLAUDEMD_MARKER
        string appearing hundreds of chars into the text must still be stripped."""
        large_diff = (
            "<system-reminder>\n"
            "changed_files: Multiple files modified:\n"
            + "--- a/some/file.py\n+++ b/some/file.py\n@@ -100,5 +100,6 @@\n some code\n" * 10
            + f"--- a/CLAUDE.md\n+++ b/CLAUDE.md\n@@ -1,3 +1,4 @@\n {cache_proxy.CLAUDEMD_MARKER}\n"
            + " # Heading\n+Added line\n"
            + "</system-reminder>"
        )
        block = _text_block(large_diff)
        # The marker is deep inside, NOT at the start → should be strippable
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_claudemd_marker_at_exact_boundary_is_strippable(self):
        block = _text_block(
            f"<system-reminder>\n{cache_proxy.CLAUDEMD_MARKER}\nContent\n</system-reminder>"
        )
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_normalize_request_strips_claude_code_entry_file_reminder(self):
        body = _make_body(
            system=[_text_block("<!-- OBS_AGENT_ENTRY_FILE_CONTEXT -->\n# Custom entry")],
            messages=[_user_msg([_text_block("hello"), _claudemd_block()])],
        )
        result_body, info = cache_proxy.normalize_request(body)
        assert info["reminders"] == 1
        assert result_body["messages"][0]["content"] == [_text_block("hello")]
        assert result_body["system"][0]["text"].count("<!-- OBS_AGENT_ENTRY_FILE_CONTEXT -->") == 1


# ── Regression: JSONL truncation masking ──────────────────────────────────


class TestJSONLTruncationMasking:
    """Verify proxy normalizations handle truncated/incomplete assistant messages
    gracefully. When JSONL is copied mid-stream, assistant messages may have
    incomplete tool_use blocks. The proxy should not crash on these."""

    def test_normalize_request_with_incomplete_tool_use(self):
        """Assistant message with a tool_use block but no corresponding tool_result.
        The proxy doesn't touch assistant messages, so this should pass through."""
        body = _make_body(
            system=[_billing_block(), _text_block("p"), _system_block_with_git_status()],
            messages=[
                _user_msg([_text_block("do something")]),
                _assistant_msg([
                    {"type": "text", "text": "I'll help"},
                    {"type": "tool_use", "id": "toolu_abc", "name": "Bash",
                     "input": {"command": "ls"}},
                ]),
                # No tool_result — truncated JSONL
            ],
            tools=[{"name": "Bash", "description": "run commands"}],
            metadata={"user_id": "user_x_session_abc-123"},
        )
        # Should not crash
        result_body, info = cache_proxy.normalize_request(body)
        assert info["action"] == "normalized"
        # Assistant message should be unchanged
        assert result_body["messages"][1]["role"] == "assistant"
        assert len(result_body["messages"][1]["content"]) == 2

    def test_normalize_request_with_empty_assistant_content(self):
        """Assistant message with empty content list (extreme truncation)."""
        body = _make_body(
            messages=[
                _user_msg([_text_block("hello")]),
                _assistant_msg([]),
            ],
        )
        result_body, info = cache_proxy.normalize_request(body)
        assert result_body["messages"][1]["content"] == []

    def test_normalize_request_with_partial_thinking_block(self):
        """Assistant message with a thinking block but no text block (mid-stream)."""
        body = _make_body(
            messages=[
                _user_msg([_text_block("think about this")]),
                _assistant_msg([
                    {"type": "thinking", "thinking": "Let me consider...",
                     "signature": "sig123"},
                ]),
            ],
        )
        result_body, info = cache_proxy.normalize_request(body)
        assert result_body["messages"][1]["content"][0]["type"] == "thinking"

    def test_normalize_handles_tool_result_without_tool_use(self):
        """User message with tool_result but no preceding tool_use (orphaned result).
        This can happen with truncated JSONL. Proxy should handle gracefully."""
        body = _make_body(
            messages=[
                _user_msg([
                    {"type": "tool_result", "tool_use_id": "toolu_orphan",
                     "content": "some result"},
                ]),
            ],
        )
        result_body, info = cache_proxy.normalize_request(body)
        # tool_result is not a text block, so it passes through
        assert result_body["messages"][0]["content"][0]["type"] == "tool_result"


# ── Regression: MCP tool count changes ────────────────────────────────────


class TestMCPToolCountChanges:
    """Verify proxy handles requests with varying tool counts gracefully.
    MCP servers can disconnect/reconnect, changing the tool count (e.g., 96→72).
    Tool sorting should still work, and normalization shouldn't crash."""

    def test_sort_with_many_tools(self):
        """Sorting works correctly with a large tool array (simulating full MCP set)."""
        tools = [{"name": f"tool_{chr(122-i)}", "description": ""} for i in range(50)]
        body = _make_body(tools=tools)
        count = cache_proxy.normalize_tool_order(body)
        assert count == 1
        names = [t["name"] for t in body["tools"]]
        assert names == sorted(names)

    def test_sort_with_fewer_tools_after_disconnect(self):
        """After MCP disconnect, fewer tools are present. Sorting still works."""
        tools = [{"name": f"tool_{chr(122-i)}", "description": ""} for i in range(30)]
        body = _make_body(tools=tools)
        count = cache_proxy.normalize_tool_order(body)
        assert count == 1
        names = [t["name"] for t in body["tools"]]
        assert names == sorted(names)

    def test_different_tool_counts_produce_different_sorted_output(self):
        """Two requests with different tool counts produce different (but each sorted) output.
        This is expected — different tool counts = different prefix = cache miss. Not fixable."""
        tools_full = sorted([
            {"name": "Read", "description": ""},
            {"name": "Write", "description": ""},
            {"name": "Bash", "description": ""},
            {"name": "tasknotes_query", "description": ""},
        ], key=lambda t: t["name"])
        tools_reduced = sorted([
            {"name": "Read", "description": ""},
            {"name": "Write", "description": ""},
            {"name": "Bash", "description": ""},
            # tasknotes_query missing (MCP disconnected)
        ], key=lambda t: t["name"])

        body1 = _make_body(tools=tools_full)
        body2 = _make_body(tools=tools_reduced)
        cache_proxy.normalize_tool_order(body1)
        cache_proxy.normalize_tool_order(body2)

        # Both sorted, but different lengths
        assert len(body1["tools"]) == 4
        assert len(body2["tools"]) == 3
        assert [t["name"] for t in body1["tools"]] == ["Bash", "Read", "Write", "tasknotes_query"]
        assert [t["name"] for t in body2["tools"]] == ["Bash", "Read", "Write"]

    def test_normalize_request_with_no_tools_key(self):
        """Request without tools key (subagent without MCP) doesn't crash."""
        body = _make_body(
            messages=[_user_msg([_text_block("hello")])],
        )
        result_body, info = cache_proxy.normalize_request(body)
        assert info["tools"] == 0
        assert "tools" not in result_body

    def test_tools_with_duplicate_names_sorted_stably(self):
        """If two tools have the same name (edge case), sorting is stable."""
        body = _make_body(tools=[
            {"name": "tool_a", "description": "version 1"},
            {"name": "tool_b", "description": ""},
            {"name": "tool_a", "description": "version 2"},
        ])
        cache_proxy.normalize_tool_order(body)
        # Both tool_a entries should be adjacent
        names = [t["name"] for t in body["tools"]]
        assert names == ["tool_a", "tool_a", "tool_b"]


# ── Rule 4: Strip Dynamic Reminders ──────────────────────────────────────


class TestStripDynamicReminders:
    def test_strips_dynamic_reminders(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello"), _dynamic_reminder_block()]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 1
        assert len(body["messages"][0]["content"]) == 1
        assert body["messages"][0]["content"][0]["text"] == "hello"

    def test_strips_skill_listing_too(self):
        """Skill blocks are no longer preserved — they get stripped like other reminders."""
        body = _make_body(messages=[
            _user_msg([_skill_block(), _dynamic_reminder_block()]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 2
        assert len(body["messages"][0]["content"]) == 0

    def test_preserves_claudemd(self):
        body = _make_body(messages=[
            _user_msg([_claudemd_block(), _dynamic_reminder_block()]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 1
        assert len(body["messages"][0]["content"]) == 1
        assert cache_proxy.CLAUDEMD_MARKER in body["messages"][0]["content"][0]["text"]

    def test_strips_multiple_reminders(self):
        body = _make_body(messages=[
            _user_msg([
                _text_block("user text"),
                _dynamic_reminder_block("changed_files"),
                _dynamic_reminder_block("todo_reminders"),
                _dynamic_reminder_block("token_usage"),
            ]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 3
        assert len(body["messages"][0]["content"]) == 1

    def test_idempotent(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello"), _dynamic_reminder_block()]),
        ])
        cache_proxy.strip_dynamic_reminders(body)
        body_copy = copy.deepcopy(body)
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 0
        assert body == body_copy

    def test_skips_assistant_messages(self):
        body = _make_body(messages=[
            _assistant_msg([_dynamic_reminder_block()]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 0

    def test_skips_non_list_content(self):
        body = _make_body(messages=[
            _user_msg("bare string with <system-reminder>stuff</system-reminder>"),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 0

    def test_no_messages(self):
        body = _make_body(messages=[])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 0

    def test_across_multiple_messages(self):
        body = _make_body(messages=[
            _user_msg([_text_block("a"), _dynamic_reminder_block()]),
            _assistant_msg("reply"),
            _user_msg([_dynamic_reminder_block(), _text_block("b")]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 2

    def test_stats_updated(self):
        body = _make_body(messages=[
            _user_msg([_dynamic_reminder_block(), _dynamic_reminder_block()]),
        ])
        cache_proxy.strip_dynamic_reminders(body)
        assert cache_proxy.stats["reminders_stripped"] == 2


# ── Rule 5: Git Status Normalization ─────────────────────────────────────


def _system_block_with_git_status(git_status_content="M file.txt\n?? untracked.md"):
    """Create a sys[2]-like block with a gitStatus section at the end."""
    return _text_block(
        "You are an interactive agent that helps users with software engineering tasks.\n"
        "Use the instructions below.\n\n"
        "# Environment\n"
        "Platform: darwin\n\n"
        f"gitStatus: This is the git status.\nCurrent branch: main\n\nStatus:\n{git_status_content}"
    )


class TestNormalizeGitStatus:
    def test_normalizes_git_status(self):
        body = _make_body(system=[
            _billing_block(),
            _text_block("Claude Code preamble"),
            _system_block_with_git_status("M dirty.py\n?? new.md"),
        ])
        count = cache_proxy.normalize_git_status(body)
        assert count == 1
        assert body["system"][2]["text"].endswith("gitStatus: normalized")
        assert "dirty.py" not in body["system"][2]["text"]

    def test_idempotent(self):
        body = _make_body(system=[
            _billing_block(),
            _text_block("preamble"),
            _system_block_with_git_status(),
        ])
        cache_proxy.normalize_git_status(body)
        body_copy = copy.deepcopy(body)
        count = cache_proxy.normalize_git_status(body)
        assert count == 0
        assert body == body_copy

    def test_different_git_status_normalizes_same(self):
        """Two bodies with different git status should produce identical sys[2]."""
        body1 = _make_body(system=[
            _billing_block(), _text_block("p"),
            _system_block_with_git_status("M file_a.py\n?? untracked_a.md"),
        ])
        body2 = _make_body(system=[
            _billing_block(), _text_block("p"),
            _system_block_with_git_status("M file_b.py\nM file_c.py\n?? other.md"),
        ])
        cache_proxy.normalize_git_status(body1)
        cache_proxy.normalize_git_status(body2)
        assert body1["system"][2]["text"] == body2["system"][2]["text"]

    def test_no_system(self):
        body = _make_body()
        count = cache_proxy.normalize_git_status(body)
        assert count == 0

    def test_system_too_short(self):
        body = _make_body(system=[_billing_block()])
        count = cache_proxy.normalize_git_status(body)
        assert count == 0

    def test_system_two_blocks(self):
        body = _make_body(system=[_billing_block(), _text_block("only two")])
        count = cache_proxy.normalize_git_status(body)
        assert count == 0

    def test_no_git_status_in_sys2(self):
        body = _make_body(system=[
            _billing_block(),
            _text_block("preamble"),
            _text_block("System prompt without any git status section"),
        ])
        count = cache_proxy.normalize_git_status(body)
        assert count == 0

    def test_preserves_content_before_git_status(self):
        body = _make_body(system=[
            _billing_block(),
            _text_block("preamble"),
            _system_block_with_git_status(),
        ])
        original_before = body["system"][2]["text"].split("gitStatus:")[0]
        cache_proxy.normalize_git_status(body)
        assert body["system"][2]["text"].startswith(original_before)

    def test_non_text_sys2_ignored(self):
        body = _make_body(system=[
            _billing_block(),
            _text_block("preamble"),
            {"type": "image", "source": {}},
        ])
        count = cache_proxy.normalize_git_status(body)
        assert count == 0

    def test_stats_updated(self):
        body = _make_body(system=[
            _billing_block(),
            _text_block("preamble"),
            _system_block_with_git_status(),
        ])
        cache_proxy.normalize_git_status(body)
        assert cache_proxy.stats["git_status_normalized"] == 1


# ── Rule 5 (cache_control) was removed — CC's native placement is sufficient.
# See spikes/cache_control_breakpoint_report.md for the spike that confirmed
# cache_control is not part of the cache key and doesn't need normalization.


# ── Rule 6: Tool Sorting ─────────────────────────────────────────────────


class TestNormalizeToolOrder:
    def test_sorts_tools_alphabetically(self):
        body = _make_body(tools=[
            {"name": "zebra", "description": "z"},
            {"name": "apple", "description": "a"},
            {"name": "middle", "description": "m"},
        ])
        count = cache_proxy.normalize_tool_order(body)
        assert count == 1
        assert [t["name"] for t in body["tools"]] == ["apple", "middle", "zebra"]

    def test_already_sorted(self):
        body = _make_body(tools=[
            {"name": "apple", "description": "a"},
            {"name": "middle", "description": "m"},
            {"name": "zebra", "description": "z"},
        ])
        count = cache_proxy.normalize_tool_order(body)
        assert count == 0

    def test_idempotent(self):
        body = _make_body(tools=[
            {"name": "z", "description": ""},
            {"name": "a", "description": ""},
        ])
        cache_proxy.normalize_tool_order(body)
        body_copy = copy.deepcopy(body)
        cache_proxy.normalize_tool_order(body)
        assert body == body_copy

    def test_no_tools(self):
        body = _make_body()
        count = cache_proxy.normalize_tool_order(body)
        assert count == 0

    def test_empty_tools(self):
        body = _make_body(tools=[])
        count = cache_proxy.normalize_tool_order(body)
        assert count == 0

    def test_single_tool(self):
        body = _make_body(tools=[{"name": "only", "description": ""}])
        count = cache_proxy.normalize_tool_order(body)
        assert count == 0

    def test_tools_without_name(self):
        body = _make_body(tools=[
            {"description": "no name"},
            {"name": "has_name", "description": ""},
        ])
        # Should not crash — tools without name get "" as sort key
        # Already in sorted order ("" < "has_name"), so count is 0
        count = cache_proxy.normalize_tool_order(body)
        assert count == 0

    def test_tools_without_name_reordered(self):
        body = _make_body(tools=[
            {"name": "has_name", "description": ""},
            {"description": "no name"},
        ])
        # "" sorts before "has_name", so reorder happens
        count = cache_proxy.normalize_tool_order(body)
        assert count == 1
        assert body["tools"][0] == {"description": "no name"}

    def test_stats_updated(self):
        body = _make_body(tools=[
            {"name": "b", "description": ""},
            {"name": "a", "description": ""},
        ])
        cache_proxy.normalize_tool_order(body)
        assert cache_proxy.stats["tools_sorted"] == 1


# ── Rule 7: Metadata ─────────────────────────────────────────────────────


class TestNormalizeMetadata:
    def test_normalizes_session_id(self):
        body = _make_body(metadata={
            "user_id": "user_abc_session_774524ca-8112-4a2e-9c3b-1234567890ab"
        })
        count = cache_proxy.normalize_metadata(body)
        assert count == 1
        assert body["metadata"]["user_id"] == "user_abc_session_0"

    def test_idempotent(self):
        body = _make_body(metadata={"user_id": "user_abc_session_0"})
        count = cache_proxy.normalize_metadata(body)
        assert count == 0

    def test_no_metadata(self):
        body = _make_body()
        count = cache_proxy.normalize_metadata(body)
        assert count == 0

    def test_empty_metadata(self):
        body = _make_body(metadata={})
        count = cache_proxy.normalize_metadata(body)
        assert count == 0

    def test_no_user_id(self):
        body = _make_body(metadata={"other_field": "value"})
        count = cache_proxy.normalize_metadata(body)
        assert count == 0

    def test_user_id_without_session(self):
        body = _make_body(metadata={"user_id": "user_abc"})
        count = cache_proxy.normalize_metadata(body)
        assert count == 0  # No _session_ pattern to match

    def test_different_sessions_normalize_same(self):
        body1 = _make_body(metadata={
            "user_id": "user_abc_session_aaaa-bbbb-cccc-dddd"
        })
        body2 = _make_body(metadata={
            "user_id": "user_abc_session_1111-2222-3333-4444"
        })
        cache_proxy.normalize_metadata(body1)
        cache_proxy.normalize_metadata(body2)
        assert body1["metadata"]["user_id"] == body2["metadata"]["user_id"]

    def test_stats_updated(self):
        body = _make_body(metadata={
            "user_id": "user_abc_session_aaa-bbb"
        })
        cache_proxy.normalize_metadata(body)
        assert cache_proxy.stats["metadata_normalized"] == 1


# ── normalize_request (all rules combined) ────────────────────────────────


class TestNormalizeRequest:
    def test_applies_all_rules(self):
        """A body with all normalizable aspects gets all 7 rules applied."""
        body = _make_body(
            system=[
                _billing_block(),
                _text_block("Claude Code preamble"),
                _system_block_with_git_status("M dirty.py"),
            ],
            messages=[
                _user_msg("bare string user msg"),
                _assistant_msg("reply"),
                _user_msg([
                    _text_block("question"),
                    _dynamic_reminder_block(),
                    _skill_block(),
                ]),
            ],
            tools=[
                {"name": "z_tool", "description": ""},
                {"name": "a_tool", "description": ""},
            ],
            metadata={"user_id": "user_x_session_abc-def-123"},
        )

        result_body, info = cache_proxy.normalize_request(body)

        # Rule 1: billing normalized
        assert info["billing"] >= 1
        assert result_body["system"][0]["text"] == cache_proxy.FIXED_BILLING_HEADER

        # Rule 2: string converted
        assert info["strings"] >= 1

        # Rule 3: skill listing stripped entirely
        assert info["skill"]["action"] == "stripped"
        # No message content should contain the skill marker
        for msg in result_body["messages"]:
            if msg.get("role") != "user":
                continue
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    assert cache_proxy.SKILL_MARKER not in block.get("text", "")

        # Rule 4: dynamic reminders stripped
        assert info["reminders"] >= 1

        # Rule 5: git status normalized
        assert info["git_status"] >= 1
        assert result_body["system"][2]["text"].endswith("gitStatus: normalized")

        # cache_control: not touched (CC handles natively)
        assert "cache_control" not in info

        # Rule 6: tools sorted
        assert info["tools"] >= 1
        assert result_body["tools"][0]["name"] == "a_tool"

        # Rule 7: metadata normalized
        assert info["metadata"] >= 1
        assert result_body["metadata"]["user_id"] == "user_x_session_0"

        # Overall action
        assert info["action"] == "normalized"

    def test_already_normalized_body(self):
        """A body without skill listing and nothing else to normalize."""
        body = _make_body(
            system=[{"type": "text", "text": cache_proxy.FIXED_BILLING_HEADER}],
            messages=[
                _user_msg([
                    _text_block("hello"),
                ]),
            ],
            tools=[
                {"name": "a", "description": ""},
                {"name": "b", "description": ""},
            ],
            metadata={"user_id": "user_x_session_0"},
        )

        _, info = cache_proxy.normalize_request(body)
        # No skill to strip, no other normalizations needed
        assert info["skill"]["action"] == "not_found"
        assert info["strings"] == 0
        assert info["reminders"] == 0
        assert info["tools"] == 0
        assert info["metadata"] == 0

    def test_idempotent_full(self):
        """Applying normalize_request twice should produce the same result."""
        body = _make_body(
            system=[
                _billing_block(),
                _text_block("preamble"),
                _system_block_with_git_status("M file.py"),
            ],
            messages=[
                _user_msg("bare string"),
                _user_msg([_skill_block(), _dynamic_reminder_block(), _text_block("q")]),
            ],
            tools=[{"name": "b"}, {"name": "a"}],
            metadata={"user_id": "user_x_session_abc-123"},
        )

        cache_proxy.normalize_request(body)
        body_after_first = copy.deepcopy(body)

        # Reset stats to avoid confusion
        for key in cache_proxy.stats:
            cache_proxy.stats[key] = 0

        cache_proxy.normalize_request(body)
        assert body == body_after_first

    def test_empty_body(self):
        """An empty body should not crash."""
        body = {}
        _, info = cache_proxy.normalize_request(body)
        assert info["billing"] == 0
        assert info["strings"] == 0
        assert info["skill"]["action"] == "no_messages"

    def test_action_normalized_on_any_change(self):
        """Action should be 'normalized' if ANY rule fires."""
        # Only tools need sorting, everything else is clean
        body = _make_body(
            messages=[_user_msg([_text_block("hello")])],
            tools=[{"name": "b"}, {"name": "a"}],
        )
        _, info = cache_proxy.normalize_request(body)
        assert info["action"] == "normalized"

    def test_action_not_found_when_nothing_to_do(self):
        """When there's nothing to normalize and no skill listing found."""
        body = _make_body(
            messages=[_user_msg([_text_block("hello")])],
        )
        _, info = cache_proxy.normalize_request(body)
        assert info["skill"]["action"] == "not_found"


# ── parse_sse_usage ───────────────────────────────────────────────────────


class TestParseSSEUsage:
    def test_extracts_usage_from_message_start(self):
        event = {
            "type": "message_start",
            "message": {
                "usage": {
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                    "input_tokens": 3,
                }
            }
        }
        chunk = f"event: message_start\ndata: {json.dumps(event)}\n\n".encode()
        usage = cache_proxy.parse_sse_usage([chunk])
        assert usage["cache_read_input_tokens"] == 100

    def test_no_message_start(self):
        chunk = b"event: content_block_start\ndata: {\"type\": \"content_block_start\"}\n\n"
        usage = cache_proxy.parse_sse_usage([chunk])
        assert usage == {}

    def test_empty_chunks(self):
        usage = cache_proxy.parse_sse_usage([])
        assert usage == {}

    def test_malformed_json(self):
        chunk = b"data: not valid json\n\n"
        usage = cache_proxy.parse_sse_usage([chunk])
        assert usage == {}


# ── Model Routing ────────────────────────────────────────────────────────


class TestStripContextSuffix:
    def test_strips_1m(self):
        assert cache_proxy._strip_context_suffix("claude-opus-4-6[1m]") == "claude-opus-4-6"

    def test_strips_200k(self):
        assert cache_proxy._strip_context_suffix("gpt-5.5[200k]") == "gpt-5.5"

    def test_strips_case_insensitive(self):
        assert cache_proxy._strip_context_suffix("gemini-3.1-flash-lite-preview[1M]") == "gemini-3.1-flash-lite-preview"
        assert cache_proxy._strip_context_suffix("gpt-5.4-mini[50K]") == "gpt-5.4-mini"

    def test_no_suffix_passthrough(self):
        assert cache_proxy._strip_context_suffix("claude-opus-4-6") == "claude-opus-4-6"

    def test_empty_string(self):
        assert cache_proxy._strip_context_suffix("") == ""


class TestNormalizeModelName:
    def test_resolves_claude_shorthands(self):
        assert cache_proxy._normalize_model_name("sonnet") == "claude-sonnet-4-6"
        assert cache_proxy._normalize_model_name("haiku[1m]") == "claude-haiku-4-5"

    def test_resolves_non_claude_shorthands(self):
        assert cache_proxy._normalize_model_name("gpt") == "gpt-5.5"
        assert cache_proxy._normalize_model_name("gpt[200k]") == "gpt-5.5"


class TestResolveUpstream:
    def test_claude_routes_to_anthropic(self):
        assert cache_proxy._resolve_upstream("claude-opus-4-6") == cache_proxy.ANTHROPIC_UPSTREAM

    def test_claude_with_suffix_routes_to_anthropic(self):
        assert cache_proxy._resolve_upstream("claude-opus-4-6[1m]") == cache_proxy.ANTHROPIC_UPSTREAM

    def test_claude_shorthand_routes_to_anthropic(self):
        assert cache_proxy._resolve_upstream("claude") == cache_proxy.ANTHROPIC_UPSTREAM

    def test_anthropic_tier_shorthands_route_to_anthropic(self):
        assert cache_proxy._resolve_upstream("sonnet") == cache_proxy.ANTHROPIC_UPSTREAM
        assert cache_proxy._resolve_upstream("haiku[1m]") == cache_proxy.ANTHROPIC_UPSTREAM

    def test_gpt_routes_to_cli_proxy(self):
        assert cache_proxy._resolve_upstream("gpt-5.5") == cache_proxy.CLI_PROXY_UPSTREAM

    def test_gemini_routes_to_cli_proxy(self):
        assert cache_proxy._resolve_upstream("gemini-3.1-flash-lite-preview[1m]") == cache_proxy.CLI_PROXY_UPSTREAM

    def test_empty_string_routes_to_cli_proxy(self):
        # Empty model string is non-Claude, goes to CLI proxy
        assert cache_proxy._resolve_upstream("") == cache_proxy.CLI_PROXY_UPSTREAM

    def test_unknown_model_routes_to_cli_proxy(self):
        assert cache_proxy._resolve_upstream("deepseek-v4") == cache_proxy.CLI_PROXY_UPSTREAM


# ── Rule 8: Tool schema sanitization (non-Claude only) ─────────────────


class TestFixArrayItemsRecursive:
    """Unit tests for _fix_array_items_recursive."""

    def test_array_without_items_gets_fixed(self):
        schema = {"type": "array"}
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 1
        assert schema["items"] == {}

    def test_array_with_items_untouched(self):
        schema = {"type": "array", "items": {"type": "string"}}
        original = copy.deepcopy(schema)
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 0
        assert schema == original

    def test_non_array_type_untouched(self):
        schema = {"type": "string"}
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 0
        assert "items" not in schema

    def test_nested_in_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
                "name": {"type": "string"},
            },
        }
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 1
        assert schema["properties"]["tags"]["items"] == {}
        assert "items" not in schema["properties"]["name"]

    def test_nested_in_anyof(self):
        """Exact reproduction of the tasknotes_update_task blockedBy schema."""
        schema = {
            "anyOf": [
                {"type": "array"},
                {"type": "null"},
            ],
        }
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 1
        assert schema["anyOf"][0]["items"] == {}
        assert "items" not in schema["anyOf"][1]

    def test_deeply_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {
                            "oneOf": [
                                {"type": "array"},
                                {"type": "null"},
                            ]
                        }
                    }
                }
            }
        }
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 1
        assert schema["properties"]["outer"]["properties"]["inner"]["oneOf"][0]["items"] == {}

    def test_multiple_arrays_fixed(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "array"},
                "b": {"type": "array"},
                "c": {"type": "array", "items": {"type": "number"}},
            },
        }
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 2
        assert schema["properties"]["a"]["items"] == {}
        assert schema["properties"]["b"]["items"] == {}
        assert schema["properties"]["c"]["items"] == {"type": "number"}

    def test_non_dict_input(self):
        assert cache_proxy._fix_array_items_recursive("not a dict") == 0
        assert cache_proxy._fix_array_items_recursive(42) == 0
        assert cache_proxy._fix_array_items_recursive(None) == 0

    def test_idempotent(self):
        schema = {"anyOf": [{"type": "array"}, {"type": "null"}]}
        cache_proxy._fix_array_items_recursive(schema)
        schema_after_first = copy.deepcopy(schema)
        count = cache_proxy._fix_array_items_recursive(schema)
        assert count == 0
        assert schema == schema_after_first


class TestSanitizeToolSchemas:
    """Unit tests for sanitize_tool_schemas_for_openai."""

    def test_exact_tasknotes_update_task_schema(self):
        """The exact schema that breaks GPT sessions."""
        body = {
            "tools": [
                {
                    "name": "tasknotes_update_task",
                    "input_schema": {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "blockedBy": {
                                "anyOf": [
                                    {"type": "array"},
                                    {"type": "null"},
                                ],
                            },
                            "title": {"type": "string"},
                        },
                        "required": ["id"],
                    },
                }
            ]
        }
        count = cache_proxy.sanitize_tool_schemas_for_openai(body)
        assert count == 1
        blocked_by = body["tools"][0]["input_schema"]["properties"]["blockedBy"]
        assert blocked_by["anyOf"][0]["items"] == {}
        assert blocked_by["anyOf"][1] == {"type": "null"}

    def test_clean_tools_untouched(self):
        body = {
            "tools": [
                {
                    "name": "read_file",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "lines": {"type": "array", "items": {"type": "integer"}},
                        },
                    },
                }
            ]
        }
        original = copy.deepcopy(body)
        count = cache_proxy.sanitize_tool_schemas_for_openai(body)
        assert count == 0
        assert body == original

    def test_no_tools(self):
        assert cache_proxy.sanitize_tool_schemas_for_openai({}) == 0
        assert cache_proxy.sanitize_tool_schemas_for_openai({"tools": []}) == 0
        assert cache_proxy.sanitize_tool_schemas_for_openai({"tools": None}) == 0

    def test_multiple_tools_with_mixed_schemas(self):
        body = {
            "tools": [
                {
                    "name": "tool_a",
                    "input_schema": {
                        "type": "object",
                        "properties": {"arr": {"type": "array"}},
                    },
                },
                {
                    "name": "tool_b",
                    "input_schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
                {
                    "name": "tool_c",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "anyOf": [
                                    {"type": "array"},
                                    {"type": "null"},
                                ]
                            }
                        },
                    },
                },
            ]
        }
        count = cache_proxy.sanitize_tool_schemas_for_openai(body)
        assert count == 2  # tool_a.arr + tool_c.items.anyOf[0]

    def test_stats_incremented(self):
        body = {
            "tools": [
                {
                    "name": "test",
                    "input_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "array"}},
                    },
                }
            ]
        }
        cache_proxy.sanitize_tool_schemas_for_openai(body)
        assert cache_proxy.stats["schemas_sanitized"] == 1
