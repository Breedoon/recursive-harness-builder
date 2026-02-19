# Telegram HTML Format

## Steps
1. Send: "Show me a Python function that implements bubble sort. Use markdown formatting: bold headings, a numbered list explaining the algorithm steps, inline code for variable names like `arr` and `n`, and a fenced code block for the implementation."
   Wait: 90

## Criteria
- The response contains a Python code block with a bubble sort implementation (contains `def` and loop constructs)
- The response includes formatted text — HTML tags like <b>, <code>, <pre>, or their visual equivalents (bold text, code formatting)
- The code is syntactically plausible Python (contains keywords like def, for, if, return, or similar)
- The response is not garbled with raw HTML entities visible to the user (no literal &lt; &amp; &gt; showing as text)
- The response does NOT contain raw HTML tags visible as literal text. Specifically, the user must NOT see literal strings like `<b>`, `</b>`, `<ol>`, `<li>`, `</li>`, `<code>`, `</code>`, `<pre>`, `</pre>`, `<ol start=`, or any other angle-bracket HTML tags as plain text in the message. If any HTML tag syntax is visible as text (rather than being rendered as formatting), this criterion FAILS.
- The response includes some explanatory text in addition to the code (not just a bare code block)
