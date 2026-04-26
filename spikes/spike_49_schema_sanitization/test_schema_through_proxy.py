#!/usr/bin/env python3
"""Integration test: Schema sanitization through cache proxy → GPT.

Starts a cache proxy (with the schema sanitization fix), sends a request
with the exact broken tasknotes_update_task schema to GPT via the proxy,
and verifies it succeeds (no 400 error).

Also sends a Claude request with the same schema to verify Claude is untouched.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ── Config ──────────────────────────────────────────────────────────────

PROXY_PORT = 18950  # Use a different port to avoid conflicts
PROXY_SCRIPT = Path(__file__).resolve().parents[2] / "src" / "cache_proxy.py"
CLI_PROXY_BASE_URL = os.environ.get("CLI_PROXY_BASE_URL", "http://127.0.0.1:8317")
CLI_PROXY_API_KEY = os.environ.get("CLI_PROXY_API_KEY", "sk-anything")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# The exact broken schema from tasknotes_update_task
BROKEN_TOOL = {
    "name": "tasknotes_update_task",
    "description": "Update an existing task",
    "input_schema": {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"description": "Task file path", "type": "string"},
            "title": {"description": "New title", "type": "string"},
            "status": {"description": "New status", "type": "string"},
            "blockedBy": {
                "anyOf": [
                    {"type": "array"},
                    {"type": "null"},
                ],
                "description": "Tasks blocking this one",
            },
        },
        "required": ["id"],
    },
}

# A clean tool for comparison
CLEAN_TOOL = {
    "name": "read_file",
    "description": "Read a file",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"description": "File path", "type": "string"},
        },
        "required": ["path"],
    },
}


def make_request_body(model: str, tools: list) -> dict:
    return {
        "model": model,
        "max_tokens": 100,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Say exactly: SCHEMA_TEST_OK"}],
            }
        ],
        "tools": tools,
    }


def start_proxy() -> subprocess.Popen:
    env = os.environ.copy()
    env["CLI_PROXY_BASE_URL"] = CLI_PROXY_BASE_URL
    env["CLI_PROXY_API_KEY"] = CLI_PROXY_API_KEY
    proc = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT), str(PROXY_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for proxy to be ready
    proxy_url = f"http://127.0.0.1:{PROXY_PORT}/health"
    for i in range(30):
        try:
            resp = httpx.get(proxy_url, timeout=2)
            if resp.status_code == 200:
                print(f"  Proxy started on :{PROXY_PORT} (pid={proc.pid})")
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    # Dump stderr for debugging
    proc.terminate()
    stderr = proc.stderr.read().decode() if proc.stderr else ""
    raise RuntimeError(f"Proxy failed to start on :{PROXY_PORT}. stderr:\n{stderr}")


def send_through_proxy(body: dict) -> dict:
    url = f"http://127.0.0.1:{PROXY_PORT}/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY or CLI_PROXY_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
    resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
    return {"status": resp.status_code, "body": resp.json() if resp.status_code == 200 else resp.text}


def main():
    results = {}
    proxy = None

    try:
        # Start proxy with our fix
        print("Starting cache proxy with schema sanitization fix...")
        proxy = start_proxy()

        # ── Test 1: GPT with broken schema (the bug case) ──
        print("\n--- Test 1: GPT + broken tasknotes schema ---")
        body = make_request_body("gpt-5.4-mini", [BROKEN_TOOL, CLEAN_TOOL])
        result = send_through_proxy(body)
        results["gpt_broken_schema"] = result
        if result["status"] == 200:
            print(f"  PASS: GPT accepted the request (status={result['status']})")
            resp_body = result["body"]
            content = resp_body.get("content", [{}])
            text = content[0].get("text", "") if content else ""
            print(f"  Response: {text[:100]}")
        else:
            print(f"  FAIL: GPT rejected (status={result['status']})")
            print(f"  Error: {str(result['body'])[:300]}")

        # ── Test 2: Claude with broken schema (should work, always did) ──
        print("\n--- Test 2: Claude + broken tasknotes schema (control) ---")
        body = make_request_body("claude-haiku-4-5-20251001", [BROKEN_TOOL, CLEAN_TOOL])
        result = send_through_proxy(body)
        results["claude_broken_schema"] = result
        if result["status"] == 200:
            print(f"  PASS: Claude accepted (status={result['status']})")
        else:
            print(f"  FAIL: Claude rejected (status={result['status']})")
            print(f"  Error: {str(result['body'])[:300]}")

        # ── Test 3: GPT without broken schema (clean tools only) ──
        print("\n--- Test 3: GPT + clean tools only ---")
        body = make_request_body("gpt-5.4-mini", [CLEAN_TOOL])
        result = send_through_proxy(body)
        results["gpt_clean_schema"] = result
        if result["status"] == 200:
            print(f"  PASS: GPT accepted clean tools (status={result['status']})")
        else:
            print(f"  FAIL: GPT rejected clean tools (status={result['status']})")

        # ── Test 4: Direct to CLIProxyAPI (no proxy) with broken schema ──
        # This should FAIL — proves our proxy fix is the difference
        print("\n--- Test 4: Direct to CLIProxyAPI (no proxy) + broken schema ---")
        url = f"{CLI_PROXY_BASE_URL}/v1/messages"
        headers = {
            "x-api-key": CLI_PROXY_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = make_request_body("gpt-5.4-mini", [BROKEN_TOOL, CLEAN_TOOL])
        timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
        resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
        results["direct_broken_schema"] = {"status": resp.status_code}
        if resp.status_code == 400:
            print(f"  EXPECTED: Direct request fails with 400 (proves proxy fix works)")
            error_text = resp.text[:200]
            print(f"  Error: {error_text}")
        elif resp.status_code == 200:
            print(f"  UNEXPECTED: Direct request succeeded — maybe CLIProxyAPI added its own fix?")
        else:
            print(f"  Status: {resp.status_code}")

    finally:
        if proxy:
            # Read stderr for proxy logs
            proxy.send_signal(signal.SIGINT)
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
            stderr = proxy.stderr.read().decode() if proxy.stderr else ""
            print(f"\n--- Proxy logs ---\n{stderr[-2000:]}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    gpt_ok = results.get("gpt_broken_schema", {}).get("status") == 200
    claude_ok = results.get("claude_broken_schema", {}).get("status") == 200
    direct_fails = results.get("direct_broken_schema", {}).get("status") == 400
    print(f"  GPT + broken schema via proxy:  {'PASS' if gpt_ok else 'FAIL'}")
    print(f"  Claude + broken schema (ctrl):  {'PASS' if claude_ok else 'FAIL'}")
    print(f"  Direct broken schema fails:     {'YES (expected)' if direct_fails else 'NO'}")
    print(f"  Overall: {'ALL PASS' if (gpt_ok and claude_ok) else 'FAILURES'}")

    # Write results JSON
    out_path = Path(__file__).parent / "results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {out_path}")

    return 0 if (gpt_ok and claude_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
