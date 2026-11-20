"""Official Antigravity CLI adapter for fresh, reasoning-only OBS agents."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock


AGY_BINARY = "/workspace/runtime/antigravity-cli/antigravity"
AGY_HOME = "/workspace/runtime/antigravity-cli/home"
AGY_MODELS = {"gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"}


def is_agy_model(model: str) -> bool:
    from obs_agent.config import parse_context_suffix, resolve_model

    return parse_context_suffix(resolve_model(model))[0].lower().startswith("gemini-")


class AgyError(RuntimeError):
    """An error that must not trigger Claude JSONL recovery or fallback."""


class AgyResultMessage(ResultMessage):
    """Result with subscription usage but no fabricated dollar cost."""


class AgyClient:
    """One fresh CLI conversation, with all tool actions requiring approval."""

    def __init__(self, model: str, cwd: Path, env: dict[str, str]):
        from obs_agent.config import parse_context_suffix, resolve_model

        self.model = parse_context_suffix(resolve_model(model))[0].lower()
        if self.model not in AGY_MODELS:
            raise AgyError(f"Gemini model {self.model!r} is not available in the verified Antigravity account")
        self.cwd = cwd
        self.env = {**os.environ, **env, "HOME": AGY_HOME, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH"):
            self.env.pop(name, None)
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._first_response = True

    async def connect(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            AGY_BINARY, "--model", self.model,
            "--settings", json.dumps({"toolPermission": "strict", "agentMode": "", "useG1Credits": False}),
            "--input-format", "stream-json", "--output-format", "stream-json",
            cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
        )
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=30)
            event = json.loads(line)
            init = event.get("init", {})
            session_id = event.get("conversation_id")
            if (event.get("event") != "init" or init.get("permission_mode") != "strict"
                    or init.get("model") != self.model or not session_id):
                raise AgyError("Antigravity did not confirm the requested model and strict permissions")
            self.session_id = session_id
        except BaseException:
            await self.disconnect()
            raise

    async def query(self, prompt: str) -> None:
        if self.process is None or self.process.returncode is not None:
            raise AgyError("Antigravity session is not running; resume is not yet supported")
        note = ("OBS provider note: this is a reasoning-only Gemini session. "
                "Tool calls, including reads, are unavailable; do not claim to have used tools. "
                "Forking and reconnecting are not supported.\n\n")
        try:
            self.process.stdin.write((json.dumps({"event": "user", "message": {"content": note + prompt}}) + "\n").encode())
            await self.process.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise AgyError("Antigravity input stream failed") from exc

    async def receive_response(self):
        if self.process is None or self.session_id is None:
            raise AgyError("Antigravity session is not connected")
        if self._first_response:
            self._first_response = False
            yield SystemMessage(subtype="init", data={"session_id": self.session_id})
        while line := await self.process.stdout.readline():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise AgyError("Antigravity returned malformed output") from exc
            if event.get("event") != "result":
                continue
            result = event.get("result") or {}
            if result.get("conversation_id") != self.session_id:
                raise AgyError("Antigravity returned an unexpected conversation ID")
            denied = result.get("denied_actions") or []
            failed = result.get("status") != "SUCCESS" or bool(denied)
            text = result.get("response") or ""
            if denied:
                text = "Antigravity tool access was denied: this initial OBS Gemini provider supports reasoning only."
            elif failed:
                text = "Antigravity request failed: " + str(result.get("error") or "unknown error")
            if text and not failed:
                yield AssistantMessage(content=[TextBlock(text=text)], model=self.model)
            yield AgyResultMessage(
                subtype="error_during_execution" if failed else "success",
                duration_ms=int(float(result.get("duration_seconds") or 0) * 1000),
                duration_api_ms=0, is_error=failed, num_turns=int(result.get("num_turns") or 1),
                session_id=self.session_id, usage=result.get("usage"), result=text,
            )
            return
        raise AgyError("Antigravity exited before returning a result")

    async def interrupt(self) -> None:
        await self.disconnect()

    async def disconnect(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
