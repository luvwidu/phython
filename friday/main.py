"""Interactive REPL for Friday, the central orchestrator.

Run with: ``python -m friday`` or, after install, ``friday``.

The REPL streams the orchestrator's responses, then waits for the next user
turn. Background dispatch jobs continue running across turns.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

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
from .config import load_mcp_config
from .prompts import SYSTEM_PROMPT
from . import sync
from .tasks import store
from .tools import ALLOWED_TOOLS, build_server
from .watcher import resume_all as resume_watchers


def _surface_notifications() -> None:
    notifs = store.pop_notifications()
    if not notifs:
        return
    print()
    print("─── 알림 ───")
    for n in notifs:
        ts = time.strftime("%H:%M:%S", time.localtime(n.get("ts", time.time())))
        print(f"[{ts}] {n.get('text', '')}")
    print("────────────")


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
    extra_servers, extra_allowed, source = load_mcp_config()
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"friday": server, **extra_servers},
        allowed_tools=ALLOWED_TOOLS + extra_allowed,
        agents=REGISTRY,
        model="claude-opus-4-7",
        permission_mode="default",
        setting_sources=["user"],
    )

    print("Friday — central orchestrator. Type a request, or 'exit' to quit.")
    sync_status = sync.init()
    print(f"  {sync_status}")
    if source:
        print(f"  loaded {len(extra_servers)} external MCP server(s) from {source}")
    resume_watchers()
    sync.schedule_sync()  # kick the worker so any startup changes get pushed
    async with ClaudeSDKClient(options=options) as client:
        while True:
            _surface_notifications()
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
