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


# ── Rule 3: Skill Listing Position ───────────────────────────────────────


class TestNormalizeSkillListing:
    def test_moves_skill_from_last_to_first(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
            _user_msg([_text_block("question"), _skill_block()]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "moved"
        # Skill should now be at messages[0].content[0]
        assert cache_proxy.SKILL_MARKER in body["messages"][0]["content"][0]["text"]

    def test_already_at_target(self):
        body = _make_body(messages=[
            _user_msg([_skill_block(), _text_block("hello")]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "already_at_target"

    def test_idempotent(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
            _user_msg([_skill_block()]),
        ])
        cache_proxy.normalize_skill_listing(body)
        body_copy = copy.deepcopy(body)
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "already_at_target"
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

    def test_skill_in_middle_message(self):
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _user_msg([_skill_block()]),
            _user_msg([_text_block("third")]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "moved"
        assert cache_proxy.SKILL_MARKER in body["messages"][0]["content"][0]["text"]
        # msg[1] had only skill_block → empty after extraction → removed
        # Result: 2 messages (first with skill prepended, third)
        assert len(body["messages"]) == 2

    def test_removes_empty_message_after_extraction(self):
        """If extracting the skill block leaves an empty content array, remove the message."""
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _user_msg([_skill_block()]),  # Only has skill block
        ])
        cache_proxy.normalize_skill_listing(body)
        # The second message should be removed (was empty after extraction)
        # Now we have just one message with skill + "first"
        assert len(body["messages"]) == 1
        assert len(body["messages"][0]["content"]) == 2  # skill + "first"

    def test_preserves_other_blocks_in_source_message(self):
        """If source message had other blocks, they remain after skill extraction."""
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _user_msg([_text_block("other"), _skill_block(), _text_block("more")]),
        ])
        cache_proxy.normalize_skill_listing(body)
        # Skill moved to msg[0], source msg still has "other" and "more"
        assert len(body["messages"]) == 2
        src_content = body["messages"][1]["content"]
        assert len(src_content) == 2
        assert src_content[0]["text"] == "other"
        assert src_content[1]["text"] == "more"

    def test_skill_in_assistant_message_ignored(self):
        """Skill listing in assistant messages should not be moved."""
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
            _assistant_msg([_skill_block()]),
        ])
        result = cache_proxy.normalize_skill_listing(body)
        assert result["action"] == "not_found"

    def test_stats_updated_moved(self):
        body = _make_body(messages=[
            _user_msg([_text_block("a")]),
            _user_msg([_skill_block()]),
        ])
        cache_proxy.normalize_skill_listing(body)
        assert cache_proxy.stats["skill_moved"] == 1

    def test_stats_updated_ok(self):
        body = _make_body(messages=[
            _user_msg([_skill_block()]),
        ])
        cache_proxy.normalize_skill_listing(body)
        assert cache_proxy.stats["skill_ok"] == 1

    def test_stats_updated_missing(self):
        body = _make_body(messages=[_user_msg([_text_block("no skill")])])
        cache_proxy.normalize_skill_listing(body)
        assert cache_proxy.stats["skill_missing"] == 1


# ── _is_strippable_system_reminder ────────────────────────────────────────


class TestIsStrippableSystemReminder:
    def test_dynamic_reminder_is_strippable(self):
        block = _dynamic_reminder_block("changed_files")
        assert cache_proxy._is_strippable_system_reminder(block) is True

    def test_skill_listing_not_strippable(self):
        block = _skill_block()
        assert cache_proxy._is_strippable_system_reminder(block) is False

    def test_claudemd_not_strippable(self):
        block = _claudemd_block()
        assert cache_proxy._is_strippable_system_reminder(block) is False

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
        """A block with both skill marker AND system-reminder should NOT be stripped."""
        block = _text_block(
            f"<system-reminder>\n{cache_proxy.SKILL_MARKER}\nsome other stuff\n</system-reminder>"
        )
        assert cache_proxy._is_strippable_system_reminder(block) is False

    def test_block_with_both_claudemd_and_reminder(self):
        """A block with both CLAUDE.md marker AND system-reminder should NOT be stripped."""
        block = _text_block(
            f"<system-reminder>\n{cache_proxy.CLAUDEMD_MARKER}\nsome stuff\n</system-reminder>"
        )
        assert cache_proxy._is_strippable_system_reminder(block) is False


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

    def test_preserves_skill_listing(self):
        body = _make_body(messages=[
            _user_msg([_skill_block(), _dynamic_reminder_block()]),
        ])
        count = cache_proxy.strip_dynamic_reminders(body)
        assert count == 1
        assert len(body["messages"][0]["content"]) == 1
        assert cache_proxy.SKILL_MARKER in body["messages"][0]["content"][0]["text"]

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


# ── Rule 5: Cache Control ────────────────────────────────────────────────


