"""Small stdio MCP interface to the same task store as the CLI.

No inference runs inside a tool call. A separately started foreground runner
dispatches queued tasks. Approvals are deliberately handled by the human CLI.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .store import Store


def string(description):
    return {"type": "string", "description": description}


PROJECT = string("Absolute path to the existing target project directory")
DEFINITIONS = [
    ("orchestrator_status", "Inspect managed tasks, native child activity, model policy and pending requests.", True,
     {"project": PROJECT}, ["project"]),
    ("orchestrator_roles", "Read current role instructions and model defaults before delegating.", True,
     {"project": PROJECT}, ["project"]),
    ("orchestrator_task", "Read a task's result, thread ID, status and recent events.", True,
     {"project": PROJECT, "task_id": string("Task ID")}, ["project", "task_id"]),
    ("orchestrator_add_task", "Queue a bounded task for the project's running scheduler; returns immediately.", False,
     {"project": PROJECT, "prompt": string("Concrete objective, scope and acceptance criteria"),
      "role": string("Role name from orchestrator_roles"), "title": string("Short task title"),
      "depends_on": {"type": "array", "items": {"type": "string"}},
      "parent_id": string("Optional managed parent task ID"), "model": string("Optional exact model override"),
      "effort": string("Optional reasoning effort")}, ["project", "prompt", "role"]),
    ("orchestrator_set_model", "Change a role's future-turn policy or a task's model. Live task publication is experimental.", False,
     {"project": PROJECT, "model": string("Exact model ID"), "effort": string("Reasoning effort"),
      "role": string("Role name, mutually exclusive with task_id"), "task_id": string("Managed task ID"),
      "live": {"type": "boolean", "description": "Request publication during an active managed turn; already captured steps and child sessions are unchanged"}},
     ["project", "model"]),
    ("orchestrator_continue_task", "Queue an explicit continuation of a terminal task using its saved Codex thread.", False,
     {"project": PROJECT, "task_id": string("Task ID"), "prompt": string("Continuation instructions")}, ["project", "task_id", "prompt"]),
    ("orchestrator_interrupt_task", "Request interruption of an active task, or cancel work not yet dispatched.", False,
     {"project": PROJECT, "task_id": string("Task ID")}, ["project", "task_id"]),
]

TOOLS = [{"name": name, "description": description,
          "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
          "annotations": {"readOnlyHint": readonly, "destructiveHint": not readonly,
                          "openWorldHint": False}}
         for name, description, readonly, properties, required in DEFINITIONS]


def validate_arguments(name, args):
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise ValueError(f"Unknown tool: {name}")
    schema = tool["inputSchema"]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
        raise ValueError("Missing or unexpected tool arguments.")
    for key, value in args.items():
        kind = schema["properties"][key]["type"]
        if kind == "string" and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a nonempty string.")
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean.")
        if kind == "array" and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise ValueError(f"{key} must be a list of strings.")
    if not Path(args["project"]).is_absolute():
        raise ValueError("project must be an absolute path.")


def call_tool(name, args):
    validate_arguments(name, args)
    store = Store(args["project"])
    if name == "orchestrator_status":
        return store.snapshot()
    if name == "orchestrator_roles":
        return {"roles": store.roles()}
    if name == "orchestrator_task":
        return {"task": store.get_task(args["task_id"]), "events": store.events(args["task_id"])}
    if name == "orchestrator_add_task":
        return {"task": store.add_task(**{k: v for k, v in args.items() if k != "project"}),
                "dispatch": "Queued; codex-orchestrator serve must be running in this project."}
    if name == "orchestrator_set_model":
        if bool(args.get("role")) == bool(args.get("task_id")):
            raise ValueError("Specify exactly one of role or task_id.")
        if args.get("role"):
            if args.get("live"):
                raise ValueError("Live publication requires a task_id; role policy affects future turns.")
            changes = {k: args[k] for k in ("model", "effort") if k in args}
            return {"role": store.set_role(args["role"], **changes), "effect": "future turns"}
        task = store.set_task_model(args["task_id"], args["model"], args.get("effort"), args.get("live", False))
        return {"task": task, "effect": "Live request queued; inspect events for actual publication outcome." if args.get("live") else "next turn"}
    if name == "orchestrator_continue_task":
        return {"task": store.continue_task(args["task_id"], args["prompt"])}
    if name == "orchestrator_interrupt_task":
        return {"task": store.interrupt_task(args["task_id"])}
    raise ValueError(f"Unknown tool: {name}")


class Session:
    def __init__(self):
        self.initialized = False

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            return None
        response = {"jsonrpc": "2.0", "id": request_id}
        params = message.get("params", {})
        if not isinstance(params, dict):
            return {**response, "error": {"code": -32602, "message": "params must be an object"}}
        if method == "initialize":
            offered = params.get("protocolVersion")
            protocol = offered if offered in ("2024-11-05", "2025-03-26", "2025-06-18") else "2025-06-18"
            self.initialized = True
            result = {"protocolVersion": protocol, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "codex-orchestrator", "version": __version__},
                      "instructions": "Use an explicit project path. Tasks require a separately started runner. Model changes never control your current host session. Approvals require the user-facing CLI."}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return {**response, "error": {"code": -32002, "message": "Initialize first"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                value = call_tool(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                          "structuredContent": value, "isError": False}
            except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return {**response, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        return {**response, "result": result}


def serve(stdin=None, stdout=None):
    if stdin is None and hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if stdout is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    session = Session()
    while True:
        line = stdin.readline(1024 * 1024 + 1)
        if not line:
            return 0
        if len(line) > 1024 * 1024:
            print("MCP input exceeds 1 MiB.", file=sys.stderr)
            return 1
        try:
            response = session.handle(json.loads(line))
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
