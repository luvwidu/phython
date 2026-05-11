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


def _resolve_repo(project_name: str = "", repo_path: str = "") -> tuple[Optional[str], str]:
    """Return (resolved_path, error_message). Either project_name or repo_path must be set."""
    if project_name:
        proj = store.get_project(project_name)
        if not proj:
            return None, f"unknown project: {project_name}"
        if not proj.repo_path:
            return None, f"project '{project_name}' has no repo_path"
        p = Path(proj.repo_path).expanduser()
        if not p.exists():
            return None, f"project '{project_name}' repo_path missing: {proj.repo_path}"
        return str(p.resolve()), ""
    if repo_path:
        p = Path(repo_path).expanduser()
        if not p.exists():
            return None, f"repo path does not exist: {repo_path}"
        return str(p.resolve()), ""
    return None, "must provide project or repo_path"


# --- project tools --------------------------------------------------------


@tool(
    "register_project",
    "Register or update a project Friday should oversee. name is the short alias "
    "you'll use everywhere (e.g. 'phython'). repo_path is the local checkout on "
    "THIS machine. github_repo is the owner/name form for PR watching. host is the "
    "machine alias where the repo lives (e.g. mac, windows) — defaults to current.",
    {"name": str, "repo_path": str, "description": str, "github_repo": str, "host": str},
)
async def register_project(args):
    from . import sync
    host = args.get("host") or (sync.machine() if sync.is_enabled() else "")
    p = store.register_project(
        name=args["name"],
        repo_path=args.get("repo_path", ""),
        description=args.get("description", ""),
        github_repo=args.get("github_repo", ""),
        host=host,
    )
    return _ok(
        f"registered '{p.name}' → {p.repo_path or '(no path)'} "
        f"host={p.host or '-'} github={p.github_repo or '-'}"
    )


@tool("unregister_project", "Remove a project from Friday's registry.", {"name": str})
async def unregister_project(args):
    if store.unregister_project(args["name"]):
        return _ok(f"unregistered {args['name']}")
    return _ok(f"no project named {args['name']}")


@tool("list_projects", "List all registered projects with quick counts.", {})
async def list_projects(args):
    projects = store.list_projects()
    if not projects:
        return _ok("(no projects registered)")
    all_tasks = store.list_tasks()
    all_jobs = store.list_jobs()
    lines = []
    for p in projects:
        open_tasks = [
            t for t in all_tasks
            if t.project == p.name and t.status in ("pending", "in_progress", "blocked")
        ]
        running_jobs = [
            j for j in all_jobs
            if j.repo_path == p.repo_path and j.status == "running"
        ]
        lines.append(
            f"- {p.name} | host={p.host or '-'} | {p.repo_path or '(no path)'} | "
            f"github={p.github_repo or '-'} | open_tasks={len(open_tasks)} "
            f"running_jobs={len(running_jobs)}"
        )
    return _ok("\n".join(lines))


@tool(
    "get_project",
    "Show one project's full state: description, repo, open tasks, recent jobs, watched PRs.",
    {"name": str},
)
async def get_project(args):
    name = args["name"]
    p = store.get_project(name)
    if not p:
        return _ok(f"no project named {name}")

    open_tasks = [
        t for t in store.list_tasks()
        if t.project == name and t.status in ("pending", "in_progress", "blocked")
    ]
    done_recent = [
        t for t in store.list_tasks(status="done")
        if t.project == name
    ][:5]
    recent_jobs = [
        j for j in store.list_jobs() if j.repo_path == p.repo_path
    ][:5]

    from .watcher import list_watches
    watches = [w for w in list_watches() if w.repo == p.github_repo]

    parts = [
        f"name: {p.name}",
        f"repo_path: {p.repo_path or '(none)'}",
        f"github_repo: {p.github_repo or '(none)'}",
        f"description: {p.description or '(none)'}",
        "",
        f"open tasks ({len(open_tasks)}):",
    ]
    parts += [f"  - [{t.status}] {t.id} {t.title}" for t in open_tasks] or ["  (none)"]
    parts.append(f"\nrecently done ({len(done_recent)}):")
    parts += [f"  - {t.id} {t.title}" for t in done_recent] or ["  (none)"]
    parts.append(f"\nrecent jobs ({len(recent_jobs)}):")
    parts += [
        f"  - {j.id} [{j.status}] {j.summary or j.instruction[:60]}"
        for j in recent_jobs
    ] or ["  (none)"]
    parts.append(f"\nwatched PRs ({len(watches)}):")
    parts += [
        f"  - #{w.number} state={w.last_state or '?'}"
        for w in watches
    ] or ["  (none)"]

    return _ok("\n".join(parts))


