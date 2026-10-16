"""External control channel: do from bash what an agent does with its tools (bead vault-h667).

Daniel 2026-10-15: "be able to run a bash command and that bash command basically doing well the
same things as what an agent can do like a message some agent can create a new agent, create a
new tree ... ideally you know it is triggerable externally". Used by deterministic jobs (a failing
cron, an error monitor, the reflection watcher) to wake or launch agents without an agent in the
loop.

Design
------
* **Transport:** one Unix-domain socket owned by the running bot, ``{OBS_CONTROL_DIR}/control.sock``
  (default ``/workspace/runtime/state/obs-control``). There is no TCP listener, so nothing off this
  host can reach it; remote callers SSH into the ``obs-test`` container and run the CLI there.
* **Authentication:** the directory is 0700 and the socket 0600; the server additionally checks the
  peer's uid with ``SO_PEERCRED`` (must equal the bot's uid) and a random token in
  ``{OBS_CONTROL_DIR}/token`` (0600, regenerated at every bot start). A request that fails either
  check is refused and audited.
* **Same code paths as the agent tools:** ``message`` calls the SendInboxMessage handler and
  ``launch`` calls the AgentTask handler built by ``tools.create_obs_tools`` — so the cold-cache
  guard (vault-vokl), the start-step check (vault-a6qm), lineage, team registration, Telegram
  topics and session_source model rules all apply exactly as for an agent. ``launch`` runs as if
  the named PARENT agent had called AgentTask; forking a parent whose cache has gone cold is
  refused like any cold wake unless ``allow_cold_wake`` is set. ``new-tree`` creates a new root
  topic the same way a person starting a topic does (default new-chat hooks, ``new_trunk``
  bootstrap) and runs the prompt as its first turn.
* **Identity:** every request names its caller (``--as NAME``); messages are sent from
  ``external:NAME`` and launched prompts start with an ``[external trigger: NAME]`` line.
* **Audit:** every request, accepted or refused, is appended to ``{OBS_CONTROL_DIR}/audit.jsonl``.
* **Idempotency:** an optional ``idempotency_key`` returns the earlier result for a repeated key
  (in memory; cleared by a bot restart).

CLI: ``python -m obs_agent.control {ping|message|launch|new-tree} ...`` (wrapper
``/workspace/runtime/bin/obs-agentctl``). Exit 0 = done, 1 = refused or failed (reason printed),
2 = could not reach or authenticate to the running bot.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hmac
import json
import os
import re
import secrets
import socket
import struct
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_REQUEST_BYTES = 1024 * 1024
OPS = ("ping", "message", "launch", "new_tree")
_CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def control_dir() -> Path:
    return Path(os.environ.get("OBS_CONTROL_DIR") or "/workspace/runtime/state/obs-control")


def socket_path() -> Path:
    return control_dir() / "control.sock"


def token_path() -> Path:
    return control_dir() / "token"


def audit_path() -> Path:
    return control_dir() / "audit.jsonl"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict[str, Any]) -> None:
    try:
        with audit_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), **entry}, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except OSError:
        return None


def validate_request(request: Any) -> str | None:
    """Return an error for a malformed request, None when it may be dispatched."""
    if not isinstance(request, dict):
        return "request must be a JSON object"
    op = request.get("op")
    if op not in OPS:
        return f"unknown op {op!r}; expected one of {', '.join(OPS)}"
    caller = str(request.get("caller") or "").strip().lower()
    if not _CALLER_RE.fullmatch(caller):
        return "caller is required (lowercase letters, digits, . _ -; max 64)"
    args = request.get("args") or {}
    if not isinstance(args, dict):
        return "args must be an object"
    required = {
        "message": ("team_name", "recipient", "content"),
        "launch": ("parent_team", "parent_agent", "display_name"),
        "new_tree": ("title", "prompt"),
    }.get(op, ())
    missing = [key for key in required if not str(args.get(key) or "").strip()]
    if missing:
        return f"{op} needs {', '.join(missing)}"
    if op == "launch" and not (str(args.get("prompt") or "").strip() or str(args.get("prompt_file") or "").strip()):
        return "launch needs prompt or prompt_file"
    return None


class ControlServer:
    """The bot-side listener. ``dispatch`` does the work; this class only authenticates."""

    def __init__(self, dispatch: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]):
        self._dispatch = dispatch
        self._server: asyncio.AbstractServer | None = None
        self._token = ""
        self._results: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        directory = control_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self._token = secrets.token_hex(32)
        tmp = token_path().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self._token)
        os.replace(tmp, token_path())
        path = socket_path()
        if path.exists() or path.is_symlink():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(path), limit=MAX_REQUEST_BYTES)
        os.chmod(path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._respond(reader, writer)
        except Exception as exc:  # never let one bad client hurt the bot
            response = {"ok": False, "error": f"control server error: {type(exc).__name__}: {exc}"}
        try:
            writer.write((json.dumps(response, ensure_ascii=True, default=str) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict[str, Any]:
        uid = _peer_uid(writer)
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        try:
            request = json.loads(line.decode("utf-8") or "null")
        except ValueError:
            request = None
        op = request.get("op") if isinstance(request, dict) else None
        caller = str(request.get("caller") or "") if isinstance(request, dict) else ""
        if uid != os.getuid():
            _audit({"op": op, "caller": caller, "peer_uid": uid, "outcome": "refused", "reason": "peer uid"})
            return {"ok": False, "auth": False, "error": "refused: caller is not the OBS user"}
        token = str(request.get("token") or "") if isinstance(request, dict) else ""
        if not self._token or not hmac.compare_digest(token, self._token):
            _audit({"op": op, "caller": caller, "peer_uid": uid, "outcome": "refused", "reason": "token"})
            return {"ok": False, "auth": False, "error": "refused: missing or wrong control token"}
        problem = validate_request(request)
        if problem:
            _audit({"op": op, "caller": caller, "outcome": "invalid", "reason": problem})
            return {"ok": False, "error": problem}
        caller = caller.strip().lower()
        args = dict(request.get("args") or {})
        key = str(request.get("idempotency_key") or "").strip()
        if key and key in self._results:
            _audit({"op": op, "caller": caller, "idempotency_key": key, "outcome": "replayed"})
            return {**self._results[key], "replayed": True}
        if op == "ping":
            result: dict[str, Any] = {"ok": True, "pong": True, "pid": os.getpid()}
        else:
            result = await self._dispatch(op, caller, args)
        if key:
            self._results[key] = result
        _audit({
            "op": op, "caller": caller, "idempotency_key": key or None,
            "outcome": "ok" if result.get("ok") else "failed",
            "target": {k: args.get(k) for k in ("team_name", "recipient", "parent_team", "parent_agent", "title")
                       if args.get(k)},
            "detail": str(result.get("error") or result.get("text") or "")[:300],
        })
        return result


def tool_result_to_control(result: dict[str, Any]) -> dict[str, Any]:
    """Map an agent tool result (content/tool_use_result/is_error) onto the control response."""
    texts = [item.get("text", "") for item in (result.get("content") or []) if isinstance(item, dict)]
    text = "\n".join(t for t in texts if t)
    failed = bool(result.get("is_error") or result.get("isError"))
    out: dict[str, Any] = {"ok": not failed, "text": text}
    if isinstance(result.get("tool_use_result"), dict):
        out["result"] = result["tool_use_result"]
    if failed:
        out["error"] = text or "failed"
    return out


# --------------------------------------------------------------------------------------- client

def send_request(op: str, caller: str, args: dict[str, Any], *, idempotency_key: str | None = None,
                 timeout: float = 120.0) -> tuple[int, dict[str, Any]]:
    try:
        token = token_path().read_text(encoding="utf-8").strip()
    except OSError as exc:
        return 2, {"ok": False, "error": f"cannot read control token ({exc}); is OBS running here?"}
    payload = {"op": op, "caller": caller, "args": args, "token": token}
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path()))
            sock.sendall((json.dumps(payload) + "\n").encode())
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        return 2, {"ok": False, "error": f"cannot reach OBS control socket {socket_path()}: {exc}"}
    try:
        response = json.loads(b"".join(chunks).decode() or "{}")
    except ValueError:
        return 2, {"ok": False, "error": "malformed response from OBS"}
    if response.get("auth") is False:
        return 2, response
    return (0 if response.get("ok") else 1), response


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="obs-agentctl", description=__doc__.split("\n\n")[0])
    ap.add_argument("--as", dest="caller", required=True, help="who is calling (audit + sender label)")
    ap.add_argument("--idempotency-key", default=None)
    ap.add_argument("--json", action="store_true", help="print the full JSON response")
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("ping")
    m = sub.add_parser("message", help="SendInboxMessage as external:NAME")
    m.add_argument("--team", dest="team_name", required=True)
    m.add_argument("--to", dest="recipient", required=True)
    m.add_argument("--text", dest="content", required=True)
    m.add_argument("--summary", default=None)
    m.add_argument("--allow-cold-wake", action="store_true")
    la = sub.add_parser("launch", help="AgentTask as if PARENT called it")
    la.add_argument("--parent-team", required=True)
    la.add_argument("--parent", dest="parent_agent", required=True)
    la.add_argument("--name", dest="display_name", required=True)
    la.add_argument("--prompt", default="")
    la.add_argument("--prompt-file", default="")
    mode = la.add_mutually_exclusive_group()
    mode.add_argument("--fork", dest="fork", action="store_true", default=True)
    mode.add_argument("--fresh", dest="fork", action="store_false")
    la.add_argument("--model", default=None)
    la.add_argument("--effort", default=None)
    la.add_argument("--hooks", default=None, help="JSON hook map, as for AgentTask")
    la.add_argument("--session-source", default=None)
    la.add_argument("--allow-cold-wake", action="store_true")
    nt = sub.add_parser("new-tree", help="new root topic whose first turn is PROMPT")
    nt.add_argument("--title", required=True)
    nt.add_argument("--prompt", required=True)
    nt.add_argument("--chat-id", type=int, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    ns = _parser().parse_args(argv)
    op = "new_tree" if ns.op == "new-tree" else ns.op
    skip = {"caller", "idempotency_key", "json", "op"}
    args = {k: v for k, v in vars(ns).items() if k not in skip and v not in (None, "")}
    code, response = send_request(op, ns.caller, args, idempotency_key=ns.idempotency_key)
    if ns.json:
        print(json.dumps(response, indent=1))
    else:
        print(response.get("text") or response.get("error") or json.dumps(response))
    return code


if __name__ == "__main__":
    sys.exit(main())
