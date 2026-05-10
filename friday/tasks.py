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

TaskStatus = Literal["pending", "in_progress", "blocked", "done", "cancelled"]
JobStatus = Literal["running", "succeeded", "failed", "cancelled"]


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
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    summary: str = ""
    output_tail: list[str] = field(default_factory=list)


class Store:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if TASKS_FILE.exists():
            raw = json.loads(TASKS_FILE.read_text())
            self._tasks = {tid: Task(**t) for tid, t in raw.items()}
        if JOBS_FILE.exists():
            raw = json.loads(JOBS_FILE.read_text())
            self._jobs = {jid: Job(**j) for jid, j in raw.items()}

    def _save_tasks(self) -> None:
        TASKS_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in self._tasks.items()}, indent=2, ensure_ascii=False)
        )

    def _save_jobs(self) -> None:
        JOBS_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in self._jobs.items()}, indent=2, ensure_ascii=False)
        )

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

    def add_job(self, task_id: Optional[str], repo_path: str, instruction: str) -> Job:
        jid = uuid.uuid4().hex[:8]
        job = Job(id=jid, task_id=task_id, repo_path=repo_path, instruction=instruction)
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

    def append_notification(self, text: str) -> None:
        line = json.dumps({"ts": time.time(), "text": text}, ensure_ascii=False)
        with NOTIFS_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

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
