"""Markdown-to-Telegram-HTML converter and message splitter.

Uses markdown-it-py for proper markdown parsing (not regex), then post-processes
the HTML into Telegram's supported subset. Telegram Bot API supports:
  <b>, <i>, <u>, <s>, <code>, <pre>, <a href="...">

Unsupported tags are converted:
  <h1>-<h6> -> <b>text</b> + newline
  <ul>/<ol>/<li> -> bullet/numbered lines
  <blockquote> -> "> " prefix per line
  <table> -> <pre> block (preserves alignment)
  <p> -> text + double newline
  <strong> -> <b>, <em> -> <i>

Message splitting respects:
  - 4000 char limit (not 4096 — HTML tags consume bytes)
  - Never splits inside fenced code blocks
  - Paragraph > line > word > hard boundaries
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

# Telegram message size limit (conservative to account for HTML entity expansion)
MAX_MESSAGE_LENGTH = 4000

# Shared markdown-it instance with table + strikethrough plugins
_md = MarkdownIt().enable("table").enable("strikethrough")


def md_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML.

    Uses markdown-it-py for parsing, then post-processes to Telegram's
    supported HTML subset.
    """
    html = _md.render(text)
    html = _convert_to_telegram_html(html)
    # markdown-it-py encodes " as &quot; (standard HTML). Telegram renders
    # this literally as visible &quot; text, so decode it back. Telegram's
    # HTML parser only requires <, >, and & to be encoded.
    html = html.replace("&quot;", '"')
    return html.strip()


def _convert_to_telegram_html(html: str) -> str:
    """Post-process standard HTML into Telegram-supported subset."""
    # Headings -> bold text + newline
    html = re.sub(r"<h[1-6]>(.*?)</h[1-6]>", r"<b>\1</b>\n", html)

    # strong -> b, em -> i
    html = html.replace("<strong>", "<b>").replace("</strong>", "</b>")
    html = html.replace("<em>", "<i>").replace("</em>", "</i>")

    # Tables -> <pre> blocks
    html = _convert_tables(html)

    # Lists -> bullet/numbered text
    html = _convert_lists(html)

    # Blockquotes -> "> " prefix
    html = _convert_blockquotes(html)

    # Code blocks: <pre><code class="...">content</code></pre> -> <pre>content</pre>
    html = re.sub(
        r'<pre><code(?:\s+class="[^"]*")?>(.*?)</code></pre>',
        r"<pre>\1</pre>",
        html,
        flags=re.DOTALL,
    )

    # Paragraphs -> text + double newline
    html = re.sub(r"<p>(.*?)</p>", r"\1\n\n", html, flags=re.DOTALL)

    # Clean up <hr> tags
    html = re.sub(r"<hr\s*/?>", "\n---\n", html)

    # Remove any remaining unsupported tags (safety net).
    # Telegram only supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a href="...">.
    # Strip everything else that wasn't handled by the converters above.
    html = re.sub(
        r"</?(?!(?:b|i|u|s|code|pre|a)\b)[a-zA-Z][^>]*>",
        "",
        html,
    )

    # Collapse triple+ newlines to double
    html = re.sub(r"\n{3,}", "\n\n", html)

    return html


def _convert_tables(html: str) -> str:
    """Convert HTML tables to <pre> blocks that preserve alignment."""
    table_pattern = re.compile(r"<table>(.*?)</table>", re.DOTALL)

    def _table_to_pre(match: re.Match) -> str:
        table_html = match.group(1)
        rows: list[list[str]] = []

        for row_match in re.finditer(r"<tr>(.*?)</tr>", table_html, re.DOTALL):
            cells = re.findall(r"<(?:th|td)>(.*?)</(?:th|td)>", row_match.group(1))
            rows.append(cells)

        if not rows:
            return ""

        # Calculate column widths
        col_widths: list[int] = []
        for row in rows:
            for i, cell in enumerate(row):
                width = len(cell.strip())
                if i >= len(col_widths):
                    col_widths.append(width)
                else:
                    col_widths[i] = max(col_widths[i], width)

        # Format rows
        lines: list[str] = []
        for row_idx, row in enumerate(rows):
            cells = []
            for i, cell in enumerate(row):
                width = col_widths[i] if i < len(col_widths) else 0
                cells.append(cell.strip().ljust(width))
            lines.append(" | ".join(cells))
            # Add separator after header row
            if row_idx == 0:
                seps = ["-" * w for w in col_widths]
                lines.append("-+-".join(seps))

        return "<pre>" + "\n".join(lines) + "</pre>"

    return table_pattern.sub(_table_to_pre, html)


