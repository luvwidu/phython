"""Claude Code worker — drives the Claude Agent SDK inside a worktree."""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from . import BaseWorker, WorkerResult


def _msg_to_text(msg) -> str:
    parts: list[str] = []
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            name = getattr(block, "name", None)
            if name:
                parts.append(f"→ tool: {name}")
    return "\n".join(parts)


class ClaudeWorker(BaseWorker):
    agent_name = "claude"

    async def _drain(self, client: ClaudeSDKClient, tail: list[str]) -> None:
        async for msg in client.receive_response():
            text = _msg_to_text(msg)
            if text:
                tail.append(text)
                if len(tail) > 120:
                    del tail[:-120]

    async def run(self, instruction: str) -> WorkerResult:
        tail: list[str] = []
        opts = ClaudeAgentOptions(
            cwd=self.worktree_path,
            permission_mode="bypassPermissions",
            setting_sources=["project", "user"],
        )
        try:
            async with ClaudeSDKClient(options=opts) as client:
                await client.query(instruction)
                await self._drain(client, tail)
                # mid-flight steering: stay alive while a queue is provided
                while self.message_queue is not None:
                    msg = await self.message_queue.get()
                    if msg is None:
                        break  # "finish cleanly" signal
                    tail.append(f"[user→agent] {msg}")
                    await client.query(msg)
                    await self._drain(client, tail)
            await self.commit_changes(f"[claude] {instruction[:60]}")
            diff, files = await self.collect_diff()
            summary = tail[-1][:200] if tail else "(no output)"
            return WorkerResult(
                agent=self.agent_name,
                success=True,
                diff=diff,
                summary=summary,
                output_tail=tail,
                files_changed=files,
            )
        except Exception as e:  # noqa: BLE001
            return WorkerResult(
                agent=self.agent_name,
                success=False,
                diff="",
                summary=f"failed: {e}",
                output_tail=tail,
                error=str(e),
            )
