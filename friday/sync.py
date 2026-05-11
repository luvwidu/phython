"""Cross-machine state sync via a private git repo.

Treats the data/ directory as a working copy of the user's friday-state
repo. Pulls on startup, and after every mutation schedules an async
commit + push (debounced).

Configured via env vars:
  FRIDAY_STATE_REPO   git URL (e.g. git@github.com:luvwidu/friday-state.git)
  FRIDAY_MACHINE      short alias for this machine (e.g. mac, windows)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .tasks import DATA_DIR

ENV_REPO = "FRIDAY_STATE_REPO"
ENV_MACHINE = "FRIDAY_MACHINE"
BRANCH = "main"
DEBOUNCE_SEC = 2.0

_enabled = False
_machine = "local"
_repo_url = ""
_dirty = False
_worker: Optional[asyncio.Task] = None
_lock: Optional[asyncio.Lock] = None


def machine() -> str:
    return _machine


def is_enabled() -> bool:
    return _enabled


def _run(args: list[str], cwd: Optional[Path] = None, timeout: int = 30) -> tuple[int, str]:
    cwd = cwd or DATA_DIR
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, "git not installed"
    except subprocess.TimeoutExpired:
        return 124, "git timeout"


def _is_repo() -> bool:
    return (DATA_DIR / ".git").exists()


def _data_has_files() -> bool:
    return any(p.name != ".git" for p in DATA_DIR.iterdir())


def init() -> str:
    """Initialize sync. Returns a one-line status message for the user."""
    global _enabled, _machine, _repo_url, _lock
    repo = os.environ.get(ENV_REPO, "").strip()
    mach = os.environ.get(ENV_MACHINE, "").strip()
    if not repo:
        return f"(sync off — set {ENV_REPO} to enable)"
    _repo_url = repo
    _machine = mach or "local"

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not _is_repo():
        if _data_has_files():
            # Local data is the seed. Init, attach remote, future push will seed origin.
            code, out = _run(["init", "-b", BRANCH])
            if code != 0:
                return f"sync init failed: {out[:200]}"
            _run(["remote", "add", "origin", repo])
        else:
            # Empty local — try to clone existing state.
            code, out = _run(["clone", repo, "."], cwd=DATA_DIR)
            if code != 0:
                # Likely an empty remote — init locally instead.
                _run(["init", "-b", BRANCH])
                _run(["remote", "add", "origin", repo])
    else:
        code, out = _run(["pull", "--rebase", "origin", BRANCH])
        if code != 0:
            # Non-fatal: maybe the remote is empty or unreachable.
            print(f"warning: sync pull failed: {out[:200]}")

    _enabled = True
    return f"sync ON — {repo} as machine={_machine}"


async def _push_once() -> None:
    if not _enabled or _lock is None:
        return
    async with _lock:
        await asyncio.to_thread(_run, ["add", "-A"])
        code, _ = await asyncio.to_thread(_run, ["diff", "--cached", "--quiet"])
        if code == 0:
            return  # nothing to commit
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        msg = f"[{_machine}] state update {ts}"
        code, out = await asyncio.to_thread(_run, ["commit", "-m", msg])
        if code != 0:
            return
        code, out = await asyncio.to_thread(_run, ["push", "origin", BRANCH])
        if code != 0:
            # try a pull --rebase and push again
            code2, _ = await asyncio.to_thread(_run, ["pull", "--rebase", "origin", BRANCH])
            if code2 == 0:
                await asyncio.to_thread(_run, ["push", "origin", BRANCH])


async def _worker_loop() -> None:
    global _dirty
    while True:
        await asyncio.sleep(DEBOUNCE_SEC)
        if _dirty:
            _dirty = False
            try:
                await _push_once()
            except Exception:  # noqa: BLE001
                pass


def schedule_sync() -> None:
    """Mark state dirty. The worker will push on its next tick."""
    global _dirty, _worker
    if not _enabled:
        return
    _dirty = True
    if _worker is None or _worker.done():
        try:
            loop = asyncio.get_running_loop()
            _worker = loop.create_task(_worker_loop())
        except RuntimeError:
            pass  # no loop yet — will catch up on next mutation
