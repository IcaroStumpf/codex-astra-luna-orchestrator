"""Terminal entry point. All commands share the same project-local store."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import __version__
from .roles import EFFORTS, validate_role
from .store import Store


def clean(value):
    """Do not interpret terminal control sequences from tool output or prompts."""
    return "".join(c if c.isprintable() else " " for c in str(value if value is not None else "-"))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def table(headers, rows):
    rows = [[clean(c) for c in row] for row in rows]
    if not rows:
        print("(none)")
        return
    width = max(60, shutil.get_terminal_size((120, 24)).columns)
    limits = [max(len(headers[i]), min(36, max(len(r[i]) for r in rows))) for i in range(len(headers))]
    minimum = [max(len(header), 8) for header in headers]
    while sum(limits) + 2 * (len(limits) - 1) > width:
        index = max(range(len(limits)), key=lambda i: limits[i] - minimum[i])
        if limits[index] <= minimum[index]:
            break
        limits[index] -= 1
    def line(cells):
        return "  ".join((c if len(c) <= n else c[:n-1] + "…").ljust(n) for c, n in zip(cells, limits))
    print(line(headers))
    print(line(["-" * n for n in limits]))
    for row in rows:
        print(line(row))


def render_status(snapshot):
    print(f"Codex Orchestrator · {clean(snapshot['project'])}")
    runner = snapshot["runner"]
    if runner:
        print(f"Runner: {clean(runner.get('status'))} | {clean(runner.get('observation'))}")
        if runner.get("error"):
            print(f"  {clean(runner['error'])}")
    else:
        print("Runner: not started. Run codex-orchestrator serve in another terminal.")
    table(["TASK", "ROLE", "STATE", "TURN MODEL", "OBJECTIVE"], [
        [t["id"], t["role"], t["status"], t["next_model"] if t["status"] in {"queued", "blocked"} else t["current_model"], t["title"]]
        for t in snapshot["tasks"]])
    for task in snapshot["tasks"]:
        if task.get("activity") not in (None, "", "Queued", "Running", "Completed", "Interrupted", "Failed"):
            print(f"  {task['id']}: {clean(task['activity'])[:300]}")
        if task["model_change_pending"]:
            print(f"  {task['id']}: next turn {clean(task['next_model'])}/{clean(task['next_effort'])}")
        if task.get("live_update_status"):
            print(f"  {task['id']}: live publication {clean(task['live_update_status'])}"
                  f" {clean(task.get('live_model'))} (already captured steps unchanged)")
        if task.get("error"):
            print(f"  {task['id']}: {clean(task['error'])[:300]}")
        usage = task.get("usage", {}).get("total", {})
        if usage:
            total = usage.get("totalTokens", usage.get("total_tokens", "-"))
            print(f"  {task['id']}: thread tokens {clean(total)}")
        if task.get("observed_model"):
            print(f"  {task['id']}: observed reroute to {clean(task['observed_model'])}")
    children = [a for a in snapshot["agents"] if a.get("parent_thread_id")]
    if children:
        print("\nNative subagents (observed from Codex events):")
        table(["THREAD", "PARENT", "ROLE", "STATE", "ACTIVITY"], [
            [a["id"], a.get("parent_thread_id"), a.get("role"), a.get("status"), a.get("activity", "")]
            for a in children])
    if snapshot["requests"]:
        print(f"\n{len(snapshot['requests'])} pending request(s). Use approvals, then respond ID.")


def parser():
    p = argparse.ArgumentParser(prog="codex-orchestrator", description="Local Codex task teams, live visibility and model controls.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--project", default=os.getcwd(), help="Project directory (default: current directory)")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize local state, optionally importing a Codex profile")
    init.add_argument("--config", type=Path, help="Existing Codex config.toml; imports model/role choices only")
    commands.add_parser("roles", help="Show the configurable role catalog")
    role = commands.add_parser("role", help="Create or replace a custom role").add_subparsers(dest="action", required=True)
    add_role = role.add_parser("add")
    add_role.add_argument("name")
    add_role.add_argument("--model", required=True)
    add_role.add_argument("--effort", choices=EFFORTS, default="medium")
    add_role.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    add_role.add_argument("--instructions", required=True)
    add_role.add_argument("--description", default="Custom role")
    commands.add_parser("models", help="Query the installed Codex model catalog (no inference)")
    model = commands.add_parser("model").add_subparsers(dest="action", required=True)
    model_set = model.add_parser("set", help="Change a role's defaults for future turns")
    model_set.add_argument("role")
    model_set.add_argument("model")
    model_set.add_argument("--effort", choices=EFFORTS)
    task = commands.add_parser("task").add_subparsers(dest="action", required=True)
    add = task.add_parser("add", help="Queue a bounded task")
    add.add_argument("prompt")
    add.add_argument("--title")
    add.add_argument("--role", default="worker")
    add.add_argument("--after", action="append", default=[])
    add.add_argument("--parent")
    add.add_argument("--model")
    add.add_argument("--effort", choices=EFFORTS)
    for action in ("show", "interrupt", "continue", "model"):
        item = task.add_parser(action)
        item.add_argument("id")
        if action == "continue":
            item.add_argument("prompt")
        if action == "model":
            item.add_argument("model")
            item.add_argument("--effort", choices=EFFORTS)
            item.add_argument("--live", action="store_true", help="Also request experimental active-turn publication")
    commands.add_parser("status", help="Show the task board").add_argument("--json", action="store_true")
    watch = commands.add_parser("watch", help="Refresh the task board until Ctrl+C")
    watch.add_argument("--interval", type=float, default=1)
    watch.add_argument("--json", action="store_true", help="Emit one JSON snapshot per line")
    watch.add_argument("--count", type=int, help="Stop after this many snapshots")
    events = commands.add_parser("events", help="Read recent persisted lifecycle events")
    events.add_argument("--task")
    events.add_argument("--after", type=int, help="Event cursor; 0 starts at the first event (omitted: recent window)")
    events.add_argument("--limit", type=int, default=100)
    serve = commands.add_parser("serve", help="Run the foreground scheduler")
    serve.add_argument("--once", action="store_true", help="Drain runnable work and exit")
    serve.add_argument("--workers", type=int, default=2)
    serve.add_argument("--live-models", action="store_true", help="Enable Codex's experimental step_model_switching for this runner only")
    commands.add_parser("approvals", help="Show pending Codex approvals and questions")
    respond = commands.add_parser("respond", help="Answer a pending request")
    respond.add_argument("id")
    answer = respond.add_mutually_exclusive_group(required=True)
    answer.add_argument("--decision", choices=("accept", "decline", "cancel"))
    answer.add_argument("--answers", help='JSON: {"question-id": {"answers": ["text"]}}')
    commands.add_parser("doctor", help="Check Python, state, Codex and protocol connectivity without inference")
    commands.add_parser("mcp", help="Serve plugin tools over stdio")
    return p


def import_profile(store, path):
    path = path.expanduser().resolve()
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    root = store.get_role("orchestrator")
    defaults = config.get("agents", {})
    if not isinstance(defaults, dict):
        raise ValueError("The imported config's agents section must be a table.")
    updates = []
    # Imports only role/model policy. It never changes permissions or user configuration.
    for role in store.roles():
        name = role["name"]
        if name == "orchestrator":
            model = config.get("model", root["model"])
            effort = config.get("model_reasoning_effort", root["effort"])
        else:
            configured = defaults.get(name, {})
            explicit_path = configured.get("config_file") if isinstance(configured, dict) else None
            if explicit_path is not None and not isinstance(explicit_path, str):
                raise ValueError(f"agents.{name}.config_file must be a path string.")
            agent_path = (path.parent / Path(explicit_path).expanduser()).resolve() if explicit_path else path.parent / "agents" / f"{name}.toml"
            if explicit_path and not agent_path.is_file():
                raise ValueError(f"Role configuration does not exist: {agent_path}")
            agent = tomllib.loads(agent_path.read_text(encoding="utf-8")) if agent_path.is_file() else {}
            model = agent.get("model", defaults.get("default_subagent_model", role["model"]))
            effort = agent.get("model_reasoning_effort", defaults.get("default_subagent_reasoning_effort", role["effort"]))
        validate_role({**role, "model": model, "effort": effort})
        updates.append((name, model, effort))
    for name, model, effort in updates:
        store.set_role(name, model=model, effort=effort)


def list_models(store):
    from .rpc import AppServer
    server = AppServer(cwd=store.project)
    try:
        server.start()
        models, cursor, seen = [], None, set()
        while True:
            result = server.request("model/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
            models.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError("Codex returned a repeating model catalog cursor.")
            seen.add(cursor)
        store.set_setting("models", models)
        store.set_setting("models_updated_at", time.time())
        return models
    finally:
        server.close()


def execute(args):
    if args.command == "mcp":
        from .mcp import serve
        return serve()
    store = Store(args.project)
    if args.command == "init":
        if args.config:
            import_profile(store, args.config)
        print(f"Initialized {store.directory}")
        print("Next: task add PROMPT --role explorer; serve; status")
    elif args.command == "roles":
        table(["ROLE", "MODEL", "EFFORT", "SANDBOX", "PURPOSE"], [
            [r["name"], r["model"], r["effort"], r["sandbox"], r["description"]] for r in store.roles()])
    elif args.command == "role":
        emit(store.set_role(args.name, model=args.model, effort=args.effort, sandbox=args.sandbox,
                            instructions=args.instructions, description=args.description))
    elif args.command == "model":
        changes = {"model": args.model}
        if args.effort:
            changes["effort"] = args.effort
        emit(store.set_role(args.role, **changes))
        print("Applies to future turns. Running tasks retain their captured settings.")
    elif args.command == "task":
        if args.action == "add":
            task = store.add_task(args.prompt, args.role, args.title, args.after, args.parent, args.model, args.effort)
            print(task["id"])
        elif args.action == "show":
            emit(store.get_task(args.id))
        elif args.action == "continue":
            emit(store.continue_task(args.id, args.prompt))
        elif args.action == "interrupt":
            emit(store.interrupt_task(args.id))
        elif args.action == "model":
            emit(store.set_task_model(args.id, args.model, args.effort, live=args.live))
            print("Live request queued; inspect status/events for publication outcome." if args.live else "Model saved for the next turn.")
    elif args.command == "status":
        (emit if args.json else render_status)(store.snapshot())
    elif args.command == "watch":
        if not math.isfinite(args.interval) or args.interval < 0.1 or (args.count is not None and args.count < 1):
            raise ValueError("Interval must be >=0.1 and count must be positive.")
        count = 0
        while args.count is None or count < args.count:
            if args.json:
                print(json.dumps(store.snapshot(), ensure_ascii=False), flush=True)
            else:
                if sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                render_status(store.snapshot())
            count += 1
            if args.count is None or count < args.count:
                time.sleep(args.interval)
    elif args.command == "events":
        emit(store.events(args.task, args.after, args.limit))
    elif args.command == "approvals":
        emit(store.requests("pending"))
    elif args.command == "respond":
        emit(store.respond(args.id, args.decision, json.loads(args.answers) if args.answers else None))
    elif args.command == "models":
        emit(list_models(store))
    elif args.command == "serve":
        if not 1 <= args.workers <= 32:
            raise ValueError("Workers must be between 1 and 32.")
        from .engine import Engine
        engine = Engine(store, max_workers=args.workers, enable_live_models=args.live_models)
        result = engine.run(once=args.once)
        if engine.last_error:
            print(f"Runner error: {clean(engine.last_error)}", file=sys.stderr)
        return result
    elif args.command == "doctor":
        executable = shutil.which("codex")
        checks = {"python": sys.version.split()[0], "project": str(store.project),
                  "state": str(store.path), "codex": executable, "version": __version__}
        if not executable:
            emit(checks)
            raise ValueError("Codex CLI not found. Install Codex and run codex login first.")
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15)
        checks["codex_version"] = clean(result.stdout).strip()
        checks["catalog_count"] = len(list_models(store))
        checks["protocol"] = "initialize + model/list succeeded; no inference performed"
        emit(checks)
    return 0


def main(argv=None):
    # The console and stdio MCP protocol use UTF-8 on every supported platform.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        return execute(parser().parse_args(argv))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"Error: {clean(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
