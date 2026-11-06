"""A tiny MCP server (stdio, JSON-RPC) with one tool: run one of the --check commands.

Harnesses whose shell rules can't be limited to exact commands (Copilot CLI) get
this instead of a shell, so the agentic check fixer can run the checks and nothing else.

    python -m reviewloop.check_server CONFIG.json

CONFIG.json is {"repo": "/path", "commands": ["pytest -q", ...]}. The agent picks a
command by its number; it can't pass arguments, so it can't change what runs.
"""

import json
import sys
from pathlib import Path

from . import context

TOOL = "run_check"
PROTOCOL_VERSION = "2025-06-18"


def tool_spec(commands: list[str]) -> dict:
    listing = "\n".join(f"{i}: {c}" for i, c in enumerate(commands, 1))
    return {
        "name": TOOL,
        "description": "Run one of the project's checks by number and return its exit code and output. "
                       "It runs exactly the command shown, in the repository root:\n" + listing,
        "inputSchema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "minimum": 1, "maximum": len(commands)}},
            "required": ["number"],
            "additionalProperties": False,
        },
    }


def handle(request: dict, repo: str, commands: list[str]) -> dict | None:
    """The response to one JSON-RPC request, or None for a notification."""
    method, rid = request.get("method"), request.get("id")
    if rid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": request.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "reviewloop", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [tool_spec(commands)]}
    elif method == "tools/call":
        params = request.get("params") or {}
        number = (params.get("arguments") or {}).get("number")
        if params.get("name") != TOOL or not isinstance(number, int) or not 1 <= number <= len(commands):
            result = {"isError": True, "content": [{"type": "text", "text": f"Pick a check from 1 to {len(commands)}."}]}
        else:
            check, = context.run_checks(Path(repo), [commands[number - 1]])
            text = f"$ {check.command}\nexit code {check.returncode}\n{check.output}"
            result = {"isError": False, "content": [{"type": "text", "text": text}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> None:
    with open(sys.argv[1]) as f:
        config = json.load(f)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request, config["repo"], config["commands"]) if isinstance(request, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
