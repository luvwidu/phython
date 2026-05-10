"""Specialist subagent definitions registered with the orchestrator."""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

CODER = AgentDefinition(
    description="Hands-on coding agent for tasks scoped to a single repository.",
    prompt=(
        "You are a focused coding agent working inside the current repository. "
        "Read the relevant code first, plan, then make minimal precise edits. "
        "Run tests when available. Do not commit, push, or open PRs unless the "
        "instruction explicitly asks you to."
    ),
    tools=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
    model="sonnet",
)

OPS = AgentDefinition(
    description="Operations assistant for productivity tools (email, calendar, drive, etc).",
    prompt=(
        "You handle productivity tasks via the MCP tools available in the session "
        "(gmail / calendar / drive / gamma). Be concise. ALWAYS confirm with the "
        "user before sending an email, creating an event, or sharing a document."
    ),
    tools=["Read", "Bash"],
    model="sonnet",
)

RESEARCHER = AgentDefinition(
    description="Researches an idea or feature and returns a structured plan.",
    prompt=(
        "You research ideas and produce a tight plan: goal, key risks, the "
        "minimum viable first step, and a 3-5 step roadmap. No code edits."
    ),
    tools=["Read", "Bash", "Grep", "Glob"],
    model="sonnet",
)

REGISTRY: dict[str, AgentDefinition] = {
    "coder": CODER,
    "ops": OPS,
    "researcher": RESEARCHER,
}
