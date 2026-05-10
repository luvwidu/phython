"""Interactive REPL for Friday, the central orchestrator.

Run with: ``python -m friday`` or, after install, ``friday``.

The REPL streams the orchestrator's responses, then waits for the next user
turn. Background dispatch jobs continue running across turns.
"""
from __future__ import annotations

import asyncio
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from .agents import REGISTRY
from .prompts import SYSTEM_PROMPT
from .tools import ALLOWED_TOOLS, build_server


def _render(msg) -> None:
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(block.text)
            elif isinstance(block, ToolUseBlock):
                print(f"  · using {block.name}")
            elif isinstance(block, ThinkingBlock):
                pass
    elif isinstance(msg, ResultMessage):
        if msg.is_error:
            print(f"[error] {msg.result}")


async def repl() -> None:
    server = build_server()
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"friday": server},
        allowed_tools=ALLOWED_TOOLS,
        agents=REGISTRY,
        model="claude-opus-4-7",
        permission_mode="default",
        setting_sources=["user"],
    )

    print("Friday — central orchestrator. Type a request, or 'exit' to quit.")
    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                user_input = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", ":q"}:
                return
            await client.query(user_input)
            print()
            async for msg in client.receive_response():
                _render(msg)


def run() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY not set; the SDK will fall back to your "
            "Claude Code login if available.",
            file=sys.stderr,
        )
    try:
        asyncio.run(repl())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
