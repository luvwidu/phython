"""Custom MCP tools for the central orchestrator.

These tools let the orchestrator manage a shared task list and dispatch
work into other repositories as background Claude Code jobs.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool

from .tasks import store


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


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


# --- background dispatch into another repo ---------------------------------

_BG_TASKS: dict[str, asyncio.Task] = {}


def _msg_to_text(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                out.append(text)
        if out:
            return "\n".join(out)
    return ""


async def _run_dispatch(job_id: str, repo_path: str, instruction: str) -> None:
    try:
        opts = ClaudeAgentOptions(
            cwd=repo_path,
            permission_mode="acceptEdits",
            setting_sources=["project", "user"],
        )
        async for msg in query(prompt=instruction, options=opts):
            text = _msg_to_text(msg)
            if text:
                store.append_job_output(job_id, text)
        store.update_job(
            job_id, status="succeeded", finished_at=time.time(), summary="completed"
        )
    except Exception as e:  # noqa: BLE001
        store.update_job(
            job_id, status="failed", finished_at=time.time(), summary=f"error: {e}"
        )
    finally:
        _BG_TASKS.pop(job_id, None)


@tool(
    "dispatch_to_repo",
    "Run a Claude Code agent inside repo_path with the given instruction. Runs in the "
    "background; returns a job id. Optionally link to an existing task_id.",
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
    bg = asyncio.create_task(_run_dispatch(job.id, repo, args["instruction"]))
    _BG_TASKS[job.id] = bg
    if args.get("task_id"):
        try:
            store.update_task(args["task_id"], status="in_progress")
            store.append_note(args["task_id"], f"dispatched job {job.id} to {repo}")
        except KeyError:
            pass
    return _ok(f"started job {job.id} in {repo}")


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


@tool("cancel_job", "Cancel a running background job by id.", {"id": str})
async def cancel_job(args):
    jid = args["id"]
    bg = _BG_TASKS.get(jid)
    if not bg:
        return _ok(f"job {jid} not running")
    bg.cancel()
    store.update_job(jid, status="cancelled", finished_at=time.time(), summary="cancelled by user")
    return _ok(f"cancelled {jid}")


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
            list_jobs,
            get_job,
            cancel_job,
        ],
    )


ALLOWED_TOOLS = [
    "mcp__friday__add_task",
    "mcp__friday__list_tasks",
    "mcp__friday__update_task",
    "mcp__friday__get_task",
    "mcp__friday__dispatch_to_repo",
    "mcp__friday__list_jobs",
    "mcp__friday__get_job",
    "mcp__friday__cancel_job",
    "Task",
    "Read",
    "Bash",
]