def _convert_lists(html: str) -> str:
    """Convert HTML lists to plain text with bullet/number prefixes."""
    # Unordered lists
    def _ul_to_text(match: re.Match) -> str:
        items = re.findall(r"<li[^>]*>(.*?)</li>", match.group(1), re.DOTALL)
        return "\n".join(f"\u2022 {item.strip()}" for item in items) + "\n"

    html = re.sub(r"<ul[^>]*>(.*?)</ul>", _ul_to_text, html, flags=re.DOTALL)

    # Ordered lists — handle <ol start="N"> from markdown-it-py
    def _ol_to_text(match: re.Match) -> str:
        start_attr = re.search(r'start="(\d+)"', match.group(0))
        start = int(start_attr.group(1)) if start_attr else 1
        items = re.findall(r"<li[^>]*>(.*?)</li>", match.group(1), re.DOTALL)
        return "\n".join(f"{start+i}. {item.strip()}" for i, item in enumerate(items)) + "\n"

    html = re.sub(r"<ol[^>]*>(.*?)</ol>", _ol_to_text, html, flags=re.DOTALL)

    return html


def _convert_blockquotes(html: str) -> str:
    """Convert blockquotes to "> " prefixed text."""
    def _bq_to_text(match: re.Match) -> str:
        content = match.group(1).strip()
        # Remove inner <p> tags
        content = re.sub(r"</?p>", "", content).strip()
        lines = content.split("\n")
        return "\n".join(f"&gt; {line}" for line in lines) + "\n"

    return re.sub(r"<blockquote>(.*?)</blockquote>", _bq_to_text, html, flags=re.DOTALL)


def split_message(html: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit within the Telegram size limit.

    Splits at paragraph boundaries (\\n\\n) first, then at line boundaries (\\n),
    then at word boundaries as a last resort. Never splits inside <pre> blocks;
    if forced, closes and reopens the fence across chunks.
    """
    if len(html) <= limit:
        return [html]

    chunks: list[str] = []
    remaining = html

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Check if we're inside a <pre> block at the split point
        candidate = remaining[:limit]
        pre_opens = candidate.count("<pre>")
        pre_closes = candidate.count("</pre>")

        if pre_opens > pre_closes:
            # We'd split inside a code block.
            # Only keep the block whole if it fits within the limit.
            pre_end = remaining.find("</pre>")
            if pre_end != -1 and pre_end + len("</pre>") <= limit:
                split_pos = pre_end + len("</pre>")
                chunks.append(remaining[:split_pos])
                remaining = remaining[split_pos:].lstrip("\n")
                continue
            # Code block too long — force split with fence close/reopen
            split_pos = remaining.rfind("\n", 0, limit - len("</pre>"))
            if split_pos > 0:
                chunks.append(remaining[:split_pos] + "</pre>")
                remaining = "<pre>" + remaining[split_pos + 1:]
                continue

        # Try splitting at double newline (paragraph)
        split_pos = remaining.rfind("\n\n", 0, limit)
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2:]
            continue

        # Try splitting at single newline
        split_pos = remaining.rfind("\n", 0, limit)
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        # Try splitting at space (word boundary)
        split_pos = remaining.rfind(" ", 0, limit)
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        # Hard split as last resort
        chunks.append(remaining[:limit])
        remaining = remaining[limit:]

    return [c for c in chunks if c.strip()]
