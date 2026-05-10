"""Custom MCP tools for Friday.

These tools let Friday manage a shared task list and run / converse with
background Claude Code jobs that live inside other repositories.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

from .tasks import store


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# --- task tools -----------------------------------------------------------


@tool(
    "add_task",
    "Register a new task in the central task list. Returns the task id.",
    {"title": str, "description": str, "project": str, "owner": str},
)
async def add_task(args):
    t = store.add_task(
        title=args["title"],
        description=args.get("description", ""),
        project=args.get("project", ""),
        owner=args.get("owner", ""),
    )
    return _ok(f"added task {t.id}: {t.title}")


@tool(
    "list_tasks",
    "List tasks. Optional status filter: pending, in_progress, blocked, done, cancelled.",
    {"status": str},
)
async def list_tasks(args):
    items = store.list_tasks(status=(args.get("status") or None))
    if not items:
        return _ok("(no tasks)")
    lines = [
        f"- [{t.status}] {t.id} | {t.title} | project={t.project or '-'} | owner={t.owner or '-'}"
        for t in items
    ]
    return _ok("\n".join(lines))


@tool(
    "update_task",
    "Update a task by id. Any of: status, owner, project, title, description, note.",
    {
        "id": str,
        "status": str,
        "owner": str,
        "project": str,
        "title": str,
        "description": str,
        "note": str,
    },
)
async def update_task(args):
    tid = args["id"]
    if not store.get_task(tid):
        return _ok(f"task {tid} not found")
    fields = {k: v for k, v in args.items() if k not in ("id", "note")}
    store.update_task(tid, **fields)
    if args.get("note"):
        store.append_note(tid, args["note"])
    t = store.get_task(tid)
    return _ok(f"updated {t.id}: status={t.status} owner={t.owner} project={t.project}")


@tool("get_task", "Show full details for a task including notes.", {"id": str})
async def get_task(args):
    t = store.get_task(args["id"])
    if not t:
        return _ok(f"task {args['id']} not found")
    notes = "\n  ".join(t.notes) if t.notes else "(none)"
    return _ok(
        f"id: {t.id}\ntitle: {t.title}\nstatus: {t.status}\nproject: {t.project}\n"
        f"owner: {t.owner}\ndescription: {t.description}\nnotes:\n  {notes}"
    )


# --- background dispatch jobs --------------------------------------------


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


def _notify(job_id: str, text: str) -> None:
    """Append a notification line for the REPL to surface on next prompt."""
    store.append_notification(f"[job {job_id}] {text}")


class JobRunner:
    """Holds a long-lived ClaudeSDKClient and a queue for follow-up messages."""

    def __init__(self, job_id: str, repo_path: str, instruction: str) -> None:
        self.job_id = job_id
        self.repo_path = repo_path
        self.initial_instruction = instruction
        self.queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self.task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _drain_response(self, client: ClaudeSDKClient) -> None:
        async for msg in client.receive_response():
            text = _msg_to_text(msg)
            if text:
                store.append_job_output(self.job_id, text)

    async def _run(self) -> None:
        try:
            opts = ClaudeAgentOptions(
                cwd=self.repo_path,
                permission_mode="acceptEdits",
                setting_sources=["project", "user"],
            )
            async with ClaudeSDKClient(options=opts) as client:
                await client.query(self.initial_instruction)
                await self._drain_response(client)
                while True:
                    next_msg = await self.queue.get()
                    if next_msg is None:
                        break
                    store.append_job_output(self.job_id, f"[user→agent] {next_msg}")
                    await client.query(next_msg)
                    await self._drain_response(client)
            store.update_job(
                self.job_id,
                status="succeeded",
                finished_at=time.time(),
                summary="finished",
            )
            _notify(self.job_id, "finished cleanly")
        except asyncio.CancelledError:
            store.update_job(
                self.job_id,
                status="cancelled",
                finished_at=time.time(),
                summary="cancelled",
            )
            _notify(self.job_id, "cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            store.update_job(
                self.job_id,
                status="failed",
                finished_at=time.time(),
                summary=f"error: {e}",
            )
            _notify(self.job_id, f"failed: {e}")
        finally:
            _RUNNERS.pop(self.job_id, None)


_RUNNERS: dict[str, JobRunner] = {}


@tool(
    "dispatch_to_repo",
    "Start a background Claude Code agent inside repo_path with the given "
    "instruction. The agent stays alive — use send_to_job to give follow-ups, "
    "tail_job to peek at output, finish_job to end politely, or cancel_job "
    "to hard-stop. Optionally link to an existing task_id.",
    {"repo_path": str, "instruction": str, "task_id": str},
)
async def dispatch_to_repo(args):
    raw = args["repo_path"]
    p = Path(raw).expanduser()
    if not p.exists():
        return _ok(f"repo path does not exist: {raw}")
    repo = str(p.resolve())
    job = store.add_job(
        task_id=(args.get("task_id") or None),
        repo_path=repo,
        instruction=args["instruction"],
    )
    runner = JobRunner(job.id, repo, args["instruction"])
    _RUNNERS[job.id] = runner
    runner.start()
    if args.get("task_id"):
        try:
            store.update_task(args["task_id"], status="in_progress")
            store.append_note(args["task_id"], f"dispatched job {job.id} to {repo}")
        except KeyError:
            pass
    return _ok(f"started job {job.id} in {repo}")


@tool(
    "send_to_job",
    "Send a follow-up instruction to a running job. The job must still be running.",
    {"id": str, "message": str},
)
async def send_to_job(args):
    runner = _RUNNERS.get(args["id"])
    if not runner:
        return _ok(f"no running job with id {args['id']}")
    await runner.queue.put(args["message"])
    return _ok(f"queued message to job {args['id']}")


@tool(
    "finish_job",
    "Politely end a running job with no more instructions; it will exit cleanly "
    "after its current turn.",
    {"id": str},
)
async def finish_job(args):
    runner = _RUNNERS.get(args["id"])
    if not runner:
        return _ok(f"no running job with id {args['id']}")
    await runner.queue.put(None)
    return _ok(f"finish signal sent to {args['id']}")


@tool("cancel_job", "Hard-cancel a running background job by id.", {"id": str})
async def cancel_job(args):
    runner = _RUNNERS.get(args["id"])
    if not runner or not runner.task:
        return _ok(f"job {args['id']} not running")
    runner.task.cancel()
    return _ok(f"cancelled {args['id']}")


@tool("list_jobs", "List dispatched background jobs (optional status filter).", {"status": str})
async def list_jobs(args):
    items = store.list_jobs(status=(args.get("status") or None))
    if not items:
        return _ok("(no jobs)")
    lines = [
        f"- {j.id} [{j.status}] task={j.task_id or '-'} repo={j.repo_path}"
        for j in items
    ]
    return _ok("\n".join(lines))


@tool("get_job", "Show details and recent output of a job.", {"id": str})
async def get_job(args):
    j = store.get_job(args["id"])
    if not j:
        return _ok("job not found")
    tail = "\n".join(j.output_tail[-25:]) if j.output_tail else "(no output yet)"
    return _ok(
        f"id: {j.id}\nstatus: {j.status}\ntask: {j.task_id}\nrepo: {j.repo_path}\n"
        f"summary: {j.summary}\n--- recent output ---\n{tail}"
    )


@tool(
    "tail_job",
    "Show the last N lines of a job's output (default 30, max 200).",
    {"id": str, "lines": int},
)
async def tail_job(args):
    j = store.get_job(args["id"])
    if not j:
        return _ok("job not found")
    n = max(1, min(int(args.get("lines") or 30), 200))
    tail = "\n".join(j.output_tail[-n:]) if j.output_tail else "(no output)"
    return _ok(tail)


# --- GitHub PR watching --------------------------------------------------


@tool(
    "watch_pr",
    "Start polling a GitHub PR for new comments, state changes, or CI failures. "
    "Requires `gh` CLI to be installed and authenticated. interval_sec defaults to "
    "120 and is clamped to a minimum of 30.",
    {"repo": str, "number": int, "interval_sec": int},
)
async def watch_pr(args):
    from .watcher import add_watch
    return _ok(add_watch(args["repo"], int(args["number"]), int(args.get("interval_sec") or 120)))


@tool("unwatch_pr", "Stop watching a PR.", {"repo": str, "number": int})
async def unwatch_pr(args):
    from .watcher import remove_watch
    return _ok(remove_watch(args["repo"], int(args["number"])))


@tool("list_watched_prs", "List PRs currently being watched.", {})
async def list_watched_prs(args):
    from .watcher import list_watches
    items = list_watches()
    if not items:
        return _ok("(no watches)")
    lines = []
    for e in items:
        last = time.strftime("%H:%M:%S", time.localtime(e.last_check)) if e.last_check else "-"
        err = f" err={e.last_error[:40]}" if e.last_error else ""
        lines.append(
            f"- {e.key} every {e.interval}s | state={e.last_state or '?'} | last={last}{err}"
        )
    return _ok("\n".join(lines))


def build_server():
    return create_sdk_mcp_server(
        name="friday",
        version="0.1.0",
        tools=[
            add_task,
            list_tasks,
            update_task,
            get_task,
            dispatch_to_repo,
            send_to_job,
            finish_job,
            cancel_job,
            list_jobs,
            get_job,
            tail_job,
            watch_pr,
            unwatch_pr,
            list_watched_prs,
        ],
    )


ALLOWED_TOOLS = [
    "mcp__friday__add_task",
    "mcp__friday__list_tasks",
    "mcp__friday__update_task",
    "mcp__friday__get_task",
    "mcp__friday__dispatch_to_repo",
    "mcp__friday__send_to_job",
    "mcp__friday__finish_job",
    "mcp__friday__cancel_job",
    "mcp__friday__list_jobs",
    "mcp__friday__get_job",
    "mcp__friday__tail_job",
    "mcp__friday__watch_pr",
    "mcp__friday__unwatch_pr",
    "mcp__friday__list_watched_prs",
    "Task",
    "Read",
    "Bash",
]
