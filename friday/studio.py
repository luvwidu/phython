"""Friday Studio — multi-agent ensemble orchestration.

A "studio run" dispatches the same instruction to multiple coding agents
(Claude, Gemini, ...) in isolated git worktrees, collects their diffs, and
lets Friday synthesize a final result.

Public entry points:
  start_run(project, instruction, agents)  -> run_id
  synthesize(run_id, strategy)             -> updates the run with synthesis
  apply_result(run_id, branch_name)        -> writes the synthesized diff
                                              onto a real branch in the project
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, query as claude_query

from . import worktree
from .tasks import store
from .workers import BaseWorker, WorkerResult
from .workers.claude import ClaudeWorker
from .workers.gemini import GeminiWorker


WORKER_REGISTRY: dict[str, type[BaseWorker]] = {
    "claude": ClaudeWorker,
    "gemini": GeminiWorker,
}

_TASKS: dict[str, asyncio.Task] = {}


def available_agents() -> list[str]:
    return list(WORKER_REGISTRY.keys())


def _notify(run_id: str, msg: str) -> None:
    store.append_notification(f"[studio {run_id}] {msg}")


async def _run_one_worker(
    run_id: str, agent: str, repo_path: str, instruction: str, base_sha: str,
) -> WorkerResult:
    wt_path, _ = await worktree.create_worktree(
        repo_path, run_id, agent, base=base_sha,
    )
    cls = WORKER_REGISTRY[agent]
    w = cls(str(wt_path), base_ref=base_sha)
    return await w.run(instruction)


async def _run_loop(run_id: str) -> None:
    run = store.get_studio_run(run_id)
    if not run:
        return
    try:
        coros = [
            _run_one_worker(run_id, a, run.repo_path, run.instruction, run.base_sha)
            for a in run.agents
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for agent, res in zip(run.agents, results):
            if isinstance(res, Exception):
                store.set_studio_result(run_id, agent, {
                    "success": False,
                    "summary": f"crashed: {res}",
                    "files_changed": [],
                    "diff_size": 0,
                    "error": str(res),
                    "output_tail": [],
                })
            else:
                store.set_studio_result(run_id, agent, {
                    "success": res.success,
                    "summary": res.summary,
                    "files_changed": res.files_changed,
                    "diff_size": len(res.diff),
                    "diff": res.diff,
                    "error": res.error,
                    "output_tail": res.output_tail[-30:],
                })
        store.update_studio_run(
            run_id, status="completed", finished_at=time.time(),
        )
        succ = sum(1 for a in run.agents
                   if store.get_studio_run(run_id).results.get(a, {}).get("success"))
        _notify(run_id, f"completed: {succ}/{len(run.agents)} agents succeeded")
    except asyncio.CancelledError:
        store.update_studio_run(
            run_id, status="cancelled", finished_at=time.time(),
        )
        _notify(run_id, "cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        store.update_studio_run(
            run_id, status="failed", finished_at=time.time(),
        )
        _notify(run_id, f"failed: {e}")
    finally:
        _TASKS.pop(run_id, None)


async def start_run(
    project_name: str, instruction: str, agents: list[str],
) -> tuple[Optional[str], str]:
    """Returns (run_id_or_None, message)."""
    proj = store.get_project(project_name)
    if not proj:
        return None, f"unknown project: {project_name}"
    if not proj.repo_path:
        return None, f"project '{project_name}' has no repo_path"
    repo = Path(proj.repo_path).expanduser()
    if not repo.exists():
        return None, f"repo missing on disk: {proj.repo_path}"
    bad = [a for a in agents if a not in WORKER_REGISTRY]
    if bad:
        return None, f"unknown agents: {bad}; available: {list(WORKER_REGISTRY)}"
    if not agents:
        return None, "no agents specified"

    try:
        base_sha = await worktree.base_ref(str(repo))
    except RuntimeError as e:
        return None, str(e)

    run_id = uuid.uuid4().hex[:8]
    store.add_studio_run(
        run_id=run_id, project=project_name, repo_path=str(repo),
        instruction=instruction, agents=agents, base_sha=base_sha,
    )
    task = asyncio.create_task(_run_loop(run_id))
    _TASKS[run_id] = task
    return run_id, f"started studio run {run_id} with {len(agents)} agents on {project_name}"


def cancel_run(run_id: str) -> str:
    task = _TASKS.get(run_id)
    if not task:
        return f"no active run with id {run_id}"
    task.cancel()
    return f"cancelling {run_id}"


# --- synthesis (Phase 4) -------------------------------------------------


SYNTH_SYSTEM = """\
You are the editor in a multi-agent coding studio. Several AI coding agents
were given the same task and each produced a diff. Your job is to compare
the diffs and either:
  - PICK: declare one diff the winner, or
  - MERGE: produce a new unified diff that combines the best parts.

