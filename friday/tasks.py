"""JSON-backed store for tasks and dispatched jobs."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
JOBS_FILE = DATA_DIR / "jobs.json"
NOTIFS_FILE = DATA_DIR / "notifications.jsonl"
PROJECTS_FILE = DATA_DIR / "projects.json"
STUDIO_FILE = DATA_DIR / "studio_runs.json"

TaskStatus = Literal["pending", "in_progress", "blocked", "done", "cancelled"]
JobStatus = Literal["running", "succeeded", "failed", "cancelled"]
StudioStatus = Literal["running", "completed", "synthesized", "failed", "cancelled"]


@dataclass
class Project:
    name: str
    repo_path: str = ""
    description: str = ""
    github_repo: str = ""  # owner/name for PR-watch convenience
    host: str = ""  # which machine the local checkout lives on (e.g. mac, windows)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class StudioRun:
    id: str
    project: str
    repo_path: str
    instruction: str
    agents: list[str]
    status: StudioStatus = "running"
    base_sha: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # per-agent results: {agent: {success, summary, files_changed, diff_size, error}}
    results: dict[str, dict] = field(default_factory=dict)
    # synthesis output (filled by Phase 4)
    synthesis: dict = field(default_factory=dict)


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: TaskStatus = "pending"
    project: str = ""
    owner: str = ""
    notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Job:
    id: str
    task_id: Optional[str]
    repo_path: str
    instruction: str
    status: JobStatus = "running"
    host: str = ""  # which machine actually runs the ClaudeSDKClient
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    summary: str = ""
    output_tail: list[str] = field(default_factory=list)


class Store:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[str, Job] = {}
        self._projects: dict[str, Project] = {}
        self._studio: dict[str, StudioRun] = {}
        self._load()

    def _load(self) -> None:
        if TASKS_FILE.exists():
            raw = json.loads(TASKS_FILE.read_text())
            self._tasks = {tid: Task(**t) for tid, t in raw.items()}
        if JOBS_FILE.exists():
            raw = json.loads(JOBS_FILE.read_text())
            self._jobs = {jid: Job(**j) for jid, j in raw.items()}
        if PROJECTS_FILE.exists():
            raw = json.loads(PROJECTS_FILE.read_text())
            self._projects = {n: Project(**p) for n, p in raw.items()}
        if STUDIO_FILE.exists():
            raw = json.loads(STUDIO_FILE.read_text())
            self._studio = {rid: StudioRun(**r) for rid, r in raw.items()}

    def _schedule_sync(self) -> None:
        try:
            from . import sync  # local import to avoid cycle
            sync.schedule_sync()
        except Exception:
            pass

    def _save_tasks(self) -> None:
        TASKS_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in self._tasks.items()}, indent=2, ensure_ascii=False)
        )
        self._schedule_sync()

    def _save_jobs(self) -> None:
        JOBS_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in self._jobs.items()}, indent=2, ensure_ascii=False)
        )
        self._schedule_sync()

    def _save_projects(self) -> None:
        PROJECTS_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in self._projects.items()}, indent=2, ensure_ascii=False)
        )
        self._schedule_sync()

    def _save_studio(self) -> None:
        STUDIO_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in self._studio.items()}, indent=2, ensure_ascii=False)
        )
        self._schedule_sync()

    # --- projects ---------------------------------------------------------

    def register_project(
        self,
        name: str,
        repo_path: str = "",
        description: str = "",
        github_repo: str = "",
        host: str = "",
    ) -> Project:
        if name in self._projects:
            p = self._projects[name]
            if repo_path:
                p.repo_path = repo_path
            if description:
                p.description = description
            if github_repo:
                p.github_repo = github_repo
            if host:
                p.host = host
            p.updated_at = time.time()
        else:
            p = Project(
                name=name,
                repo_path=repo_path,
                description=description,
                github_repo=github_repo,
                host=host,
            )
            self._projects[name] = p
        self._save_projects()
        return p

    def unregister_project(self, name: str) -> bool:
        if name in self._projects:
            del self._projects[name]
            self._save_projects()
            return True
        return False

    def get_project(self, name: str) -> Optional[Project]:
        return self._projects.get(name)

    def list_projects(self) -> list[Project]:
        return sorted(self._projects.values(), key=lambda p: p.name)

    def add_task(self, title: str, description: str = "", project: str = "", owner: str = "") -> Task:
        tid = uuid.uuid4().hex[:8]
        task = Task(id=tid, title=title, description=description, project=project, owner=owner)
        self._tasks[tid] = task
        self._save_tasks()
        return task

    def update_task(self, tid: str, **fields_) -> Task:
        if tid not in self._tasks:
            raise KeyError(tid)
        t = self._tasks[tid]
        for k, v in fields_.items():
            if v in (None, ""):
                continue
            if hasattr(t, k):
                setattr(t, k, v)
        t.updated_at = time.time()
        self._save_tasks()
        return t

    def append_note(self, tid: str, note: str) -> None:
        t = self._tasks[tid]
        t.notes.append(f"[{time.strftime('%Y-%m-%d %H:%M')}] {note}")
        t.updated_at = time.time()
        self._save_tasks()

    def list_tasks(self, status: Optional[str] = None) -> list[Task]:
        items = list(self._tasks.values())
        if status:
            items = [t for t in items if t.status == status]
        items.sort(key=lambda t: t.updated_at, reverse=True)
        return items

    def get_task(self, tid: str) -> Optional[Task]:
        return self._tasks.get(tid)

    def add_job(
        self,
        task_id: Optional[str],
        repo_path: str,
        instruction: str,
        host: str = "",
    ) -> Job:
        jid = uuid.uuid4().hex[:8]
        job = Job(
            id=jid, task_id=task_id, repo_path=repo_path,
            instruction=instruction, host=host,
        )
        self._jobs[jid] = job
        self._save_jobs()
        return job

    def update_job(self, jid: str, **fields_) -> Job:
        j = self._jobs[jid]
        for k, v in fields_.items():
            if hasattr(j, k):
                setattr(j, k, v)
        self._save_jobs()
        return j

    def append_job_output(self, jid: str, line: str, max_tail: int = 80) -> None:
        j = self._jobs[jid]
        j.output_tail.append(line)
        if len(j.output_tail) > max_tail:
            j.output_tail = j.output_tail[-max_tail:]
        self._save_jobs()

    def list_jobs(self, status: Optional[str] = None) -> list[Job]:
        items = list(self._jobs.values())
        if status:
            items = [j for j in items if j.status == status]
        items.sort(key=lambda j: j.started_at, reverse=True)
        return items

    def get_job(self, jid: str) -> Optional[Job]:
        return self._jobs.get(jid)

    # --- studio runs ------------------------------------------------------

    def add_studio_run(
        self, run_id: str, project: str, repo_path: str,
        instruction: str, agents: list[str], base_sha: str = "",
    ) -> StudioRun:
        run = StudioRun(
            id=run_id, project=project, repo_path=repo_path,
            instruction=instruction, agents=list(agents), base_sha=base_sha,
        )
        self._studio[run_id] = run
        self._save_studio()
        return run

    def update_studio_run(self, run_id: str, **fields_) -> StudioRun:
        r = self._studio[run_id]
        for k, v in fields_.items():
            if hasattr(r, k):
                setattr(r, k, v)
        self._save_studio()
        return r

    def set_studio_result(self, run_id: str, agent: str, result: dict) -> None:
        r = self._studio[run_id]
        r.results[agent] = result
        self._save_studio()

    def get_studio_run(self, run_id: str) -> Optional[StudioRun]:
        return self._studio.get(run_id)

    def list_studio_runs(self) -> list[StudioRun]:
        return sorted(self._studio.values(), key=lambda r: r.started_at, reverse=True)

    def append_notification(self, text: str) -> None:
        try:
            from . import sync
            host = sync.machine() if sync.is_enabled() else ""
        except Exception:
            host = ""
        tagged = f"[{host}] {text}" if host else text
        line = json.dumps({"ts": time.time(), "text": tagged}, ensure_ascii=False)
        with NOTIFS_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._schedule_sync()

    def pop_notifications(self) -> list[dict]:
        if not NOTIFS_FILE.exists():
            return []
        raw = NOTIFS_FILE.read_text(encoding="utf-8").strip()
        NOTIFS_FILE.write_text("", encoding="utf-8")
        out: list[dict] = []
        for ln in raw.splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


store = Store()
