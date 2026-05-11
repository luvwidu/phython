"""Worker abstraction for Friday.

A worker drives a coding agent (Claude Code, Gemini CLI, ...) inside an
isolated working copy (a git worktree). The worker runs the agent on a
task description, captures structured output, and produces a diff against
the base commit.

This module defines the abstract interface. Concrete workers live in
sibling modules (claude.py, gemini.py).
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WorkerResult:
    agent: str                  # "claude", "gemini", ...
    success: bool
    diff: str                   # `git diff <base>..HEAD` output (may be empty)
    summary: str                # short human description of what the agent did
    output_tail: list[str] = field(default_factory=list)
    error: Optional[str] = None
    files_changed: list[str] = field(default_factory=list)


class BaseWorker(abc.ABC):
    """Drives one coding agent inside one worktree."""

    agent_name: str = "base"

    def __init__(self, worktree_path: str, base_ref: str = "HEAD") -> None:
        self.worktree_path = worktree_path
        self.base_ref = base_ref

    @abc.abstractmethod
    async def run(self, instruction: str) -> WorkerResult:
        """Execute the instruction. Return WorkerResult once finished or failed."""
        raise NotImplementedError

    # --- helpers shared by concrete workers --------------------------------

    async def _git(self, *args: str, timeout: int = 30) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=self.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "git timeout"
        out = (stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")).strip()
        return proc.returncode or 0, out

    async def collect_diff(self) -> tuple[str, list[str]]:
        """Return (diff_text, changed_files) against base_ref."""
        code, diff = await self._git("diff", f"{self.base_ref}..HEAD")
        if code != 0:
            return "", []
        code2, names = await self._git("diff", f"{self.base_ref}..HEAD", "--name-only")
        files = [ln.strip() for ln in names.splitlines() if ln.strip()] if code2 == 0 else []
        return diff, files

    async def commit_changes(self, message: str) -> bool:
        """Stage and commit any uncommitted changes. Returns True if a commit was made."""
        await self._git("add", "-A")
        code, _ = await self._git("diff", "--cached", "--quiet")
        if code == 0:
            return False
        code, _ = await self._git(
            "-c", "user.email=friday@local",
            "-c", "user.name=Friday Worker",
            "commit", "-m", message,
        )
        return code == 0
