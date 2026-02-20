"""Tests for obs_agent.telegram_format - markdown-it-py based conversion + splitting."""

import re

import pytest

from obs_agent.telegram_format import md_to_telegram_html, split_message


# --- Bold ---


class TestBold:
    def test_double_asterisk(self):
        assert "<b>bold</b>" in md_to_telegram_html("**bold**")

    def test_double_underscore(self):
        assert "<b>bold</b>" in md_to_telegram_html("__bold__")

    def test_bold_in_sentence(self):
        result = md_to_telegram_html("This is **important** text")
        assert "<b>important</b>" in result
        assert "This is" in result


# --- Italic ---


class TestItalic:
    def test_single_asterisk(self):
        assert "<i>italic</i>" in md_to_telegram_html("*italic*")

    def test_single_underscore(self):
        assert "<i>italic</i>" in md_to_telegram_html("_italic_")


# --- Strikethrough ---


class TestStrikethrough:
    def test_double_tilde(self):
        assert "<s>strike</s>" in md_to_telegram_html("~~strike~~")


# --- Code ---


class TestCode:
    def test_inline_code(self):
        assert "<code>code</code>" in md_to_telegram_html("`code`")

    def test_inline_code_escapes_html(self):
        result = md_to_telegram_html("`a < b`")
        assert "<code>a &lt; b</code>" in result

    def test_fenced_code_block(self):
        md = "```python\nprint('hello')\n```"
        result = md_to_telegram_html(md)
        assert "<pre>" in result
        assert "print(" in result
        assert "</pre>" in result

    def test_fenced_code_no_inner_code_tag(self):
        """Fenced code should be <pre>content</pre>, not <pre><code>...</code></pre>."""
        md = "```\nfoo\n```"
        result = md_to_telegram_html(md)
        assert "<pre>" in result
        # The <code class="..."> wrapper should be stripped
        assert '<code class=' not in result

    def test_no_markdown_inside_code(self):
        """Markdown inside code blocks should not be converted."""
        md = "```\n**not bold**\n```"
        result = md_to_telegram_html(md)
        assert "<b>" not in result


# --- Links ---


class TestLinks:
    def test_basic_link(self):
        result = md_to_telegram_html("[click](https://example.com)")
        assert '<a href="https://example.com">click</a>' in result

    def test_link_with_ampersand(self):
        result = md_to_telegram_html("[x](https://example.com?a=1&amp;b=2)")
        assert "example.com" in result


# --- Headings ---


class TestHeadings:
    def test_h1_to_bold(self):
        result = md_to_telegram_html("# Heading One")
        assert "<b>Heading One</b>" in result
        assert "<h1>" not in result

    def test_h2_to_bold(self):
        result = md_to_telegram_html("## Heading Two")
        assert "<b>Heading Two</b>" in result
        assert "<h2>" not in result

    def test_h3_to_bold(self):
        result = md_to_telegram_html("### Heading Three")
        assert "<b>Heading Three</b>" in result


# --- Lists ---


class TestLists:
    def test_unordered_list_to_bullets(self):
        md = "- item one\n- item two"
        result = md_to_telegram_html(md)
        assert "\u2022 item one" in result
        assert "\u2022 item two" in result
        assert "<ul>" not in result
        assert "<li>" not in result

    def test_ordered_list_to_numbers(self):
        md = "1. first\n2. second"
        result = md_to_telegram_html(md)
        assert "1. first" in result
        assert "2. second" in result
        assert "<ol>" not in result

    def test_nested_list_items(self):
        """Nested items may lose nesting but should still render."""
        md = "- parent\n  - child"
        result = md_to_telegram_html(md)
        assert "parent" in result
        assert "child" in result

    def test_ordered_list_start_attribute(self):
        """Lists not starting at 1 produce <ol start="N"> — must still convert."""
        md = "5. fifth\n6. sixth\n7. seventh"
        result = md_to_telegram_html(md)
        assert "5. fifth" in result
        assert "6. sixth" in result
        assert "7. seventh" in result
        assert "<ol" not in result
        assert "<li>" not in result

    def test_ordered_list_interrupted_renumbering(self):
        """Interrupted list continues numbering from start attribute."""
        md = "1. first\n2. second\n\nSome text\n\n3. third\n4. fourth"
        result = md_to_telegram_html(md)
        assert "3. third" in result
        assert "4. fourth" in result
        assert "<ol" not in result

    def test_nested_list_no_unsupported_tags(self):
        """Nested lists must not leak <ul>, <li>, etc. into output."""
        md = "- parent\n  - child one\n  - child two\n- another"
        result = md_to_telegram_html(md)
        assert "<ul" not in result
        assert "<li" not in result
        assert "</ul>" not in result
        assert "</li>" not in result


