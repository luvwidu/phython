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
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .agents import REGISTRY
from .config import load_mcp_config
from .prompts import SYSTEM_PROMPT
from . import sync
from .tasks import store
from .tools import ALLOWED_TOOLS, build_server
from .watcher import resume_all as resume_watchers


console = Console()


def _surface_notifications() -> None:
    notifs = store.pop_notifications()
    if not notifs:
        return
    body = Text()
    for i, n in enumerate(notifs):
        ts = time.strftime("%H:%M:%S", time.localtime(n.get("ts", time.time())))
        body.append(f"[{ts}] ", style="dim")
        body.append(n.get("text", ""), style="yellow")
        if i < len(notifs) - 1:
            body.append("\n")
    console.print()
    console.print(Panel(body, title="알림", title_align="left", border_style="yellow"))


def _render_assistant_text(text: str) -> None:
    """Render assistant text. Use markdown when it looks like markdown, else plain."""
    has_md = any(marker in text for marker in ("```", "**", "##", "- ", "| ", "* "))
    try:
        if has_md:
            console.print(Markdown(text))
        else:
            console.print(text)
    except Exception:
        console.print(text)


def _render(msg) -> None:
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                _render_assistant_text(block.text)
            elif isinstance(block, ToolUseBlock):
                console.print(f"  · {block.name}", style="dim cyan")
            elif isinstance(block, ThinkingBlock):
                pass
    elif isinstance(msg, ResultMessage):
        if msg.is_error:
            console.print(f"[error] {msg.result}", style="bold red")


def _print_banner() -> None:
    console.print()
    console.print(
        "[bold]Friday[/bold] — central orchestrator. "
        "Type a request, or 'exit' to quit.",
    )


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

    _print_banner()
    sync_status = sync.init()
    console.print(f"  {sync_status}", style="dim")
    if source:
        console.print(
            f"  loaded {len(extra_servers)} external MCP server(s) from {source}",
            style="dim",
        )
    resume_watchers()
    sync.schedule_sync()
    async with ClaudeSDKClient(options=options) as client:
        while True:
            _surface_notifications()
            try:
                console.print()
                # rich-styled prompt printed inline; input() then receives keystrokes
                console.print("[bold cyan]you[/bold cyan][bold]>[/bold] ", end="")
                user_input = (await asyncio.to_thread(input, "")).strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", ":q"}:
                return
            await client.query(user_input)
            console.print()
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
