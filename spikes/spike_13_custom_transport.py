"""
Spike 13: Custom transport wrapper to see raw protocol messages
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

class LoggingTransport:
    """Wraps a transport to log all messages."""

    def __init__(self, inner: SubprocessCLITransport):
        self._inner = inner
        self.log = []

    async def connect(self):
        return await self._inner.connect()

    async def write(self, data: str):
        parsed = None
        try:
            parsed = json.loads(data.strip())
        except:
            pass
        msg_type = parsed.get("type", "?") if parsed else "?"
        subtype = ""
        if parsed and "request" in parsed:
            subtype = f" ({parsed['request'].get('subtype', '?')})"
        elif parsed and "response" in parsed:
            subtype = f" ({parsed['response'].get('subtype', '?')})"
        print(f"  >>> WRITE [{msg_type}{subtype}]: {data.strip()[:200]}")
        self.log.append(("write", data))
        return await self._inner.write(data)

    async def read_messages(self):
        async for msg in self._inner.read_messages():
            msg_type = msg.get("type", "?") if isinstance(msg, dict) else "?"
            subtype = ""
            if isinstance(msg, dict):
                subtype = msg.get("subtype", "")
                if subtype:
                    subtype = f" ({subtype})"
            msg_str = json.dumps(msg, default=str)[:200] if isinstance(msg, dict) else str(msg)[:200]
            print(f"  <<< READ  [{msg_type}{subtype}]: {msg_str}")
            self.log.append(("read", msg))
            yield msg

    def is_ready(self):
        return self._inner.is_ready()

    async def end_input(self):
        return await self._inner.end_input()


async def main():
    agents = {
        "mini": AgentDefinition(
            description="Answers in one word",
            prompt="Answer with exactly one word. Nothing else.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=3,
    )

    # Create the transport manually so we can wrap it
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
        print("=== Raw protocol messages ===\n")
        await client.query("Use the mini agent: what color is the sky?")

        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                print(f"\n  FINAL: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    print(f"\n=== Total messages: {len(logging_transport.log)} ===")

if __name__ == "__main__":
    asyncio.run(main())