Be concise. When you pick, explain in 2-3 bullets why. When you merge,
output ONLY the unified diff inside a single ```diff fence; explain in 2-3
bullets above the fence.
"""


def _truncate(s: str, n: int = 6000) -> str:
    if len(s) <= n:
        return s
    return s[: n // 2] + "\n...[truncated]...\n" + s[-n // 2:]


async def synthesize(run_id: str, strategy: str = "pick_best") -> tuple[bool, str]:
    """Run Friday-as-editor on the completed run. Returns (success, message)."""
    run = store.get_studio_run(run_id)
    if not run:
        return False, f"no run {run_id}"
    if run.status != "completed":
        return False, f"run {run_id} status is {run.status}; need 'completed'"

    successful = {
        a: r for a, r in run.results.items() if r.get("success") and r.get("diff")
    }
    if not successful:
        store.update_studio_run(run_id, status="failed")
        return False, "no successful agent with a non-empty diff"
    if len(successful) == 1 and strategy != "merge":
        only = list(successful)[0]
        store.update_studio_run(run_id, synthesis={
            "strategy": "pick_best",
            "chosen": only,
            "reasoning": "only one successful agent",
            "final_diff": successful[only]["diff"],
        }, status="synthesized")
        return True, f"only {only} had output — auto-picked"

    prompt_parts = [
        f"# Task\n{run.instruction}\n",
        f"# Strategy: {strategy}",
    ]
    for agent, r in successful.items():
        prompt_parts.append(
            f"\n## Agent: {agent}\n"
            f"summary: {r.get('summary','')[:300]}\n"
            f"files: {', '.join(r.get('files_changed', []))}\n"
            f"```diff\n{_truncate(r['diff'])}\n```\n"
        )
    if strategy == "pick_best":
        prompt_parts.append(
            "\nDecide which agent's diff to keep as the winner. "
            "Respond starting with `CHOICE: <agent_name>` on its own line, "
            "then 2-3 bullet reasons."
        )
    else:  # merge
        prompt_parts.append(
            "\nProduce a unified diff that combines the strengths of each. "
            "Use one ```diff fence containing the final diff. The diff MUST "
            "apply cleanly from the base. Above the fence, give 2-3 bullets "
            "explaining what you took from each agent."
        )
    prompt = "\n".join(prompt_parts)

    opts = ClaudeAgentOptions(
        system_prompt=SYNTH_SYSTEM,
        permission_mode="default",
        setting_sources=[],
        allowed_tools=[],
    )
    response_text_parts: list[str] = []
    try:
        async for msg in claude_query(prompt=prompt, options=opts):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        response_text_parts.append(text)
    except Exception as e:  # noqa: BLE001
        return False, f"synthesis call failed: {e}"
    response = "\n".join(response_text_parts).strip()

    synthesis: dict = {"strategy": strategy, "raw_response": response}
    if strategy == "pick_best":
        chosen: Optional[str] = None
        for line in response.splitlines():
            if line.upper().startswith("CHOICE:"):
                cand = line.split(":", 1)[1].strip().split()[0]
                if cand in successful:
                    chosen = cand
                    break
        if not chosen:
            return False, f"could not parse CHOICE from response: {response[:200]}"
        synthesis.update({
            "chosen": chosen,
            "final_diff": successful[chosen]["diff"],
            "reasoning": response,
        })
    else:  # merge
        diff_text = _extract_fenced_diff(response)
        if not diff_text:
            return False, "merge response had no ```diff fence"
        synthesis.update({
            "chosen": "merged",
            "final_diff": diff_text,
            "reasoning": response,
        })

    store.update_studio_run(run_id, synthesis=synthesis, status="synthesized")
    _notify(run_id, f"synthesized via {strategy} → {synthesis.get('chosen')}")
    return True, f"synthesized: {synthesis.get('chosen')}"


