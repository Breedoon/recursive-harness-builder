"""Unit tests for cache proxy streaming error handling.

Tests the _stream_upstream_with_retry method:
1. Non-2xx status code → falls back to non-streaming error response
2. Retryable status (408) then 200 → retry succeeds
3. Mid-stream connection drop → graceful SSE error event
4. Non-retryable error (401) → immediate non-streaming error
5. Connection-level failure with retry → retries then raises
"""
from __future__ import annotations

import io
import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

# Add src/ to path so we can import cache_proxy
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_proxy


# ── Helpers ──────────────────────────────────────────────────────────────


class FakeHeaders:
    """Minimal httpx-like Headers supporting multi_items()."""

    def __init__(self, items: list[tuple[str, str]] | None = None):
        self._items = items or [("content-type", "application/json")]

    def multi_items(self) -> list[tuple[str, str]]:
        return self._items


class FakeStreamResponse:
    """Mock httpx streaming response."""

    def __init__(self, status_code: int, body: bytes,
                 headers: list[tuple[str, str]] | None = None,
                 iter_error: Exception | None = None,
                 chunks: list[bytes] | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = FakeHeaders(headers)
        self._iter_error = iter_error
        self._chunks = chunks

    def read(self) -> bytes:
        return self._body

    def iter_raw(self) -> Iterator[bytes]:
        if self._chunks is not None:
            for chunk in self._chunks:
                yield chunk
            if self._iter_error:
                raise self._iter_error
        elif self._iter_error:
            raise self._iter_error
        else:
            yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeClient:
    """Mock httpx.Client that returns FakeStreamResponses in sequence."""

    def __init__(self, responses: list[FakeStreamResponse]):
        self._responses = responses
        self._call_count = 0

    def stream(self, method: str, url: str, **kwargs):
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]

    @property
    def call_count(self):
        return self._call_count


class FakeHandler:
    """Minimal stand-in for ProxyHandler, capturing HTTP output."""

    def __init__(self):
        self.wfile = io.BytesIO()
        self._response_code = None
        self._headers: list[tuple[str, str]] = []
        self._headers_ended = False

    def send_response(self, code: int):
        self._response_code = code

    def send_header(self, key: str, value: str):
        self._headers.append((key, value))

    def end_headers(self):
        self._headers_ended = True

    def get_response_code(self) -> int | None:
        return self._response_code

    def get_written_body(self) -> bytes:
        return self.wfile.getvalue()

    def get_header(self, name: str) -> str | None:
        for k, v in self._headers:
            if k.lower() == name.lower():
                return v
        return None


# ── Tests ────────────────────────────────────────────────────────────────


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_408_returns_non_streaming_error():
    """Upstream 408 → proxy returns clean non-streaming error response."""
    error_json = json.dumps({"error": {"type": "timeout", "message": "Request Timeout"}}).encode()
    client = FakeClient([
        FakeStreamResponse(408, error_json),
        FakeStreamResponse(408, error_json),
        FakeStreamResponse(408, error_json),
    ])
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry
    method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    # Should have retried _MAX_RETRIES times (3 total attempts)
    assert client.call_count == cache_proxy._MAX_RETRIES + 1
    # Final response should be 408 as non-streaming error
    assert handler.get_response_code() == 408
    body = handler.get_written_body()
    assert b"timeout" in body.lower() or b"Request Timeout" in body
    # Should have Content-Length header (non-streaming)
    assert handler.get_header("Content-Length") == str(len(error_json))


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_408_then_200_retry_succeeds():
    """Upstream 408 on first try, 200 on retry → streams successfully."""
    error_json = json.dumps({"error": "timeout"}).encode()
    sse_body = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start"}\n\n'
    )
    client = FakeClient([
        FakeStreamResponse(408, error_json),
        FakeStreamResponse(200, sse_body),
    ])
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry
    method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    assert client.call_count == 2
    assert handler.get_response_code() == 200
    body = handler.get_written_body()
    assert b"message_start" in body


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_mid_stream_connection_drop():
    """Stream drops mid-response → SSE error event written."""
    partial_chunks = [
        b'event: message_start\ndata: {"type":"message_start"}\n\n',
        b'event: content_block_start\ndata: {"type":"content_block_start"}\n\n',
    ]
    client = FakeClient([
        FakeStreamResponse(
            200, b"",
            chunks=partial_chunks,
            iter_error=ConnectionError("read timeout"),
        ),
    ])
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry
    method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    assert handler.get_response_code() == 200  # Headers already sent
    body = handler.get_written_body()
    # Should contain the partial chunks
    assert b"message_start" in body
    # Should contain SSE error event
    assert b"event: error" in body
    assert b"stream_error" in body


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_non_retryable_error_immediate():
    """Non-retryable error (401) → immediate non-streaming error, no retry."""
    error_json = json.dumps({"error": {"type": "auth", "message": "Invalid API key"}}).encode()
    client = FakeClient([
        FakeStreamResponse(401, error_json),
    ])
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry
    method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    # No retry for 401
    assert client.call_count == 1
    assert handler.get_response_code() == 401
    body = handler.get_written_body()
    assert b"Invalid API key" in body


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_connection_error_retried():
    """Connection-level error → retried, then re-raised if persistent."""

    class ErrorClient:
        def __init__(self):
            self.call_count = 0

        def stream(self, *args, **kwargs):
            self.call_count += 1
            raise ConnectionRefusedError("Connection refused")

    client = ErrorClient()
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry

    with pytest.raises(ConnectionRefusedError):
        method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    assert client.call_count == cache_proxy._MAX_RETRIES + 1


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_429_retried_then_succeeds():
    """429 Too Many Requests → retried and succeeds on second attempt."""
    error_json = json.dumps({"error": "rate limited"}).encode()
    sse_body = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n\n'
    )
    client = FakeClient([
        FakeStreamResponse(429, error_json),
        FakeStreamResponse(200, sse_body),
    ])
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry
    method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    assert client.call_count == 2
    assert handler.get_response_code() == 200


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_500_exhausted_retries():
    """500 error on all attempts → returns 500 as non-streaming error."""
    error_json = json.dumps({"error": "internal server error"}).encode()
    client = FakeClient([
        FakeStreamResponse(500, error_json),
        FakeStreamResponse(500, error_json),
        FakeStreamResponse(500, error_json),
    ])
    handler = FakeHandler()
    method = cache_proxy.ProxyHandler._stream_upstream_with_retry
    method(handler, client, "http://fake/v1/messages", b'{}', {}, "test")

    assert client.call_count == cache_proxy._MAX_RETRIES + 1
    assert handler.get_response_code() == 500
    body = handler.get_written_body()
    assert b"internal server error" in body


@patch("cache_proxy._RETRY_BACKOFF_BASE", 0.01)
def test_retryable_constants():
    """Verify the retryable status codes set includes expected codes."""
    expected = {408, 429, 500, 502, 503, 529}
    assert cache_proxy._RETRYABLE_STATUS_CODES == expected
    assert cache_proxy._MAX_RETRIES == 2
