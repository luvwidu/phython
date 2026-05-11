"""Per-run git worktree management for Friday Studio.

Each studio run gets its own worktrees (one per agent) under
~/.friday/worktrees/<run_id>/<agent>/, branched off the project's current
HEAD. Each agent works in isolation; we extract diffs against the base ref
and clean up afterwards.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

WORKTREE_ROOT = Path.home() / ".friday" / "worktrees"


async def _run(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    txt = (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip()
    return proc.returncode or 0, txt


async def base_ref(repo_path: str) -> str:
    """Return the current HEAD commit of the project repo."""
    code, out = await _run(["git", "rev-parse", "HEAD"], Path(repo_path))
    if code != 0:
        raise RuntimeError(f"could not get HEAD of {repo_path}: {out}")
    return out.splitlines()[0].strip()


async def create_worktree(
    repo_path: str, run_id: str, agent: str, base: Optional[str] = None,
) -> tuple[Path, str]:
    """Create a worktree for one agent's run. Returns (path, base_sha)."""
    repo = Path(repo_path).expanduser().resolve()
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise RuntimeError(f"{repo} is not a git repo")
    base_sha = base or await base_ref(str(repo))
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    target = WORKTREE_ROOT / run_id / agent
    if target.exists():
        # Stale leftover — clean it up first
        await remove_worktree(str(target), force=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = f"friday/{run_id}/{agent}"
    code, out = await _run(
        ["git", "worktree", "add", "-b", branch, str(target), base_sha],
        repo,
    )
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out}")
    return target, base_sha


async def remove_worktree(path: str, force: bool = False) -> None:
    """Remove a worktree, freeing the branch."""
    p = Path(path)
    if not p.exists():
        return
    # find the repo this worktree belongs to by walking until we find a regular .git dir
    repo: Optional[Path] = None
    for parent in p.parents:
        gitdir = parent / ".git"
        if gitdir.is_dir():
            repo = parent
            break
    if repo:
        args = ["git", "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(p))
        await _run(args, repo)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


async def cleanup_run(run_id: str) -> None:
    """Remove all worktrees for a run."""
    run_dir = WORKTREE_ROOT / run_id
    if not run_dir.exists():
        return
    for agent_dir in run_dir.iterdir():
        if agent_dir.is_dir():
            await remove_worktree(str(agent_dir), force=True)
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
