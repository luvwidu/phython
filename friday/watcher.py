"""GitHub PR watcher.

Polls watched PRs via the `gh` CLI and pushes change notifications to the
shared notification queue. Watches survive process restarts via JSON state.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from .tasks import DATA_DIR, store

WATCH_FILE = DATA_DIR / "watched_prs.json"


@dataclass
class WatchEntry:
    key: str
    repo: str
    number: int
    interval: int
    last_state: str = ""
    last_comment_id: Optional[int] = None
    last_check: float = 0.0
    last_error: str = ""


_ENTRIES: dict[str, WatchEntry] = {}
_TASKS: dict[str, asyncio.Task] = {}


def _load() -> None:
    global _ENTRIES
    if not WATCH_FILE.exists():
        _ENTRIES = {}
        return
    raw = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    _ENTRIES = {k: WatchEntry(**v) for k, v in raw.items()}


def _save() -> None:
    WATCH_FILE.write_text(
        json.dumps({k: asdict(v) for k, v in _ENTRIES.items()}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _gh(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return proc.returncode, (proc.stdout if proc.returncode == 0 else proc.stderr)
    except FileNotFoundError:
        return 127, "gh CLI not installed"
    except subprocess.TimeoutExpired:
        return 124, "gh timeout"


async def _check_once(entry: WatchEntry) -> list[str]:
    msgs: list[str] = []
    code, out = await asyncio.to_thread(
        _gh,
        [
            "pr", "view", str(entry.number),
            "--repo", entry.repo,
            "--json", "state,statusCheckRollup,comments",
        ],
    )
    if code != 0:
        entry.last_error = out.strip()[:200]
        msgs.append(f"watch error: {entry.last_error}")
        return msgs
    entry.last_error = ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        msgs.append(f"parse error: {e}")
        return msgs

    state = data.get("state", "")
    if entry.last_state and state != entry.last_state:
        msgs.append(f"state {entry.last_state} → {state}")
    if not entry.last_state and state:
        msgs.append(f"baseline state: {state}")
    entry.last_state = state

    failing = [
        c for c in (data.get("statusCheckRollup") or [])
        if (c.get("conclusion") or "").upper() == "FAILURE"
    ]
    if failing:
        names = ", ".join(c.get("name", "?") for c in failing[:3])
        msgs.append(f"CI failing: {names}")

    comments = data.get("comments") or []
    if comments:
        latest = comments[-1]
        latest_id = latest.get("id")
        if entry.last_comment_id is not None and latest_id != entry.last_comment_id:
            try:
                idx = next(
                    (i for i, c in enumerate(comments) if c.get("id") == entry.last_comment_id),
                    -1,
                )
                new = comments[idx + 1:] if idx >= 0 else comments
            except Exception:
                new = [latest]
            author = latest.get("author", {}).get("login", "?")
            msgs.append(f"{len(new)} new comment(s); latest by {author}")
        entry.last_comment_id = latest_id

    entry.last_check = time.time()
    return msgs


async def _watch_loop(key: str) -> None:
    while True:
        entry = _ENTRIES.get(key)
        if not entry:
            return
        try:
            changes = await _check_once(entry)
            for m in changes:
                store.append_notification(f"[PR {entry.repo}#{entry.number}] {m}")
            _save()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            store.append_notification(
                f"[PR {entry.repo}#{entry.number}] watcher error: {e}"
            )
        await asyncio.sleep(max(30, entry.interval))


def resume_all() -> None:
    """Restart watch loops for every persisted entry. Call once inside an event loop."""
    _load()
    for key in _ENTRIES:
        if key not in _TASKS or _TASKS[key].done():
            _TASKS[key] = asyncio.create_task(_watch_loop(key))


def add_watch(repo: str, number: int, interval: int = 120) -> str:
    if "/" not in repo:
        return f"repo must be in owner/name form, got: {repo}"
    key = f"{repo}#{number}"
    if key in _ENTRIES:
        return f"already watching {key}"
    interval = max(30, int(interval))
    _ENTRIES[key] = WatchEntry(key=key, repo=repo, number=number, interval=interval)
    _save()
    _TASKS[key] = asyncio.create_task(_watch_loop(key))
    return f"watching {key} every {interval}s"


def remove_watch(repo: str, number: int) -> str:
    key = f"{repo}#{number}"
    task = _TASKS.pop(key, None)
    if task:
        task.cancel()
    if _ENTRIES.pop(key, None):
        _save()
        return f"unwatched {key}"
    return f"not watching {key}"


def list_watches() -> list[WatchEntry]:
    return list(_ENTRIES.values())
