"""Loopback-only Messages API policy for the cold-notice E2E scenarios."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from pathlib import Path


CHILDREN = {
    "1": [("A", 60)], "2": [("A", 45)], "3": [("A", 45)],
    "3b-i": [("A", 35), ("B", 80)],
    "3b-ii": [("A", 25), ("B", 70)], "4": [("A", 5)],
    "7-human": [("A", 45)], "7-inbox": [("A", 45)], "8": [("A", 5)],
}


def clean(text: str, limit: int = 12000) -> str:
    text = re.sub(r"cch=0{5}", "[billing marker redacted]", text)
    text = re.sub(r"</?system[-]reminder[^>]*>", "[reminder marker redacted]", text)
    return text[:limit]


def texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(str(block.get("text", "")))
        elif block.get("type") == "tool_result":
            out.extend(texts(block.get("content")))
    return out


def blocks(body: dict, kind: str) -> list[dict]:
    return [b for m in body.get("messages", []) for b in
            (m.get("content", []) if isinstance(m.get("content"), list) else [])
            if isinstance(b, dict) and b.get("type") == kind]


def leaf(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def tool_name(body: dict, wanted: str) -> str:
    for tool in body.get("tools", []):
        name = str(tool.get("name", ""))
        if leaf(name) == wanted:
            return name
    raise RuntimeError(f"Required real CLI tool unavailable: {wanted}")


def launch_handle(body: dict, index: int) -> dict:
    uses = [b for b in blocks(body, "tool_use") if leaf(b.get("name", "")) == "AgentTask"]
    wanted = uses[index]["id"]
    result = next(b for b in blocks(body, "tool_result") if b.get("tool_use_id") == wanted)
    text = "\n".join(texts(result.get("content")))
    for start in range(len(text)):
        if text[start] != "{":
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("task_id"):
            return obj
    task = re.search(r"<task_id>([^<]+)</task_id>", text)
    if task:
        return {"task_id": html.unescape(task.group(1))}
    task = re.search(r"task_id[^\n:]*:\s*([0-9a-f-]{36})", text)
    if task:
        return {"task_id": task.group(1)}
    raise RuntimeError("Real AgentTask launch result did not contain a task handle")


def classify(body: dict) -> tuple[str, dict]:
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        for text in texts(message.get("content")):
            match = re.search(r"RUOI-(PARENT|CHILD|SENDER) (\{[^\n]+\})", text)
            if match:
                return match.group(1).lower(), json.loads(match.group(2))
    return "side", {}


def decide(body: dict, role: str, spec: dict) -> tuple[str, list[dict], str]:
    def final(kind, text):
        return kind, [{"type": "text", "text": text}], "end_turn"

    def call(kind, name, args):
        selected = tool_name(body, name)
        schema = next(t for t in body["tools"] if t["name"] == selected).get("input_schema", {})
        properties = schema.get("properties", {})
        args = {key: (json.dumps(value) if isinstance(value, bool) else str(value))
                if properties.get(key, {}).get("type") == "string" and not isinstance(value, str) else value
                for key, value in args.items()}
        return kind, [{"type": "tool_use", "name": selected, "input": args}], "tool_use"

    if role == "side" or not body.get("tools"):
        return final("side", "OK")
    uses = blocks(body, "tool_use")
    if role == "child":
        if not any(leaf(b.get("name", "")) == "Bash" for b in uses):
            return call("child-sleep", "Bash", {
                "command": f"sleep {int(spec['sleep'])}",
                "description": "Isolated scenario wait", "timeout": 120000,
            })
        if spec.get("scenario") == "1" and spec.get("s1_mode", "error") == "error":
            return "child-http-400", [], "end_turn"
        return final("child-final", f"RUOI-RESULT-{spec['child']}")
    if role == "sender":
        if not any(leaf(b.get("name", "")) == "SendInboxMessage" for b in uses):
            return call("sender-message", "SendInboxMessage", {
                "team_name": "2026-10-22-18-00-ruoi-e2e", "recipient": spec["to"],
                "sender": "ruoi-sender", "summary": "RUOI isolated sender notice",
                "content": "RUOI-SENDER-NOTICE " + str(spec.get("scenario", "")),
                "allow_cold_wake": bool(spec.get("override")),
            })
        return final("sender-final", "RUOI sender finished")
    scenario = spec["scenario"]
    children = CHILDREN.get(scenario, [])
    launches = [b for b in uses if leaf(b.get("name", "")) == "AgentTask"]
    if len(launches) < len(children):
        child, seconds = children[len(launches)]
        return call("parent-launch-" + child, "AgentTask", {
            "display_name": "RUOI child " + child,
            "prompt": "RUOI-CHILD " + json.dumps({"child": child, "sleep": seconds, "scenario": scenario,
                                                  "s1_mode": spec.get("s1_mode", "error")}),
            "fork": False, "inherit_schedules": "false", "hooks": "{}",
        })
    if scenario in ("3", "3b-ii") and not any(leaf(b.get("name", "")) == "AgentTaskOutput" for b in uses):
        handle = launch_handle(body, 1 if scenario == "3b-ii" else 0)
        return call("parent-blocked-output", "AgentTaskOutput", {
            "task_id": handle["task_id"], "team_name": "", "agent_name": "",
            "block": True, "timeout": 120000,
        })
    all_text = "\n".join(text for message in body.get("messages", [])
                         for text in texts(message.get("content")))
    if scenario.startswith("7-") and (
        "RUOI-HUMAN-WAKE" in all_text or "RUOI-SENDER-NOTICE" in all_text
    ):
        reads = sum(leaf(b.get("name", "")) == "ReadInbox" for b in uses)
        if reads < 2:
            return call(f"parent-read-{reads + 1}", "ReadInbox", {
                "team_name": "2026-10-22-18-00-ruoi-e2e", "agent": "ruoi-parent", "include_read": False,
                "mark_read": True, "limit": 50,
            })
    output = "RUOI parent idle"
    if scenario in ("3", "3b-ii"):
        output_uses = [b for b in uses if leaf(b.get("name", "")) == "AgentTaskOutput"]
        output_results = [b for b in blocks(body, "tool_result") if b.get("tool_use_id") == output_uses[-1]["id"]]
        observed = "\n".join(text for b in output_results for text in texts(b.get("content")))
        output = "RUOI parent tool result: " + clean(observed, 1800)
    return final("parent-final", output)


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Stub:
    def __init__(self, root: Path, before_tool=None):
        self.root = root
        self.path = root / "stub.jsonl"
        self.rows: list[dict] = []
        self.before_tool = before_tool
        self.counter = 0

    def log(self, row):
        self.rows.append(row)
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, default=str) + "\n")

    async def handle(self, reader, writer):
        try:
            await self.respond(reader, writer)
        except Exception as exc:
            self.log({"t": time.time(), "kind": "stub-error", "error": clean(repr(exc))})
            payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": str(exc)}}).encode()
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
            try:
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    async def respond(self, reader, writer):
        raw_head = await reader.readuntil(b"\r\n\r\n")
        lines = raw_head.decode().split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = dict((k.lower().strip(), v.strip()) for k, v in
                       (line.split(":", 1) for line in lines[1:] if ":" in line))
        raw = await reader.readexactly(int(headers.get("content-length", "0")))
        body = json.loads(raw) if raw else {}
        self.counter += 1
        number = self.counter
        role, spec = classify(body)
        if path.startswith("/v1/messages/count_tokens"):
            payload = b'{"input_tokens":100}'
            self.log({"t": time.time(), "kind": "count_tokens", "role": role, "bytes": len(raw)})
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
            await writer.drain()
            return
        if method != "POST" or not path.startswith("/v1/messages"):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
            return
        kind, content, stop = decide(body, role, spec)
        for index, block in enumerate(content):
            if block["type"] == "tool_use":
                block["id"] = f"toolu_ruoi_{number}_{index}"
                if self.before_tool:
                    self.before_tool(role, leaf(block["name"]), block["input"])
        user_texts = ["\n".join(texts(m.get("content"))) for m in body.get("messages", []) if m.get("role") == "user"]
        result_rows = []
        names_by_id = {b["id"]: leaf(b.get("name", "")) for b in blocks(body, "tool_use")}
        for result in blocks(body, "tool_result"):
            result_rows.append({"tool": names_by_id.get(result.get("tool_use_id")),
                                "id": result.get("tool_use_id"), "is_error": bool(result.get("is_error")),
                                "text": clean("\n".join(texts(result.get("content"))))})
        hints = [clean(line) for text in user_texts for line in text.splitlines()
                 if "notice(s) arrived while" in line or "did not wake you" in line]
        self.log({"n": number, "t": time.time(), "path": path, "role": role,
                  "child": spec.get("child"), "kind": kind, "bytes": len(raw),
                  "request_sha256": hashlib.sha256(raw).hexdigest(),
                  "n_messages": len(body.get("messages", [])),
                  "last_user_digest": clean(user_texts[-1] if user_texts else "", 260),
                  "hint_lines": hints, "tools_returned": content, "tool_results": result_rows,
                  "stub_cache_creation_input_tokens": 0, "usage_is_synthetic": True})
        usage = {"input_tokens": 50, "output_tokens": 1, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0}
        if kind == "child-http-400":
            payload = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                  "message": "RUOI intentional non-retryable scenario failure"}}).encode()
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
            await writer.drain()
            return
        model = body.get("model") or "claude-stub"
        message = {"id": f"msg_ruoi_{number}", "type": "message", "role": "assistant", "model": model,
                   "content": content, "stop_reason": stop, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
            payload = json.dumps(message).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n")
        writer.write(sse("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}))
        for index, block in enumerate(content):
            if block["type"] == "text":
                initial = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                initial = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            writer.write(sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": initial}))
            writer.write(sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}))
            writer.write(sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        writer.write(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 10}}))
        writer.write(sse("message_stop", {"type": "message_stop"}))
        await writer.drain()