@tool(
    "overview",
    "Cross-project dashboard grouped by host machine: per project, show open task / "
    "running job / watched PR counts and surface recent activity. The first thing to "
    "call when checking 'how is everything'.",
    {},
)
async def overview(args):
    from . import sync
    projects = store.list_projects()
    all_tasks = store.list_tasks()
    all_jobs = store.list_jobs()
    from .watcher import list_watches
    watches = list_watches()

    current = sync.machine() if sync.is_enabled() else ""
    header = "── Friday overview ──"
    if current:
        header += f"  (this machine: {current})"
    lines = [header]
    if not projects:
        lines.append("(no projects registered — use register_project to add one)")

    by_host: dict[str, list] = {}
    for p in projects:
        by_host.setdefault(p.host or "(unspecified)", []).append(p)

    for host_name in sorted(by_host):
        marker = " ←current" if host_name == current else ""
        lines.append(f"\n[host: {host_name}]{marker}")
        for p in by_host[host_name]:
            open_tasks = [
                t for t in all_tasks
                if t.project == p.name and t.status in ("pending", "in_progress", "blocked")
            ]
            running_jobs = [
                j for j in all_jobs
                if j.repo_path == p.repo_path and j.status == "running"
            ]
            proj_watches = [w for w in watches if w.repo == p.github_repo]
            in_prog = [t.title for t in open_tasks if t.status == "in_progress"][:3]
            focus = f" focus: {'; '.join(in_prog)}" if in_prog else ""
            lines.append(
                f"  • {p.name}: {len(open_tasks)} open / {len(running_jobs)} running / "
                f"{len(proj_watches)} watched{focus}"
            )

    orphan_tasks = [
        t for t in all_tasks
        if t.status in ("pending", "in_progress", "blocked")
        and (not t.project or not store.get_project(t.project))
    ]
    if orphan_tasks:
        lines.append(f"\nunassigned open tasks ({len(orphan_tasks)}):")
        lines += [f"  - [{t.status}] {t.id} {t.title}" for t in orphan_tasks[:5]]

    running = [j for j in all_jobs if j.status == "running"]
    if running:
        lines.append(f"\nrunning jobs ({len(running)}):")
        for j in running[:5]:
            tag = f"[{j.host}] " if j.host else ""
            lines.append(f"  - {tag}{j.id} {j.repo_path}: {j.instruction[:60]}")

    return _ok("\n".join(lines))


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
    "Start a background Claude Code agent in a project. Provide either `project` "
    "(registered alias, preferred) or `repo_path` (raw path). The agent stays alive — "
    "use send_to_job for follow-ups, tail_job to peek, finish_job to end politely, "
    "cancel_job to hard-stop. Optionally link to an existing task_id.",
    {"project": str, "repo_path": str, "instruction": str, "task_id": str},
)
async def dispatch_to_repo(args):
    from . import sync
    repo, err = _resolve_repo(args.get("project", ""), args.get("repo_path", ""))
    if err:
        return _ok(err)
    host = sync.machine() if sync.is_enabled() else ""
    job = store.add_job(
        task_id=(args.get("task_id") or None),
        repo_path=repo,
        instruction=args["instruction"],
        host=host,
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


def _job_not_local_msg(jid: str) -> Optional[str]:
    """Return a helpful message if the job lives on another machine, else None."""
    from . import sync
    j = store.get_job(jid)
    if not j:
        return f"no job with id {jid}"
    if sync.is_enabled() and j.host and j.host != sync.machine():
        return (
            f"job {jid} runs on '{j.host}', not this machine ('{sync.machine()}'); "
            f"switch to that machine to control it"
        )
    return None


@tool(
    "send_to_job",
    "Send a follow-up instruction to a running job. The job must be running on this machine.",
    {"id": str, "message": str},
)
async def send_to_job(args):
    runner = _RUNNERS.get(args["id"])
    if not runner:
        return _ok(_job_not_local_msg(args["id"]) or f"no running job with id {args['id']}")
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
        return _ok(_job_not_local_msg(args["id"]) or f"no running job with id {args['id']}")
    await runner.queue.put(None)
    return _ok(f"finish signal sent to {args['id']}")


@tool("cancel_job", "Hard-cancel a running background job by id.", {"id": str})
async def cancel_job(args):
    runner = _RUNNERS.get(args["id"])
    if not runner or not runner.task:
        return _ok(_job_not_local_msg(args["id"]) or f"job {args['id']} not running")
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


# --- Studio: multi-agent ensemble runs -----------------------------------


@tool(
    "studio_run",
    "Dispatch the same instruction to multiple coding agents in parallel, each in "
    "its own isolated git worktree. agents is a comma-separated list (default: "
    "'claude,gemini'). If interactive=true, workers stay alive after the first "
    "turn so you can steer them mid-flight via send_to_studio_agent and end them "
    "with finish_studio_run. Default is one-shot (interactive=false). "
    "Returns a run_id.",
    {"project": str, "instruction": str, "agents": str, "interactive": bool},
)
async def studio_run(args):
    from . import studio
    raw_agents = (args.get("agents") or "claude,gemini").strip()
    agents = [a.strip() for a in raw_agents.split(",") if a.strip()]
    interactive = bool(args.get("interactive") or False)
    run_id, msg = await studio.start_run(
        project_name=args["project"],
        instruction=args["instruction"],
        agents=agents,
        interactive=interactive,
    )
    return _ok(msg)


@tool(
    "send_to_studio_agent",
    "Send a mid-flight instruction to ONE agent inside an active interactive "
    "studio run. Only works while the run is still alive and was started with "
    "interactive=true. Claude supports follow-ups fully; Gemini ignores them "
    "(first turn only — for now).",
    {"id": str, "agent": str, "message": str},
)
async def send_to_studio_agent(args):
    from . import studio
    ok, msg = await studio.send_to_agent(
        args["id"], args["agent"], args["message"],
    )
    return _ok(msg)


@tool(
    "finish_studio_run",
    "Tell all agents in an interactive studio run to wrap up after their current "
    "turn. Each agent exits cleanly with the work they have so far. Use this "
    "instead of cancel_studio_run when you want their output preserved.",
    {"id": str},
)
async def finish_studio_run(args):
    from . import studio
    ok, msg = await studio.finish_run(args["id"])
    return _ok(msg)


@tool(
    "list_studio_runs",
    "List recent studio runs (newest first).",
    {},
)
async def list_studio_runs(args):
    runs = store.list_studio_runs()
    if not runs:
        return _ok("(no studio runs yet)")
    lines = []
    for r in runs[:20]:
        agents = ",".join(r.agents)
        lines.append(
            f"- {r.id} [{r.status}] project={r.project} agents={agents} "
            f"task={r.instruction[:60]}"
        )
    return _ok("\n".join(lines))


@tool(
    "get_studio_run",
    "Show one studio run: per-agent summary, file lists, diff sizes, and (if "
    "synthesized) the chosen result.",
    {"id": str},
)
async def get_studio_run(args):
    r = store.get_studio_run(args["id"])
    if not r:
        return _ok("run not found")
    lines = [
        f"id: {r.id}",
        f"project: {r.project}",
        f"repo: {r.repo_path}",
        f"status: {r.status}",
        f"base: {r.base_sha[:10]}",
        f"task: {r.instruction}",
        f"agents: {', '.join(r.agents)}",
        "",
        "results:",
    ]
    for agent in r.agents:
        res = r.results.get(agent)
        if not res:
            lines.append(f"  {agent}: (no result yet)")
            continue
        status = "✓" if res.get("success") else "✗"
        files = res.get("files_changed", [])
        lines.append(
            f"  {agent}: {status} {res.get('summary','')[:80]} "
            f"({len(files)} files, {res.get('diff_size',0)} bytes diff)"
        )
        for f in files[:5]:
            lines.append(f"      - {f}")
        if res.get("error"):
            lines.append(f"      ! {res['error'][:120]}")
    if r.synthesis:
        lines.append("")
        lines.append(f"synthesis: strategy={r.synthesis.get('strategy')} "
                     f"chosen={r.synthesis.get('chosen')}")
    return _ok("\n".join(lines))


@tool(
    "synthesize_studio_run",
    "Have Friday compare the agents' diffs and decide. strategy='pick_best' (default) "
    "picks one diff as the winner; 'merge' produces an integrated diff combining "
    "strengths of each. Run must be in 'completed' status.",
    {"id": str, "strategy": str},
)
async def synthesize_studio_run(args):
    from . import studio
    strategy = args.get("strategy") or "pick_best"
    if strategy not in {"pick_best", "merge"}:
        return _ok(f"unknown strategy: {strategy} (use pick_best or merge)")
    ok, msg = await studio.synthesize(args["id"], strategy)
    return _ok(msg)


@tool(
    "apply_studio_result",
    "Apply the synthesized diff onto a new branch in the project repo. The branch "
    "is created from the same base commit the run forked from. Changes are left "
    "uncommitted so the user can review before committing. Worktrees and the "
    "friday/<run>/<agent> branches are auto-cleaned on success unless "
    "auto_cleanup=false.",
    {"id": str, "branch_name": str, "auto_cleanup": bool},
)
async def apply_studio_result(args):
    from . import studio
    auto = args.get("auto_cleanup")
    if auto is None:
        auto = True
    ok, msg = await studio.apply_result(args["id"], args["branch_name"], bool(auto))
    return _ok(msg)


@tool(
    "cleanup_studio_run",
    "Remove the run's worktrees under ~/.friday/worktrees/<id>/ and delete the "
    "associated friday/<id>/<agent> branches in the project repo. Call this if "
    "you applied a result manually or want to reclaim disk + branch list.",
    {"id": str},
)
async def cleanup_studio_run(args):
    from . import studio
    msg = await studio.cleanup_run_artifacts(args["id"])
    return _ok(msg)


@tool(
    "cancel_studio_run",
    "Cancel a running studio run. Worktrees for that run are NOT auto-cleaned — "
    "use get_studio_run to inspect, then they get reused on next run.",
    {"id": str},
)
async def cancel_studio_run(args):
    from . import studio
    return _ok(studio.cancel_run(args["id"]))


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
            register_project,
            unregister_project,
            list_projects,
            get_project,
            overview,
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
            studio_run,
            send_to_studio_agent,
            finish_studio_run,
            list_studio_runs,
            get_studio_run,
            synthesize_studio_run,
            apply_studio_result,
            cleanup_studio_run,
            cancel_studio_run,
        ],
    )


ALLOWED_TOOLS = [
    "mcp__friday__register_project",
    "mcp__friday__unregister_project",
    "mcp__friday__list_projects",
    "mcp__friday__get_project",
    "mcp__friday__overview",
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
    "mcp__friday__studio_run",
    "mcp__friday__send_to_studio_agent",
    "mcp__friday__finish_studio_run",
    "mcp__friday__list_studio_runs",
    "mcp__friday__get_studio_run",
    "mcp__friday__synthesize_studio_run",
    "mcp__friday__apply_studio_result",
    "mcp__friday__cleanup_studio_run",
    "mcp__friday__cancel_studio_run",
    "Task",
    "Read",
    "Bash",
]
