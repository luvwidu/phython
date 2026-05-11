"""Gemini CLI worker — drives `gemini` CLI inside a worktree.

Uses the non-interactive `gemini -p <prompt>` form so we can capture the
agent's actions as a one-shot subprocess. The CLI is expected to make file
edits directly in the worktree; we then commit + diff.
"""
from __future__ import annotations

import asyncio

from . import BaseWorker, WorkerResult


class GeminiWorker(BaseWorker):
    agent_name = "gemini"

    async def run(self, instruction: str) -> WorkerResult:
        tail: list[str] = []
        try:
            # `gemini -p "<prompt>"` runs in non-interactive mode in cwd.
            # `--yolo` (or equivalent) auto-accepts file edits; gemini-cli's
            # default is to ask, which would hang in a non-tty subprocess.
            proc = await asyncio.create_subprocess_exec(
                "gemini",
                "-p", instruction,
                "--yolo",
                cwd=self.worktree_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=600,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return WorkerResult(
                    agent=self.agent_name,
                    success=False,
                    diff="",
                    summary="gemini timeout after 10m",
                    output_tail=tail,
                    error="timeout",
                )

            stdout_s = stdout.decode("utf-8", "replace")
            stderr_s = stderr.decode("utf-8", "replace")
            for chunk in (stdout_s, stderr_s):
                for line in chunk.splitlines():
                    if line.strip():
                        tail.append(line)
                        if len(tail) > 80:
                            tail = tail[-80:]

            if proc.returncode != 0:
                err = stderr_s.strip() or stdout_s.strip()
                return WorkerResult(
                    agent=self.agent_name,
                    success=False,
                    diff="",
                    summary=f"gemini failed: {err[:200]}",
                    output_tail=tail,
                    error=err[:500],
                )

            await self.commit_changes(f"[gemini] {instruction[:60]}")
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

        except FileNotFoundError:
            return WorkerResult(
                agent=self.agent_name,
                success=False,
                diff="",
                summary="`gemini` CLI not found in PATH",
                output_tail=tail,
                error="gemini not installed",
            )
        except Exception as e:  # noqa: BLE001
            return WorkerResult(
                agent=self.agent_name,
                success=False,
                diff="",
                summary=f"gemini error: {e}",
                output_tail=tail,
                error=str(e),
            )