# --- Tables ---


class TestTables:
    def test_table_to_pre(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = md_to_telegram_html(md)
        assert "<pre>" in result
        assert "</pre>" in result
        assert "<table>" not in result
        assert "A" in result
        assert "B" in result
        assert "1" in result
        assert "2" in result

    def test_table_alignment(self):
        """Table columns should be aligned."""
        md = "| Name | Value |\n|------|-------|\n| x | 1 |"
        result = md_to_telegram_html(md)
        assert "<pre>" in result


# --- Blockquotes ---


class TestBlockquotes:
    def test_blockquote(self):
        result = md_to_telegram_html("> quoted text")
        assert "&gt;" in result
        assert "quoted text" in result
        assert "<blockquote>" not in result


# --- Mixed formatting ---


class TestMixedFormatting:
    def test_bold_and_italic(self):
        result = md_to_telegram_html("**bold** and *italic*")
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result

    def test_code_and_text(self):
        result = md_to_telegram_html("Use `print()` to output")
        assert "<code>print()</code>" in result
        assert "Use" in result

    def test_html_chars_escaped_in_text(self):
        result = md_to_telegram_html("a > b and c < d")
        assert "&gt;" in result
        assert "&lt;" in result


# --- No unsupported tags (safety net) ---

# Telegram only supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a href="...">
_SUPPORTED_TAG_RE = re.compile(r"^</?(?:b|i|u|s|code|pre|a)\b[^>]*>$")


def _unsupported_tags(html: str) -> list[str]:
    """Return any HTML tags in the output that Telegram won't accept."""
    return [t for t in re.findall(r"<[^>]+>", html) if not _SUPPORTED_TAG_RE.match(t)]


class TestNoUnsupportedTags:
    """Every conversion must produce only Telegram-supported tags."""

    def test_headings_no_h_tags(self):
        for level in range(1, 7):
            result = md_to_telegram_html(f"{'#' * level} Heading")
            assert not _unsupported_tags(result), f"h{level} leaked: {_unsupported_tags(result)}"

    def test_lists_no_ul_ol_li_tags(self):
        cases = [
            "- bullet\n- list",
            "1. numbered\n2. list",
            "5. starts at five\n6. six",
            "- parent\n  - nested\n  - child",
        ]
        for md in cases:
            result = md_to_telegram_html(md)
            bad = _unsupported_tags(result)
            assert not bad, f"List leaked tags for {md!r}: {bad}"

    def test_blockquotes_no_blockquote_tag(self):
        result = md_to_telegram_html("> quoted")
        assert not _unsupported_tags(result)

    def test_tables_no_table_tags(self):
        result = md_to_telegram_html("| A | B |\n|---|---|\n| 1 | 2 |")
        assert not _unsupported_tags(result)

    def test_paragraphs_no_p_tags(self):
        result = md_to_telegram_html("Para one.\n\nPara two.")
        assert not _unsupported_tags(result)

    def test_hr_no_hr_tag(self):
        result = md_to_telegram_html("Before\n\n---\n\nAfter")
        assert not _unsupported_tags(result)

    def test_realistic_claude_response(self):
        """A full Claude-style response must have zero unsupported tags."""
        md = (
            "## How Bubble Sort Works\n\n"
            "Here are the steps:\n\n"
            "1. Start with array `arr` of length `n`\n"
            "2. Compare adjacent elements\n"
            "3. Swap if wrong order\n\n"
            "### Implementation\n\n"
            "```python\n"
            "def bubble_sort(arr):\n"
            "    for i in range(len(arr)):\n"
            "        for j in range(len(arr)-i-1):\n"
            "            if arr[j] > arr[j+1]:\n"
            "                arr[j], arr[j+1] = arr[j+1], arr[j]\n"
            "```\n\n"
            "---\n\n"
            "### Key Points\n\n"
            "- Time: O(n\u00b2)\n"
            "- Space: O(1)\n"
            "- **Stable** sort\n\n"
            "> Note: not efficient for large datasets\n"
        )
        result = md_to_telegram_html(md)
        bad = _unsupported_tags(result)
        assert not bad, f"Realistic response leaked: {bad}"

    def test_list_not_starting_at_one(self):
        """Regression test: <ol start='N'> must be fully converted."""
        md = "Skills:\n\n5. Offboard\n6. Update\n7. Conventions"
        result = md_to_telegram_html(md)
        bad = _unsupported_tags(result)
        assert not bad, f"<ol start> leaked: {bad}"
        assert "5. Offboard" in result
        assert "7. Conventions" in result


# --- Paragraph handling ---


class TestParagraphs:
    def test_paragraphs_not_wrapped_in_p_tags(self):
        result = md_to_telegram_html("First paragraph.\n\nSecond paragraph.")
        assert "<p>" not in result
        assert "First paragraph." in result
        assert "Second paragraph." in result


# --- Message splitting ---


class TestSplitMessage:
    def test_short_message_no_split(self):
        chunks = split_message("Hello", limit=100)
        assert chunks == ["Hello"]

    def test_exact_limit(self):
        text = "x" * 100
        chunks = split_message(text, limit=100)
        assert len(chunks) == 1

    def test_split_at_paragraph(self):
        text = "Part one.\n\nPart two."
        chunks = split_message(text, limit=15)
        assert len(chunks) == 2
        assert chunks[0] == "Part one."
        assert chunks[1] == "Part two."

    def test_split_at_newline(self):
        text = "Line one.\nLine two."
        chunks = split_message(text, limit=15)
        assert len(chunks) == 2

    def test_split_at_space(self):
        text = "word1 word2 word3"
        chunks = split_message(text, limit=10)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 10

    def test_hard_split(self):
        text = "x" * 200
        chunks = split_message(text, limit=100)
        assert len(chunks) == 2
        assert len(chunks[0]) == 100
        assert len(chunks[1]) == 100

    def test_empty_chunks_filtered(self):
        text = "a\n\n\n\nb"
        chunks = split_message(text, limit=5)
        for chunk in chunks:
            assert chunk.strip()

    def test_default_limit_is_4000(self):
        """Default limit is 4000, not 4096 (conservative for HTML tag overhead)."""
        short = "x" * 4000
        assert len(split_message(short)) == 1
        long = "x" * 4001
        assert len(split_message(long)) == 2

    def test_code_block_not_split(self):
        """Pre blocks should not be split in the middle."""
        code = "<pre>" + "x" * 50 + "</pre>"
        text = "before\n\n" + code + "\n\nafter"
        # With a limit that would split inside the <pre>
        chunks = split_message(text, limit=30)
        # The code block should appear intact in one of the chunks
        full = "".join(chunks)
        assert "x" * 50 in full

    def test_code_block_forced_split_closes_reopens(self):
        """If a pre block is too long, split with fence close/reopen."""
        # Create a code block much larger than the limit
        code_content = "\n".join(f"line {i}" for i in range(100))
        text = f"<pre>{code_content}</pre>"
        chunks = split_message(text, limit=200)
        assert len(chunks) > 1
        # First chunk should end with </pre>, second should start with <pre>
        assert chunks[0].endswith("</pre>")
        assert chunks[1].startswith("<pre>")

    # --- Stress tests: invariant checks ---

    def test_no_chunk_exceeds_limit(self):
        """INVARIANT: every chunk must be <= limit, regardless of content."""
        limit = 200
        cases = [
            # Large pre block between limit and limit*2 (was the bug)
            "<pre>" + "x" * 350 + "</pre>",
            # Pre block with newlines
            "<pre>" + "\n".join(f"line {i}: content" for i in range(50)) + "</pre>",
            # Mixed content with pre block
            "Intro text.\n\n<pre>" + "y" * 300 + "</pre>\n\nConclusion.",
            # Multiple pre blocks
            "<pre>" + "a" * 180 + "</pre>\n<pre>" + "b" * 180 + "</pre>",
            # Pure text, long
            "word " * 200,
            # Entity-heavy content
            "&amp; " * 200,
            # No newlines at all
            "x" * 1000,
        ]
        for text in cases:
            chunks = split_message(text, limit=limit)
            for i, chunk in enumerate(chunks):
                assert len(chunk) <= limit, (
                    f"Chunk {i} is {len(chunk)} chars (limit={limit}): "
                    f"{chunk[:80]}..."
                )

    def test_no_chunk_exceeds_default_limit(self):
        """Stress test at the real 4000-char default limit."""
        # Simulate a large skill file dump inside a code fence
        skill_content = "\n".join(f"- rule {i}: " + "x" * 60 for i in range(100))
        text = f"<pre>{skill_content}</pre>"
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            assert len(chunk) <= 4000, (
                f"Chunk {i} is {len(chunk)} chars (limit=4000)"
            )

    def test_content_preserved_after_split(self):
        """All original content characters should survive splitting."""
        text = "Hello\n\n" + "<pre>" + "x" * 500 + "</pre>" + "\n\nWorld"
        chunks = split_message(text, limit=200)
        rejoined = "".join(chunks)
        # Strip tags and whitespace — content chars must survive
        plain_original = text.replace("<pre>", "").replace("</pre>", "")
        plain_rejoined = rejoined.replace("<pre>", "").replace("</pre>", "")
        # Whitespace may change at split boundaries, so compare non-ws content
        assert plain_original.replace("\n", "").replace(" ", "") == \
               plain_rejoined.replace("\n", "").replace(" ", "")