def _extract_fenced_diff(text: str) -> str:
    lines = text.splitlines()
    in_fence = False
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("```diff"):
            in_fence = True
            continue
        if in_fence and s.startswith("```"):
            break
        if in_fence:
            out.append(ln)
    return "\n".join(out).strip()


async def apply_result(
    run_id: str, branch_name: str, auto_cleanup: bool = True,
) -> tuple[bool, str]:
    """Apply the synthesized diff to a new branch in the project repo.

    For pick_best strategy, we bypass `git apply` entirely — we just check out
    the chosen agent's worktree branch's files into the new branch. That sidesteps
    every patch-format edge case (new files, binary, CRLF, missing trailing newline).

    For merge strategy, we write the synthesized diff to a temp file and try
    `git apply` with progressively-looser flags.
    """
    run = store.get_studio_run(run_id)
    if not run:
        return False, f"no run {run_id}"
    synth = run.synthesis or {}
    if not synth.get("final_diff"):
        return False, "no synthesized diff — call synthesize_studio_run first"
    repo = Path(run.repo_path)

    # Create the new branch from the run's base.
    code, out = await worktree._run(
        ["git", "checkout", "-B", branch_name, run.base_sha], repo,
    )
    if code != 0:
        return False, f"git checkout failed: {out[:300]}"

    chosen = synth.get("chosen")
    strategy = synth.get("strategy", "pick_best")

    if strategy == "pick_best" and chosen and chosen in run.results:
        # Copy files from the agent's worktree branch.
        agent_branch = f"friday/{run_id}/{chosen}"
        files = run.results[chosen].get("files_changed", [])
        if not files:
            return False, f"{chosen} has no changed files to copy"
        code, out = await worktree._run(
            ["git", "checkout", agent_branch, "--"] + files, repo,
        )
        if code != 0:
            return False, f"could not checkout {chosen}'s files: {out[:300]}"
        msg = f"applied {chosen}'s changes ({len(files)} files) to {branch_name}, uncommitted"
    else:
        # merge strategy — apply the synthesized diff via a temp file.
        diff = synth["final_diff"]
        if not diff.endswith("\n"):
            diff = diff + "\n"
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8",
        ) as f:
            f.write(diff)
            patch_path = f.name
        try:
            for flags in (["--index"], [], ["--3way"]):
                code, out = await worktree._run(
                    ["git", "apply", *flags, patch_path], repo,
                )
                if code == 0:
                    msg = f"applied synthesized diff to {branch_name} via `git apply {' '.join(flags)}`"
                    break
            else:
                return False, f"git apply failed under all strategies; last error: {out[:300]}"
        finally:
            Path(patch_path).unlink(missing_ok=True)

    if auto_cleanup:
        await cleanup_run_artifacts(run_id)
        msg += " (worktrees cleaned up)"
    return True, msg


async def cleanup_run_artifacts(run_id: str, delete_branches: bool = True) -> str:
    """Remove the run's worktrees and (optionally) the friday/* branches."""
    run = store.get_studio_run(run_id)
    if not run:
        return f"no run {run_id}"
    await worktree.cleanup_run(run_id)
    if delete_branches:
        repo = Path(run.repo_path)
        for agent in run.agents:
            await worktree._run(
                ["git", "branch", "-D", f"friday/{run_id}/{agent}"], repo,
            )
    return f"cleaned up worktrees for {run_id}"