class TestNormalizeCacheControl:
    def test_adds_cache_control_to_user_message(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello")]),
        ])
        count = cache_proxy.normalize_cache_control(body)
        assert count == 1
        assert body["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_no_ttl_in_cache_control(self):
        """Verify no TTL is set — the API determines TTL based on account eligibility."""
        body = _make_body(messages=[_user_msg([_text_block("hello")])])
        cache_proxy.normalize_cache_control(body)
        cc = body["messages"][0]["content"][-1]["cache_control"]
        assert "ttl" not in cc
        assert cc == {"type": "ephemeral"}

    def test_already_has_cache_control(self):
        body = _make_body(messages=[
            _user_msg([_text_block("hello", cache_control={"type": "ephemeral"})]),
        ])
        cache_proxy.normalize_cache_control(body)
        # After normalization, the block should still have cache_control
        cc = body["messages"][0]["content"][-1].get("cache_control")
        assert cc == {"type": "ephemeral"}

    def test_idempotent(self):
        body = _make_body(messages=[_user_msg([_text_block("hello")])])
        cache_proxy.normalize_cache_control(body)
        body_copy = copy.deepcopy(body)
        cache_proxy.normalize_cache_control(body)
        # Body should be identical after applying twice
        assert body == body_copy

    def test_adds_to_all_user_messages(self):
        body = _make_body(messages=[
            _user_msg([_text_block("first")]),
            _assistant_msg("reply"),
            _user_msg([_text_block("second")]),
            _user_msg([_text_block("third")]),
        ])
        count = cache_proxy.normalize_cache_control(body)
        assert count == 3
        for msg in body["messages"]:
            if msg["role"] == "user":
                assert "cache_control" in msg["content"][-1]

    def test_skips_assistant_messages(self):
        body = _make_body(messages=[_assistant_msg([_text_block("hi")])])
        count = cache_proxy.normalize_cache_control(body)
        assert count == 0

    def test_skips_empty_content_list(self):
        body = _make_body(messages=[_user_msg([])])
        count = cache_proxy.normalize_cache_control(body)
        assert count == 0

    def test_skips_non_dict_last_block(self):
        body = _make_body(messages=[{"role": "user", "content": ["just a string"]}])
        count = cache_proxy.normalize_cache_control(body)
        assert count == 0

    def test_adds_to_last_block_of_multi_block_content(self):
        body = _make_body(messages=[
            _user_msg([_text_block("first"), _text_block("second")]),
        ])
        cache_proxy.normalize_cache_control(body)
        # Only last block should have cache_control
        assert "cache_control" not in body["messages"][0]["content"][0]
        assert "cache_control" in body["messages"][0]["content"][1]

    def test_stats_updated(self):
        body = _make_body(messages=[_user_msg([_text_block("a")])])
        cache_proxy.normalize_cache_control(body)
        assert cache_proxy.stats["cc_added"] == 1


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
            system=[_billing_block()],
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

        # Rule 3: skill listing moved to msg[0].content[0]
        assert info["skill"]["action"] == "moved"
        assert cache_proxy.SKILL_MARKER in result_body["messages"][0]["content"][0]["text"]

        # Rule 4: dynamic reminders stripped
        assert info["reminders"] >= 1

        # Rule 5: cache_control added
        assert info["cache_control"] >= 1

        # Rule 6: tools sorted
        assert info["tools"] >= 1
        assert result_body["tools"][0]["name"] == "a_tool"

        # Rule 7: metadata normalized
        assert info["metadata"] >= 1
        assert result_body["metadata"]["user_id"] == "user_x_session_0"

        # Overall action
        assert info["action"] == "normalized"

    def test_already_normalized_body(self):
        """A body that's already fully normalized should report already_at_target or no changes."""
        body = _make_body(
            system=[{"type": "text", "text": cache_proxy.FIXED_BILLING_HEADER}],
            messages=[
                _user_msg([
                    _skill_block(),
                    _text_block("hello", cache_control={"type": "ephemeral"}),
                ]),
            ],
            tools=[
                {"name": "a", "description": ""},
                {"name": "b", "description": ""},
            ],
            metadata={"user_id": "user_x_session_0"},
        )

        _, info = cache_proxy.normalize_request(body)
        # Skill already at target, no other normalizations needed
        assert info["skill"]["action"] == "already_at_target"
        assert info["strings"] == 0
        assert info["reminders"] == 0
        assert info["tools"] == 0
        assert info["metadata"] == 0

    def test_idempotent_full(self):
        """Applying normalize_request twice should produce the same result."""
        body = _make_body(
            system=[_billing_block()],
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
            messages=[_user_msg([_text_block("hello", cache_control={"type": "ephemeral"})])],
            tools=[{"name": "b"}, {"name": "a"}],
        )
        _, info = cache_proxy.normalize_request(body)
        assert info["action"] == "normalized"

    def test_action_not_found_when_nothing_to_do(self):
        """When there's nothing to normalize and no skill listing found."""
        body = _make_body(
            messages=[_user_msg([_text_block("hello", cache_control={"type": "ephemeral"})])],
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
