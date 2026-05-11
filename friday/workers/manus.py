"""Manus worker — submits a task to the Manus cloud API and waits for results.

Manus is a remote autonomous agent. Unlike Claude/Gemini which edit files in
the local worktree, Manus runs in its own sandbox and returns text + file
artifacts. We download those into the worktree, then commit + diff like the
other workers so studio.py treats it uniformly.

Defensive design: the public Manus API docs are partially gated, so we try
multiple plausible auth headers and response field names. The full API
response is captured in output_tail for debugging when something doesn't
match expectations.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from . import BaseWorker, WorkerResult

ENV_KEY = "MANUS_API_KEY"
ENV_BASE = "MANUS_BASE_URL"
DEFAULT_BASE = "https://api.manus.ai/v1"

POLL_INTERVAL = 15            # seconds between status checks
POLL_TIMEOUT = 30 * 60        # 30 minutes max wait
HTTP_TIMEOUT = 60             # per-request timeout


def _auth_headers(key: str) -> dict[str, str]:
    """Send both auth styles since docs disagree; one will be ignored."""
    return {
        "API_KEY": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _first(d: dict, *keys: str) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


class ManusWorker(BaseWorker):
    agent_name = "manus"

    async def run(self, instruction: str) -> WorkerResult:
        tail: list[str] = []
        api_key = os.environ.get(ENV_KEY, "").strip()
        if not api_key:
            return WorkerResult(
                agent=self.agent_name, success=False, diff="",
                summary=f"{ENV_KEY} not set in environment",
                error="missing API key",
                output_tail=tail,
            )
        base_url = os.environ.get(ENV_BASE, DEFAULT_BASE).rstrip("/")
        headers = _auth_headers(api_key)

        try:
            async with httpx.AsyncClient(
                base_url=base_url, headers=headers, timeout=HTTP_TIMEOUT,
            ) as client:
                task_id = await self._submit(client, instruction, tail)
                if not task_id:
                    return WorkerResult(
                        agent=self.agent_name, success=False, diff="",
                        summary="manus submit failed (see output_tail)",
                        output_tail=tail, error="submit failed",
                    )

                final = await self._poll(client, task_id, tail)
                if final is None:
                    return WorkerResult(
                        agent=self.agent_name, success=False, diff="",
                        summary=f"manus polling failed or timed out",
                        output_tail=tail, error="poll failed/timeout",
                    )

                return await self._collect_outputs(client, final, instruction, tail)

        except httpx.HTTPError as e:
            return WorkerResult(
                agent=self.agent_name, success=False, diff="",
                summary=f"manus http error: {e}",
                output_tail=tail, error=str(e),
            )
        except Exception as e:  # noqa: BLE001
            return WorkerResult(
                agent=self.agent_name, success=False, diff="",
                summary=f"manus error: {e}",
                output_tail=tail, error=str(e),
            )

    async def _submit(
        self, client: httpx.AsyncClient, instruction: str, tail: list[str],
    ) -> Optional[str]:
        body = {
            "prompt": instruction,
            "agentProfile": "quality",
            "taskMode": "agent",
            "hidden": True,
        }
        # Try the documented endpoint, plus one fallback shape
        for path in ("/tasks", "/agent/tasks"):
            tail.append(f"POST {path}")
            try:
                resp = await client.post(path, json=body)
            except httpx.HTTPError as e:
                tail.append(f"  http error: {e}")
                continue
            tail.append(f"  status: {resp.status_code}")
            if resp.status_code in (404,):
                continue
            text_head = resp.text[:300].replace("\n", " ")
            tail.append(f"  body: {text_head}")
            if resp.status_code >= 400:
                # Surface error and try next path
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                continue
            tid = _first(data, "task_id", "id", "taskId")
            if isinstance(tid, str):
                tail.append(f"  task_id: {tid}")
                return tid
            # Some APIs nest under "data" or "task"
            for nest in ("data", "task", "result"):
                inner = data.get(nest)
                if isinstance(inner, dict):
                    tid = _first(inner, "task_id", "id", "taskId")
                    if isinstance(tid, str):
                        tail.append(f"  task_id (nested in {nest}): {tid}")
                        return tid
        return None

    async def _poll(
        self, client: httpx.AsyncClient, task_id: str, tail: list[str],
    ) -> Optional[dict]:
        elapsed = 0
        path_attempts = (f"/tasks/{task_id}", f"/agent/tasks/{task_id}")
        good_path: Optional[str] = None
        while elapsed < POLL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            paths = (good_path,) if good_path else path_attempts
            for path in paths:
                try:
                    resp = await client.get(path)
                except httpx.HTTPError as e:
                    tail.append(f"poll {elapsed}s {path}: http error {e}")
                    continue
                if resp.status_code == 404:
                    continue
                good_path = path
                if resp.status_code >= 400:
                    tail.append(
                        f"poll {elapsed}s: HTTP {resp.status_code} {resp.text[:120]}"
                    )
                    break
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    tail.append(f"poll {elapsed}s: non-JSON response")
                    break
                # peel one wrapper if present
                if "task" in data and isinstance(data["task"], dict):
                    data = data["task"]
                status = (_first(data, "status", "state") or "").lower()
                tail.append(f"poll {elapsed}s: status={status}")
                if status in ("completed", "succeeded", "success", "done"):
                    return data
                if status in ("failed", "error", "cancelled", "canceled"):
                    err = _first(data, "error", "error_message", "message") or ""
                    tail.append(f"manus terminal status: {err[:200]}")
                    return data
                break
        return None

    async def _collect_outputs(
        self, client: httpx.AsyncClient, task_data: dict,
        instruction: str, tail: list[str],
    ) -> WorkerResult:
        status = (_first(task_data, "status", "state") or "").lower()
        if status not in ("completed", "succeeded", "success", "done"):
            return WorkerResult(
                agent=self.agent_name, success=False, diff="",
                summary=f"manus ended in status={status}",
                output_tail=tail, error=json.dumps(task_data)[:500],
            )

        # Try to pull a primary text result
        text_output = ""
        for field in ("output", "result", "summary", "text", "content", "answer"):
            v = task_data.get(field)
            if isinstance(v, str) and v.strip():
                text_output = v
                break
        if not text_output:
            msgs = task_data.get("messages")
            if isinstance(msgs, list):
                for m in reversed(msgs):
                    content = m.get("content") if isinstance(m, dict) else None
                    if isinstance(content, str) and content.strip():
                        text_output = content
                        break
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                                text_output = blk["text"]
                                break
                        if text_output:
                            break

        # Find file artifacts
        files_written: list[str] = []
        artifacts = None
        for field in ("artifacts", "outputs", "files", "attachments"):
            v = task_data.get(field)
            if isinstance(v, list) and v:
                artifacts = v
                break
        if artifacts:
            for art in artifacts:
                if not isinstance(art, dict):
                    continue
                url = _first(art, "url", "download_url", "signed_url", "uri")
                name = _first(art, "name", "filename", "path", "title")
                if not url or not name:
                    continue
                try:
                    r = await client.get(url, follow_redirects=True)
                    if r.status_code == 200:
                        out = Path(self.worktree_path) / Path(name).name
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(r.content)
                        files_written.append(out.name)
                        tail.append(f"downloaded artifact: {out.name}")
                except Exception as e:  # noqa: BLE001
                    tail.append(f"artifact download failed for {name}: {e}")

        # If no file artifacts but Manus produced text, save as MANUS_OUTPUT.md
        if not files_written and text_output:
            out = Path(self.worktree_path) / "MANUS_OUTPUT.md"
            out.write_text(text_output, encoding="utf-8")
            files_written.append("MANUS_OUTPUT.md")
            tail.append("saved text output as MANUS_OUTPUT.md")

        if not files_written:
            return WorkerResult(
                agent=self.agent_name, success=False, diff="",
                summary="manus completed but produced no usable output",
                output_tail=tail,
                error=f"raw response keys: {list(task_data.keys())}",
            )

        await self.commit_changes(f"[manus] {instruction[:60]}")
        diff, files = await self.collect_diff()
        summary = (text_output[:200] if text_output else
                   f"manus produced {len(files_written)} file(s)")
        return WorkerResult(
            agent=self.agent_name, success=True, diff=diff,
            summary=summary, output_tail=tail, files_changed=files,
        )
