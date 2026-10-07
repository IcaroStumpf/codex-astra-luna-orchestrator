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
FILTERS = {"task_id": string("Managed task ID"), "workflow_id": string("Workflow execution ID"),
           "role": string("Managed role name"), "states": {"type": "array", "items": {"type": "string"}},
           "active_only": {"type": "boolean"}}
DEFINITIONS = [
    ("orchestrator_status", "Inspect managed tasks, native child activity, model policy and pending requests.", True,
     {"project": PROJECT, **FILTERS}, ["project"]),
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
    ("orchestrator_steer_task", "Queue additional input for the current active turn. It never starts a continuation or changes model policy.", False,
     {"project": PROJECT, "task_id": string("Task ID"), "prompt": string("Additional requirement or correction")},
     ["project", "task_id", "prompt"]),
    ("orchestrator_dispatch", "Pause or resume admission of new tasks. Active turns and approval handling continue.", False,
     {"project": PROJECT, "paused": {"type": "boolean"}}, ["project", "paused"]),
    ("orchestrator_workflow_templates", "Inspect built-in feature, bugfix and review task graphs before submitting.", True,
     {"project": PROJECT}, ["project"]),
    ("orchestrator_submit_workflow", "Atomically queue a reusable dependency graph; the separate project runner executes it.", False,
     {"project": PROJECT, "source": string("Built-in template name or absolute path to a JSON workflow"),
      "goal": string("Concrete objective and constraints"), "name": string("Optional human-readable execution label")},
     ["project", "source", "goal"]),
    ("orchestrator_workflows", "List durable workflow executions and their current aggregate status.", True,
     {"project": PROJECT}, ["project"]),
    ("orchestrator_workflow", "Read a workflow execution and all of its member tasks and results.", True,
     {"project": PROJECT, "workflow_id": string("Workflow execution ID")}, ["project", "workflow_id"]),
    ("orchestrator_usage", "Inspect reported thread token usage, with managed and native totals kept separate and missing coverage labeled.", True,
     {"project": PROJECT, **FILTERS}, ["project"]),
    ("orchestrator_report", "Build a local evidence report with task results, native agents, controls and recent history. No file is written.", True,
     {"project": PROJECT, "task_id": string("Optional managed task ID"), "workflow_id": string("Optional workflow ID")},
     ["project"]),
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
    if name in ("orchestrator_status", "orchestrator_usage"):
        from .views import filter_snapshot, usage_report
        from .store import STATUSES
        if args.get("task_id"):
            store.get_task(args["task_id"])
        if args.get("workflow_id"):
            store.get_workflow_run(args["workflow_id"])
        if args.get("role"):
            store.get_role(args["role"])
        if set(args.get("states", [])) - STATUSES:
            raise ValueError("Unknown task status in states.")
        snapshot = filter_snapshot(store.snapshot(), **{k: v for k, v in args.items() if k != "project"})
        return usage_report(snapshot) if name == "orchestrator_usage" else snapshot
    if name == "orchestrator_roles":
        return {"roles": store.roles()}
    if name == "orchestrator_task":
        return {"task": store.get_task(args["task_id"]), "events": store.events(args["task_id"]),
                "controls": [c for c in store.controls() if c.get("task_id") == args["task_id"]][-100:]}
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
    if name == "orchestrator_steer_task":
        from .controls import steer_task
        return {"control": steer_task(store, args["task_id"], args["prompt"]),
                "effect": "Queued for the current turn only; inspect task controls/events for delivery outcome."}
    if name == "orchestrator_dispatch":
        from .controls import set_dispatch
        return {"dispatch": set_dispatch(store, args["paused"])}
    if name == "orchestrator_workflow_templates":
        from .workflows import templates
        return {"templates": templates()}
    if name == "orchestrator_submit_workflow":
        from .workflows import templates, load_definition
        if args["source"] not in {t["name"] for t in templates()} and not Path(args["source"]).is_absolute():
            raise ValueError("A workflow file source must be an absolute path.")
        return {"workflow": store.submit_workflow(load_definition(args["source"]), args["goal"], args.get("name")),
                "dispatch": "Queued; codex-orchestrator serve must be running in this project."}
    if name == "orchestrator_workflows":
        return {"workflows": store.workflow_runs()}
    if name == "orchestrator_workflow":
        return {"workflow": store.get_workflow_run(args["workflow_id"])}
    if name == "orchestrator_report":
        from .views import build_report
        if args.get("task_id") and args.get("workflow_id"):
            raise ValueError("Choose either task_id or workflow_id for a report.")
        return build_report(store, task_id=args.get("task_id"), workflow_id=args.get("workflow_id"))
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
