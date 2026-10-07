"""Transactional project-local task state shared by the runner, CLI and MCP."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .roles import EFFORTS, default_roles, validate_model, validate_role
from .workflows import expand_workflow, validate_definition

ACTIVE = {"running", "waiting_approval", "interrupting"}
TERMINAL = {"completed", "failed", "interrupted", "lost", "cancelled"}
STATUSES = ACTIVE | TERMINAL | {"queued", "blocked"}
SCHEMA_VERSION = 2


def now() -> float:
    return time.time()


def identifier() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, project: str | Path):
        self.project = Path(project).expanduser().resolve()
        if not self.project.is_dir():
            raise ValueError(f"Project directory does not exist: {self.project}")
        self.directory = self.project / ".orchestrator"
        if self.directory.is_symlink():
            raise ValueError("The .orchestrator state directory must not be a symbolic link.")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / "state.sqlite3"
        if self.path.is_symlink():
            raise ValueError("The state database must not be a symbolic link.")
        with self.connect() as db:
            # Check an existing version before running DDL so a newer database
            # is rejected without being changed by an older runtime.
            existing_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            version = 1
            if "settings" in existing_tables:
                version_row = db.execute(
                    "SELECT data FROM settings WHERE key='schema_version'").fetchone()
                if version_row is not None:
                    version = json.loads(version_row[0])
                    if not isinstance(version, int) or isinstance(version, bool) or version > SCHEMA_VERSION:
                        raise ValueError(f"Unsupported state schema {version}; use a compatible runtime.")
                    if version < 1:
                        raise ValueError(f"Unsupported state schema {version}; use a compatible runtime.")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                    task_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS controls (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('schema_version', '1')")
            for role in default_roles():
                db.execute("INSERT OR IGNORE INTO roles VALUES (?, ?)", (role["name"], json.dumps(role)))
            if version == 1:
                self._migrate_v1_to_v2(db)
            elif version != SCHEMA_VERSION:
                raise ValueError(f"Unsupported state schema {version}; use a compatible runtime.")

    @staticmethod
    def _migrate_v1_to_v2(db):
        """Add workflow grouping to v1 projects without dropping existing state."""

        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("CREATE TABLE IF NOT EXISTS workflow_runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            for row in db.execute("SELECT id, data FROM tasks").fetchall():
                task = json.loads(row[1])
                task.setdefault("workflow_id", None)
                task.setdefault("workflow_step", None)
                db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), row[0]))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('dispatch', ?)",
                       (json.dumps({"paused": False}),))
            db.execute("UPDATE settings SET data=? WHERE key='schema_version'", (json.dumps(2),))
            db.commit()
        except Exception:
            db.rollback()
            raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, table, key, value):
        row = db.execute(f"SELECT data FROM {table} WHERE {key}=?", (value,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {table.rstrip('s')}: {value}")
        return json.loads(row[0])

    def _list(self, table):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def get_setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT data FROM settings WHERE key=?", (key,)).fetchone()
            return default if row is None else json.loads(row[0])

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def roles(self):
        return self._list("roles")

    def get_role(self, name):
        with self.connect() as db:
            return self._read(db, "roles", "name", name)

    def set_role(self, name, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM roles WHERE name=?", (name,)).fetchone()
            role = json.loads(row[0]) if row else {"name": name}
            role.update(changes)
            role["name"] = name
            validate_role(role)
            db.execute("INSERT OR REPLACE INTO roles VALUES (?, ?)", (name, json.dumps(role)))
            self._event(db, None, "role/updated", role)
            return role

    def add_task(self, prompt, role="worker", title=None, depends_on=None, parent_id=None, model=None, effort=None):
        dependencies = list(dict.fromkeys(depends_on or []))
        task_id = identifier()
        task = self._build_task(task_id, prompt, role, title, dependencies, parent_id, model, effort)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._read(db, "roles", "name", role)
            for dep in dependencies + ([parent_id] if parent_id else []):
                self._read(db, "tasks", "id", dep)
            db.execute("INSERT INTO tasks VALUES (?, ?)", (task["id"], json.dumps(task)))
            self._event(db, task["id"], "task/created", {"title": task["title"], "role": role})
        return task

    @staticmethod
    def _build_task(task_id, prompt, role, title=None, depends_on=None, parent_id=None,
                    model=None, effort=None, workflow_id=None, workflow_step=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Task prompt must not be empty.")
        if model is not None:
            validate_model(model)
        if effort is not None and effort not in EFFORTS:
            raise ValueError("Unsupported reasoning effort.")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("Task role must not be empty.")
        clean_prompt = prompt.strip()
        task_title = title.strip() if isinstance(title, str) and title.strip() else clean_prompt.splitlines()[0][:100]
        return dict(id=task_id, title=task_title, prompt=prompt,
                    role=role, status="queued", depends_on=list(depends_on or []), parent_id=parent_id,
                    model=model, effort=effort, current_model=None, current_effort=None,
                    thread_id=None, turn_id=None, result="", error=None, activity="Queued",
                    created_at=now(), updated_at=now(), run_id=None,
                    workflow_id=workflow_id, workflow_step=workflow_step)

    def submit_workflow(self, definition, goal, name=None):
        """Atomically create a named workflow run and every task in its DAG."""

        normalized = validate_definition(definition)
        expanded = expand_workflow(normalized, goal)
        run_name = normalized["name"] if name is None else name
        if not isinstance(run_name, str) or not run_name.strip() or len(run_name) > 128:
            raise ValueError("Workflow run name must contain 1 to 128 characters.")
        run_name = run_name.strip()

        workflow_id = identifier()
        task_ids = {step["key"]: identifier() for step in expanded["tasks"]}
        task_records = []
        for step in expanded["tasks"]:
            dependencies = [task_ids[key] for key in step["depends_on"]]
            task_records.append(self._build_task(
                task_ids[step["key"]], step["prompt"], step["role"], step["title"], dependencies,
                model=step.get("model"), effort=step.get("effort"),
                workflow_id=workflow_id, workflow_step=step["key"],
            ))
        created = now()
        run = {
            "id": workflow_id,
            "name": run_name,
            "definition_name": normalized["name"],
            "definition": normalized,
            "goal": goal.strip(),
            "task_ids": [task_ids[step["key"]] for step in expanded["tasks"]],
            "by_key": task_ids,
            "created_at": created,
            "updated_at": created,
        }

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Resolve roles again inside the write transaction, then write all
            # records/events together so a malformed graph can never leave a
            # partial runnable workflow behind.
            for step in expanded["tasks"]:
                self._read(db, "roles", "name", step["role"])
            db.execute("INSERT INTO workflow_runs VALUES (?, ?)", (workflow_id, json.dumps(run)))
            for task in task_records:
                db.execute("INSERT INTO tasks VALUES (?, ?)", (task["id"], json.dumps(task)))
                self._event(db, task["id"], "task/created", {
                    "title": task["title"], "role": task["role"],
                    "workflow_id": workflow_id, "workflow_step": task["workflow_step"],
                })
            self._event(db, None, "workflow/submitted", {
                "workflow_id": workflow_id, "name": run_name,
                "definition_name": normalized["name"], "task_count": len(task_records),
            })
        return self.get_workflow_run(workflow_id)

    def workflow_runs(self):
        with self.connect() as db:
            runs = [json.loads(row[0]) for row in db.execute("SELECT data FROM workflow_runs ORDER BY rowid")]
            tasks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks ORDER BY rowid")]
        tasks_by_workflow = {}
        for task in tasks:
            workflow_id = task.get("workflow_id")
            if workflow_id is not None:
                tasks_by_workflow.setdefault(workflow_id, []).append(task)
        return [self._workflow_detail(run, tasks_by_workflow.get(run["id"], [])) for run in runs]

    def get_workflow_run(self, workflow_id):
        with self.connect() as db:
            run = self._read(db, "workflow_runs", "id", workflow_id)
            tasks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks ORDER BY rowid")]
        tasks = [task for task in tasks if task.get("workflow_id") == workflow_id]
        return self._workflow_detail(run, tasks)

    @staticmethod
    def _workflow_detail(run, all_tasks):
        task_by_id = {task["id"]: task for task in all_tasks}
        tasks = [task_by_id[task_id] for task_id in run.get("task_ids", []) if task_id in task_by_id]
        statuses = {task["status"] for task in tasks}
        if tasks and statuses == {"completed"}:
            status = "completed"
        elif "failed" in statuses:
            status = "failed"
        elif "lost" in statuses:
            status = "lost"
        elif "interrupted" in statuses:
            status = "interrupted"
        elif "cancelled" in statuses:
            status = "cancelled"
        elif "waiting_approval" in statuses:
            status = "waiting_approval"
        elif statuses & {"running", "interrupting"}:
            status = "running"
        elif "queued" in statuses:
            status = "queued"
        elif "blocked" in statuses:
            status = "blocked"
        else:
            status = "queued"
        return {**run, "status": status, "tasks": tasks}

    def tasks(self):
        return self._list("tasks")

    def get_task(self, task_id):
        with self.connect() as db:
            return self._read(db, "tasks", "id", task_id)

    def update_task(self, task_id, **changes):
        return self.update_task_if(task_id, {}, **changes)

    def update_task_if(self, task_id, expected, **changes):
        """Apply an observation only while the task still matches its source state."""
        if "status" in changes and changes["status"] not in STATUSES:
            raise ValueError("Unknown task status.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, "tasks", "id", task_id)
            if any(task.get(key) != value for key, value in expected.items()):
                return None
            task.update(changes)
            task.update(id=task_id, updated_at=now())
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
            return task

    def claim_task(self, task_id, run_id, policy):
        """Claim only still-runnable work, atomically against CLI cancellation."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dispatch_row = db.execute("SELECT data FROM settings WHERE key='dispatch'").fetchone()
            if dispatch_row is not None:
                dispatch = json.loads(dispatch_row[0])
                if not isinstance(dispatch, dict) or type(dispatch.get("paused", False)) is not bool:
                    raise ValueError("Dispatch settings must be an object with a boolean paused field.")
                if dispatch.get("paused", False):
                    return None
            task = self._read(db, "tasks", "id", task_id)
            if task["status"] not in {"queued", "blocked"}:
                return None
            if any(self._read(db, "tasks", "id", dep)["status"] != "completed" for dep in task["depends_on"]):
                return None
            task.update(status="running", run_id=run_id, current_model=policy["model"],
                        current_effort=policy["effort"], current_sandbox=policy["sandbox"],
                        live_update_status=None, live_model=None, live_effort=None,
                        observed_model=None, updated_at=now(), activity="Starting Codex turn")
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
            self._event(db, task_id, "task/claimed", {"run_id": run_id, "model": policy["model"]})
            return task

    def task_policy(self, task):
        role = self.get_role(task["role"])
        return {**role, "model": task.get("model") or role["model"],
                "effort": task.get("effort") or role["effort"]}

    def set_task_model(self, task_id, model, effort=None, live=False):
        validate_model(model)
        if effort is not None and effort not in EFFORTS:
            raise ValueError("Unsupported reasoning effort.")
        changes = {"model": model}
        if effort is not None:
            changes["effort"] = effort
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, "tasks", "id", task_id)
            task.update(changes, updated_at=now())
            if live:
                if task["status"] not in ACTIVE or not task.get("turn_id"):
                    raise ValueError("Live changes require an active turn. Omit --live for the next turn.")
                policy = self._read(db, "roles", "name", task["role"])
                control = dict(id=identifier(), task_id=task_id, kind="model", status="pending",
                               model=model, effort=effort or task.get("effort") or policy["effort"],
                               run_id=task["run_id"], turn_id=task["turn_id"], created_at=now())
                db.execute("INSERT INTO controls VALUES (?, ?)", (control["id"], json.dumps(control)))
                task["live_update_status"] = "pending"
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
            self._event(db, task_id, "model/pending", {**changes, "live": live})
            return task

    def continue_task(self, task_id, prompt):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Continuation prompt must not be empty.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, "tasks", "id", task_id)
            if task["status"] not in TERMINAL:
                raise ValueError("Only a terminal task can continue. Interrupt active work first.")
            previous = {"turn_id": task["turn_id"], "status": task["status"], "result": task["result"], "error": task["error"]}
            self._event(db, task_id, "task/continued", previous)
            task.update(prompt=prompt, status="queued", result="", error=None, turn_id=None,
                        activity="Continuation queued", updated_at=now())
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
            return task

    def interrupt_task(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, "tasks", "id", task_id)
            if task["status"] in {"queued", "blocked"}:
                task.update(status="cancelled", activity="Cancelled before dispatch", updated_at=now())
                db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
            elif task["status"] in ACTIVE:
                control = dict(id=identifier(), task_id=task_id, kind="interrupt", status="pending",
                               run_id=task["run_id"], turn_id=task["turn_id"], created_at=now())
                db.execute("INSERT INTO controls VALUES (?, ?)", (control["id"], json.dumps(control)))
            else:
                raise ValueError("Task is already terminal.")
            self._event(db, task_id, "task/interrupt_requested", {})
            return task

    @staticmethod
    def _event(db, task_id, kind, data):
        db.execute("INSERT INTO events(timestamp,task_id,kind,data) VALUES (?,?,?,?)",
                   (now(), task_id, kind, json.dumps(data)))

    def event(self, task_id, kind, data):
        with self.connect() as db:
            self._event(db, task_id, kind, data)

    def events(self, task_id=None, after=None, limit=100):
        if after is not None and after < 0:
            raise ValueError("Event cursor must be nonnegative.")
        with self.connect() as db:
            query = "SELECT id,timestamp,task_id,kind,data FROM events WHERE id>?"
            params = [after if after is not None else 0]
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)
            # Cursor reads must not skip earlier events during a burst.
            query += " ORDER BY id ASC LIMIT ?" if after is not None else " ORDER BY id DESC LIMIT ?"
            params.append(max(1, min(int(limit), 1000)))
            rows = db.execute(query, params).fetchall()
            ordered = rows if after is not None else reversed(rows)
            return [dict(id=r[0], timestamp=r[1], task_id=r[2], kind=r[3], data=json.loads(r[4])) for r in ordered]

    def upsert_agent(self, thread_id, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM agents WHERE id=?", (thread_id,)).fetchone()
            agent = json.loads(row[0]) if row else {"id": thread_id}
            agent.update(fields)
            agent.update(id=thread_id, updated_at=now())
            db.execute("INSERT OR REPLACE INTO agents VALUES (?, ?)", (thread_id, json.dumps(agent)))
            return agent

    def agents(self):
        return self._list("agents")

    def add_request(self, rpc_id, method, params, run_id, task_id=None):
        request = dict(id=identifier(), rpc_id=rpc_id, method=method, params=params, run_id=run_id,
                       task_id=task_id, status="pending", response=None, created_at=now())
        with self.connect() as db:
            db.execute("INSERT INTO requests VALUES (?, ?)", (request["id"], json.dumps(request)))
        return request

    def requests(self, status=None):
        return [r for r in self._list("requests") if status is None or r["status"] == status]

    def update_request(self, request_id, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            request = self._read(db, "requests", "id", request_id)
            request.update(fields)
            db.execute("UPDATE requests SET data=? WHERE id=?", (json.dumps(request), request_id))
            return request

    def respond(self, request_id, decision=None, answers=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            request = self._read(db, "requests", "id", request_id)
            if request["status"] != "pending":
                raise ValueError("This request is no longer pending.")
            method = request["method"]
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                if decision not in ("accept", "decline", "cancel") or answers is not None:
                    raise ValueError("Choose accept, decline, or cancel for this approval.")
                response = {"decision": decision}
            elif method == "item/tool/requestUserInput":
                if not isinstance(answers, dict) or decision is not None:
                    raise ValueError("Provide answers as a JSON object keyed by question ID.")
                questions = request["params"].get("questions", [])
                if set(answers) != {q["id"] for q in questions}:
                    raise ValueError("Answers must include each question ID exactly once.")
                if any(not isinstance(v, dict) or not isinstance(v.get("answers"), list)
                       or not v["answers"] or not all(isinstance(a, str) for a in v["answers"]) for v in answers.values()):
                    raise ValueError('Each answer must have the shape {"answers": ["text"]}.')
                response = {"answers": answers}
            else:
                raise ValueError("Unsupported request; the runner will reject it explicitly.")
            request.update(status="answered", response=response)
            db.execute("UPDATE requests SET data=? WHERE id=?", (json.dumps(request), request_id))
            self._event(db, request["task_id"], "request/answered", {"request_id": request_id})
            return request

    def controls(self):
        return self._list("controls")

    def update_control(self, control_id, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            control = self._read(db, "controls", "id", control_id)
            control.update(fields)
            db.execute("UPDATE controls SET data=? WHERE id=?", (json.dumps(control), control_id))
            return control

    def snapshot(self):
        tasks = []
        for task in self.tasks():
            policy = self.task_policy(task)
            task["next_model"] = policy["model"]
            task["next_effort"] = policy["effort"]
            task["model_change_pending"] = task["status"] in ACTIVE and (
                task["current_model"] != policy["model"] or task["current_effort"] != policy["effort"])
            tasks.append(task)
        runner = self.get_setting("runner")
        if runner:
            runner["heartbeat_age"] = max(0, now() - runner.get("heartbeat", 0))
            runner["observation"] = "recent heartbeat" if runner["heartbeat_age"] < 10 else "stale heartbeat; liveness unknown"
        workflow_summaries = [
            {key: run[key] for key in ("id", "name", "definition_name", "status", "task_ids", "created_at")}
            for run in self.workflow_runs()
        ]
        return dict(project=str(self.project), runner=runner, tasks=tasks, agents=self.agents(),
                    requests=self.requests("pending"), roles=self.roles(),
                    dispatch=self.get_setting("dispatch", {"paused": False}), workflows=workflow_summaries)


class RunnerLock:
    """OS-owned exclusive lock; a stale file is never treated as a live runner."""

    def __init__(self, directory):
        self.path = Path(directory) / "runner.lock"
        self.file = None

    def __enter__(self):
        if self.path.is_symlink():
            raise ValueError("Runner lock must not be a symbolic link.")
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ValueError("A runner already owns this project.") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
