"""
Spike 14: Full protocol dump — see complete control protocol messages
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

class LoggingTransport:
    """Wraps a transport to log all messages with FULL content."""

    def __init__(self, inner):
        self._inner = inner
        self.messages = []

    async def connect(self):
        return await self._inner.connect()

    async def write(self, data: str):
        self.messages.append((">>>", data.strip()))
        return await self._inner.write(data)

    async def read_messages(self):
        async for msg in self._inner.read_messages():
            self.messages.append(("<<<", msg))
            yield msg

    def is_ready(self):
        return self._inner.is_ready()

    async def end_input(self):
        return await self._inner.end_input()

    async def close(self):
        return await self._inner.close()

async def main():
    agents = {
        "mini": AgentDefinition(
            description="Answers in one word",
            prompt="Answer with exactly one word.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=3,
    )

    async def _empty_stream():
        return
        yield {}

    inner_transport = SubprocessCLITransport(
        prompt=_empty_stream(),
        options=options,
    )
    logging_transport = LoggingTransport(inner_transport)
    client = ClaudeSDKClient(options, transport=logging_transport)

    async with client:
        await client.query("Use the mini agent: what is 1+1?")
        async for msg in client.receive_response():
            pass  # Just collecting protocol messages

    # Dump full protocol
    print("=" * 80)
    print("FULL PROTOCOL DUMP")
    print("=" * 80)
    for i, (direction, data) in enumerate(logging_transport.messages):
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                print(f"\n[{i}] {direction} {parsed.get('type', '?')}:")
                print(json.dumps(parsed, indent=2, default=str))
            except:
                print(f"\n[{i}] {direction} RAW: {data[:500]}")
        else:
            msg_type = data.get("type", "?")
            subtype = data.get("subtype", "")
            print(f"\n[{i}] {direction} {msg_type}" + (f" ({subtype})" if subtype else "") + ":")
            print(json.dumps(data, indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(main())
