"""Load optional external MCP server configuration.

Friday will look for an MCP config in (first match wins):
  1. ./friday_mcp.json
  2. ~/.config/friday/mcp.json

The file shape is::

    {
      "servers": {
        "<server_name>": { "type": "stdio", "command": "...", "args": [...] },
        "<other>":       { "type": "http",  "url": "https://..." }
      },
      "allowed_tools": [
        "mcp__<server_name>__<tool>",
        ...
      ]
    }

Tools from external servers must be listed in ``allowed_tools`` to be invokable
by Friday. See the Claude Agent SDK docs for the exact server config shape.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_PATHS = [
    Path("friday_mcp.json"),
    Path.home() / ".config" / "friday" / "mcp.json",
]


def load_mcp_config() -> tuple[dict, list[str], Path | None]:
    """Return (mcp_servers_dict, extra_allowed_tools, source_path_or_None)."""
    for p in CONFIG_PATHS:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"warning: failed to parse {p}: {e}", file=sys.stderr)
            return {}, [], None
        servers = data.get("servers") or {}
        allowed = data.get("allowed_tools") or []
        if not isinstance(servers, dict) or not isinstance(allowed, list):
            print(f"warning: {p} has invalid shape; ignoring", file=sys.stderr)
            return {}, [], None
        return servers, [str(x) for x in allowed], p
    return {}, [], None
