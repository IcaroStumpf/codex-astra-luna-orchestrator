"""Run persisted orchestration tasks through the Codex app-server."""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .rpc import AppServer, RpcError
from .store import ACTIVE, TERMINAL, RunnerLock


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
}
_TERMINAL_TURNS = {"completed", "failed", "interrupted"}
_FAILED_TASKS = {"failed", "blocked", "lost", "interrupted", "cancelled"}
_MAX_RESULT_BYTES = 128 * 1024
_MAX_ACTIVITY_CHARS = 320
_MAX_PLAN_STEPS = 12
_HEARTBEAT_INTERVAL = 2.0
_SHUTDOWN_GRACE = 1.5
_METHOD_NOT_FOUND = -32601

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AIza[0-9A-Za-z_-]{20,})\b"), "[redacted]"),
    (re.compile(r"(?i)(--?(?:api[-_]?key|token|password|passwd|secret)(?:=|\s+))([^\s'\"]+)"), r"\1[redacted]"),
)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _bounded_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = "\n…[truncated]"
    return encoded[: max(0, limit - len(suffix.encode("utf-8")))].decode("utf-8", "ignore") + suffix


def _safe_display(value: str, limit: int = _MAX_ACTIVITY_CHARS) -> str:
    value = value.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def _error_message(error: BaseException, limit: int = 1000) -> str:
    # RPC error payloads can contain arbitrary server data. Persist only the
    # bounded human-readable message, never the raw payload or stderr stream.
    return _safe_display(str(error), limit)


class Engine:
    """Schedule tasks and translate app-server events into project state.

    The server is injected for tests and can be a synchronous ``AppServer``.
    Each invocation owns the project's runner lock and one app-server process.
    """

    def __init__(
        self,
        store,
        server=None,
        max_workers: int = 2,
        poll_interval: float = 0.1,
        shutdown_grace: float = _SHUTDOWN_GRACE,
        enable_live_models: bool = False,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if shutdown_grace < 0:
            raise ValueError("shutdown_grace must be non-negative")
        self.store = store
        self.server = server
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self.shutdown_grace = shutdown_grace
        self.enable_live_models = enable_live_models
        self.run_id: str | None = None
        self.last_error: str | None = None
        self._thread_to_task: dict[str, str] = {}
        self._item_phases: dict[tuple[str, str, str], str | None] = {}
        self._commentary: dict[tuple[str, str, str], str] = {}
        self._last_activity_write: dict[str, float] = {}
        self._fallback_messages: dict[tuple[str, str], str] = {}
        self._selected_ids: set[str] = set()
        self._started_server = False
        self._last_heartbeat = 0.0
        self._stopping = False
        self._live_model_supported: bool | None = None
        self._orphaned_agent_deadlines: dict[str, float] = {}

    def run(self, once: bool = False, stop_event: threading.Event | None = None) -> int:
        """Own the runner lock, then service tasks until idle or stopped.

        ``once=True`` drains all currently queued work and its dependencies,
        then returns nonzero when any selected task failed, was blocked, was
        interrupted, was cancelled, or has an uncertain/lost outcome.
        """

        self.run_id = uuid.uuid4().hex
        self.last_error = None
        lock = RunnerLock(self.store.directory)
        try:
            lock.__enter__()
        except (OSError, ValueError) as exc:
            self.last_error = _error_message(exc)
            return 1

        try:
            self._selected_ids = {
                task["id"]
                for task in self.store.tasks()
                if task.get("status") in {"queued", "blocked"} or task.get("status") in ACTIVE
            }
            self._recover_active_tasks()
            self._expire_old_requests()
            self._set_runner("starting")

            try:
                if self.server is None:
                    command = None
                    if self.enable_live_models:
                        command = [shutil.which("codex") or "codex", "--enable", "step_model_switching", "app-server"]
                    self.server = AppServer(command=command, cwd=self.store.project)
                self._started_server = True
                self.server.start()
                self._refresh_models()
                self._set_runner("running")

                while True:
                    if stop_event is not None and stop_event.is_set():
                        return self._stop_active_tasks()

                    self._heartbeat()
                    self._process_controls()
                    self._deliver_answered_requests()
                    self._drain_events()
                    self._check_native_lifecycle()
                    self._schedule_ready_tasks()
                    self._drain_events()
                    self._check_native_lifecycle()

                    if once and not self._has_work():
                        return self._selected_exit_code()

                    event = self.server.next_event(timeout=self.poll_interval)
                    if event is not None:
                        self._handle_event(event)
                    elif not getattr(self.server, "alive", True):
                        raise RpcError("Codex app-server exited while the runner was active")
            except KeyboardInterrupt:
                return self._stop_active_tasks()
            except Exception as exc:
                self.last_error = _error_message(exc)
                self.store.event(None, "runner/error", {"message": self.last_error})
                # An ambiguous dispatch or an unconfirmed native child means
                # another writer cannot safely start in this server process.
                # Closing the owned process first is the stop boundary.
                self._close_server()
                self._mark_active_lost(f"Runner could not confirm task completion: {self.last_error}")
                self._mark_active_agents_lost(f"Runner stopped before native agent completion was confirmed: {self.last_error}")
                return 1
            finally:
                self._close_server()
                self._mark_active_agents_lost("Runner closed before native agent completion was confirmed.")
                self._set_runner("stopped", error=self.last_error)
        finally:
            lock.__exit__(None, None, None)

    def _recover_active_tasks(self) -> None:
        """Mark old in-flight work lost only while this runner owns the lock."""

        for task in self.store.tasks():
            if task.get("status") not in ACTIVE:
                continue
            previous_run = task.get("run_id")
            message = "A previous runner stopped before this task's turn completion was recorded; it was not replayed."
            self.store.update_task(
                task["id"],
                status="lost",
                activity="Lost after runner restart; explicit continuation required",
                error=message,
            )
            self.store.event(
                task["id"],
                "task/lost",
                {"reason": "runner_restart", "previous_run_id": previous_run, "thread_id": task.get("thread_id"), "turn_id": task.get("turn_id")},
            )

    def _expire_old_requests(self) -> None:
        for request in self.store.requests():
            if request.get("status") not in {"pending", "answered"}:
                continue
            if request.get("run_id") == self.run_id:
                continue
            self.store.update_request(request["id"], status="expired", expired_at=time.time())
            self.store.event(request.get("task_id"), "request/expired", {"request_id": request["id"], "reason": "previous_runner"})

    def _refresh_models(self) -> None:
        models = []
        cursor = None
        seen_cursors: set[str] = set()
        try:
            while True:
                params: dict[str, Any] = {"includeHidden": False, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                page = self.server.request("model/list", params)
                if not isinstance(page, Mapping):
                    raise RpcError("model/list returned an invalid response")
                data = page.get("data")
                if not isinstance(data, list):
                    raise RpcError("model/list response is missing its data list")
                models.extend(item for item in data if isinstance(item, dict))
                next_cursor = page.get("nextCursor")
                if not next_cursor:
                    break
                if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                    raise RpcError("model/list returned an invalid pagination cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except Exception as exc:
            self.store.event(None, "models/refresh_failed", {"message": _error_message(exc)})
            return
        self.store.set_setting("models", models)
        self.store.set_setting("models_updated_at", time.time())
        self.store.event(None, "models/refreshed", {"count": len(models)})

    def _set_runner(self, status: str, **extra: Any) -> None:
        current = self.store.get_setting("runner", {}) or {}
        now = time.time()
        current.update(
            run_id=self.run_id,
            pid=os.getpid(),
            project=str(self.store.project),
            status=status,
            heartbeat=now,
        )
        if status == "starting":
            current["started_at"] = now
            current.pop("finished_at", None)
        elif status == "stopped":
            current["finished_at"] = now
        current.update(extra)
        self.store.set_setting("runner", current)
        self._last_heartbeat = current["heartbeat"]

    def _heartbeat(self) -> None:
        if time.time() - self._last_heartbeat < _HEARTBEAT_INTERVAL:
            return
        current = self.store.get_setting("runner", {}) or {}
        current.update(run_id=self.run_id, pid=os.getpid(), project=str(self.store.project), status="running", heartbeat=time.time())
        self.store.set_setting("runner", current)
        self._last_heartbeat = current["heartbeat"]

    def _close_server(self) -> None:
        if not self._started_server or self.server is None:
            return
        self._started_server = False
        try:
            self.server.close()
        except Exception as exc:
            if self.last_error is None:
                self.last_error = _error_message(exc)

    def _active_tasks(self) -> list[dict[str, Any]]:
        return [
            task
            for task in self.store.tasks()
            if task.get("status") in ACTIVE and task.get("run_id") == self.run_id
        ]

    def _has_work(self) -> bool:
        return any(
            task.get("status") == "queued"
            or (task.get("status") in ACTIVE and task.get("run_id") == self.run_id)
            for task in self.store.tasks()
        ) or bool(self._active_native_agents())

    def _selected_exit_code(self) -> int:
        for task_id in self._selected_ids:
            try:
                task = self.store.get_task(task_id)
            except ValueError:
                continue
            if task.get("status") != "completed":
                return 1
        return 0

    def _dependency_state(self, task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> tuple[str, str | None]:
        dependencies = task.get("depends_on") or []
        for dep_id in dependencies:
            dependency = by_id.get(dep_id)
            if dependency is None:
                return "blocked", f"Dependency {dep_id} no longer exists."
            status = dependency.get("status")
            if status in TERMINAL and status != "completed":
                return "blocked", f"Dependency {dep_id} ended with status {status}."
        if all(by_id.get(dep_id, {}).get("status") == "completed" for dep_id in dependencies):
            return "ready", None
        return "waiting", None

    def _ready_tasks(self) -> list[dict[str, Any]]:
        tasks = self.store.tasks()
        by_id = {task["id"]: task for task in tasks}
        ready: list[dict[str, Any]] = []
        for task in tasks:
            if task.get("status") not in {"queued", "blocked"}:
                continue
            state, reason = self._dependency_state(task, by_id)
            if state == "blocked":
                if task.get("status") != "blocked" or task.get("error") != reason:
                    updated = self.store.update_task_if(
                        task["id"], {"status": task["status"]}, status="blocked", error=reason,
                        activity="Blocked by a dependency",
                    )
                    if updated:
                        self.store.event(task["id"], "task/blocked", {"reason": reason})
            elif state == "ready":
                if task.get("status") == "blocked":
                    updated = self.store.update_task_if(
                        task["id"], {"status": "blocked"}, status="queued", error=None,
                        activity="Dependencies completed; queued",
                    )
                    if not updated:
                        continue
                    task = updated
                    self.store.event(task["id"], "task/unblocked", {"reason": "dependencies_completed"})
                ready.append(task)
            elif task.get("status") == "blocked":
                task = self.store.update_task_if(
                    task["id"], {"status": "blocked"}, status="queued", error=None,
                    activity="Waiting for dependencies",
                )
                if not task:
                    continue
                self.store.event(task["id"], "task/unblocked", {"reason": "dependency_requeued"})

        active = self._active_tasks()
        if not active and not ready:
            # A queued dependency cycle or otherwise unresolved graph must not
            # keep one-shot mode alive forever.
            refreshed = self.store.tasks()
            refreshed_by_id = {task["id"]: task for task in refreshed}
            for task in refreshed:
                if task.get("status") != "queued":
                    continue
                state, _ = self._dependency_state(task, refreshed_by_id)
                if state == "waiting":
                    reason = "Dependencies cannot be resolved; inspect the task graph and explicitly requeue it."
                    updated = self.store.update_task_if(
                        task["id"], {"status": "queued"}, status="blocked", error=reason,
                        activity="Blocked by unresolved dependencies",
                    )
                    if updated:
                        self.store.event(task["id"], "task/blocked", {"reason": reason})
        return ready

    def _schedule_ready_tasks(self) -> None:
        active = self._active_tasks()
        native_active = self._active_native_agents()
        active_owner_ids = {task["id"] for task in active}
        active_owner_ids.update(agent.get("task_id") for agent in native_active if agent.get("task_id"))
        owner_tasks = []
        for task_id in active_owner_ids:
            try:
                owner_tasks.append(self.store.get_task(task_id))
            except ValueError:
                continue
        writer_active = any(task.get("current_sandbox") == "workspace-write" for task in owner_tasks)
        if len(active) >= self.max_workers:
            return
        ready = self._ready_tasks()
        if not ready:
            return
        # A writer is exclusive against both readers and writers. Read-only
        # tasks may share capacity with other read-only tasks.
        if owner_tasks and writer_active:
            return

        for task in ready:
            if len(active) >= self.max_workers:
                break
            try:
                policy = self.store.task_policy(task)
            except Exception as exc:
                message = _error_message(exc)
                self.store.update_task(task["id"], status="failed", error=message, activity="Could not resolve role policy")
                self.store.event(task["id"], "task/failed", {"message": message})
                continue

            sandbox = policy.get("sandbox")
            if sandbox not in {"read-only", "workspace-write"}:
                message = f"Unsupported role sandbox: {sandbox!r}"
                self.store.update_task(task["id"], status="failed", error=message, activity="Invalid role sandbox")
                self.store.event(task["id"], "task/failed", {"message": message})
                continue
            if sandbox == "workspace-write" and owner_tasks:
                continue
            if sandbox == "read-only" and writer_active:
                continue

            claimed = self.store.claim_task(task["id"], self.run_id, policy)
            if claimed is None:
                continue
            self._dispatch_task(claimed, policy)
            updated = self.store.get_task(task["id"])
            if updated.get("status") in ACTIVE and updated.get("run_id") == self.run_id:
                active.append(updated)
                owner_tasks.append(updated)
                if sandbox == "workspace-write":
                    writer_active = True
                    break

    def _dispatch_task(self, task: dict[str, Any], policy: dict[str, Any]) -> None:
        task_id = task["id"]
        model = policy["model"]
        effort = policy["effort"]
        role = policy["name"]
        thread_id = task.get("thread_id")
        try:
            if thread_id:
                self.server.request("thread/resume", {"threadId": thread_id})
            else:
                response = self.server.request(
                    "thread/start",
                    {
                        "cwd": str(self.store.project),
                        "model": model,
                        "sandbox": policy["sandbox"],
                        "developerInstructions": (
                            "You are executing an already-dispatched managed task. Do not re-enqueue this assignment, "
                            "start another runner, or wait for new managed tasks in this same runner. Use native Codex "
                            "subagents for any necessary internal delegation and wait for them before completing. The "
                            "calling parent owns cross-task integration."
                        ),
                    },
                )
                thread = response.get("thread") if isinstance(response, Mapping) else None
                thread_id = thread.get("id") if isinstance(thread, Mapping) else None
                if not isinstance(thread_id, str) or not thread_id:
                    raise RpcError("thread/start response did not include a thread ID")

            # Publish the task/thread relationship before starting the turn.
            # The server can emit turn notifications before its RPC response.
            self._thread_to_task[thread_id] = task_id
            self.store.update_task(
                task_id,
                status="running",
                thread_id=thread_id,
                turn_id=None,
                run_id=self.run_id,
                current_model=model,
                current_effort=effort,
                current_sandbox=policy["sandbox"],
                observed_model=None,
                live_update_status=None,
                activity="Starting turn",
                error=None,
            )
            self.store.event(task_id, "task/turn_starting", {"thread_id": thread_id, "role": role, "model": model, "effort": effort})

            response = self.server.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": self._task_input(task, policy)}],
                    "model": model,
                    "effort": effort,
                    "sandboxPolicy": self._sandbox_policy(policy),
                    "turnTrigger": "codex-orchestrator",
                },
            )
            turn = response.get("turn") if isinstance(response, Mapping) else None
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise RpcError("turn/start returned without a turn ID; the outcome is uncertain.")
            self.store.update_task(task_id, turn_id=turn_id, activity="Running")
            self.store.event(task_id, "task/turn_started", {"thread_id": thread_id, "turn_id": turn_id, "model": model, "effort": effort})
        except RpcError as exc:
            if exc.code is None:
                raise
            else:
                self._mark_task_failed(task_id, _error_message(exc))
        except Exception as exc:
            raise RpcError(f"Turn dispatch outcome is uncertain: {_error_message(exc)}") from exc

    def _task_input(self, task: dict[str, Any], policy: dict[str, Any]) -> str:
        lines = [
            f"Role: {policy['name']}",
            _text(policy.get("instructions")),
            "",
            "Bounded objective:",
            _text(task.get("prompt")),
        ]
        dependencies = task.get("depends_on") or []
        if dependencies:
            completed = []
            for dependency_id in dependencies:
                try:
                    dependency = self.store.get_task(dependency_id)
                except ValueError:
                    continue
                if dependency.get("status") != "completed":
                    continue
                result = _bounded_text(_text(dependency.get("result")), 12 * 1024)
                completed.append(f"Dependency {dependency_id} ({dependency.get('title') or 'task'}):\n{result}")
            if completed:
                lines.extend(["", "Completed dependency results:", *completed])
        return "\n".join(lines)

    def _sandbox_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        if policy["sandbox"] == "read-only":
            return {"type": "readOnly"}
        return {"type": "workspaceWrite", "writableRoots": [str(self.store.project)]}

    def _process_controls(self) -> None:
        for control in self.store.controls():
            if control.get("status") != "pending":
                continue
            if control.get("run_id") != self.run_id:
                self.store.update_control(control["id"], status="expired", completed_at=time.time())
                continue
            try:
                task = self.store.get_task(control["task_id"])
            except ValueError:
                self.store.update_control(control["id"], status="expired", completed_at=time.time())
                continue
            if task.get("run_id") != self.run_id or task.get("status") not in ACTIVE:
                self.store.update_control(control["id"], status="expired", completed_at=time.time())
                continue

            kind = control.get("kind")
            if not task.get("thread_id"):
                # The task is still creating/resuming its thread. Keep the
                # interrupt until dispatch has either produced a turn ID or
                # failed; do not bind it to a later continuation.
                if kind == "interrupt" and control.get("turn_id") is None:
                    continue
                self.store.update_control(control["id"], status="expired", completed_at=time.time())
                continue
            if kind == "interrupt" and control.get("turn_id") is None:
                # Store.interrupt_task can run after claim but before the
                # first turn response. In this run, the first confirmed turn
                # is the intended target.
                if not task.get("turn_id"):
                    continue
            elif not task.get("turn_id"):
                if kind == "model":
                    self.store.update_control(control["id"], status="target_unavailable", completed_at=time.time())
                    self.store.update_task(task["id"], live_update_status="targetUnavailable")
                continue
            elif control.get("turn_id") != task.get("turn_id"):
                self.store.update_control(control["id"], status="expired", completed_at=time.time())
                continue

            if kind == "interrupt":
                self._send_interrupt(control, task)
            elif kind == "model":
                self._publish_live_model(control, task)
            else:
                self.store.update_control(control["id"], status="unsupported", completed_at=time.time())
                self.store.event(task["id"], "control/unsupported", {"kind": control.get("kind")})

    def _send_interrupt(self, control: dict[str, Any], task: dict[str, Any]) -> None:
        try:
            self.server.request("turn/interrupt", {"threadId": task["thread_id"], "turnId": task["turn_id"]})
        except Exception as exc:
            message = _error_message(exc)
            self.store.update_control(control["id"], status="failed", error=message, completed_at=time.time())
            self.store.event(task["id"], "task/interrupt_failed", {"message": message})
            return
        self.store.update_control(control["id"], status="sent", completed_at=time.time())
        self.store.update_task(task["id"], status="interrupting", activity="Interrupt requested")
        self.store.event(task["id"], "task/interrupt_sent", {"turn_id": task["turn_id"]})

    def _publish_live_model(self, control: dict[str, Any], task: dict[str, Any]) -> None:
        model = control.get("model")
        effort = control.get("effort")
        if not self.enable_live_models:
            message = "Live model publication is disabled for this runner; start serve with --live-models. The new policy applies to a future turn."
            self._live_model_supported = False
            self.store.update_control(control["id"], status="unsupported", error=message, completed_at=time.time())
            self.store.update_task(task["id"], live_update_status="unsupported", live_update_error=message,
                                   activity="Live model updates are disabled; next turn uses the pending model")
            self.store.event(task["id"], "model/live_update_failed", {"status": "unsupported", "message": message, "model": model, "effort": effort})
            return
        if self._live_model_supported is False:
            message = "This Codex app-server does not support live turn settings; the new policy applies to a future turn."
            self.store.update_control(control["id"], status="unsupported", error=message, completed_at=time.time())
            self.store.update_task(task["id"], live_update_status="unsupported", live_update_error=message, activity="Live model updates are unsupported; next turn uses the pending model")
            self.store.event(task["id"], "model/live_update_failed", {"status": "unsupported", "message": message, "model": model, "effort": effort})
            return
        params: dict[str, Any] = {"threadId": task["thread_id"], "turnId": task["turn_id"], "model": model}
        if effort is not None:
            params["effort"] = effort
        self.store.update_control(control["id"], status="sending")
        try:
            response = self.server.request("turn/settings/update", params)
        except RpcError as exc:
            disabled_feature = exc.code == -32600 and "step_model_switching" in str(exc) and "feature" in str(exc).lower()
            if exc.code == _METHOD_NOT_FOUND or disabled_feature:
                status = "unsupported"
                if disabled_feature:
                    message = "This Codex app-server has step_model_switching disabled; start serve with --live-models. The new policy applies to a future turn."
                else:
                    message = "This Codex app-server does not support turn/settings/update; the new policy applies to a future turn."
                control_status = "unsupported"
                self._live_model_supported = False
            else:
                status = "failed"
                message = _error_message(exc)
                control_status = "failed"
            self.store.update_control(control["id"], status=control_status, error=message, completed_at=time.time())
            self.store.update_task(task["id"], live_update_status=status, live_update_error=message, activity=f"Live model update {status}; next turn uses the pending model")
            self.store.event(task["id"], "model/live_update_failed", {"status": status, "message": message, "model": model, "effort": effort})
            return
        except Exception as exc:
            message = _error_message(exc)
            self.store.update_control(control["id"], status="failed", error=message, completed_at=time.time())
            self.store.update_task(task["id"], live_update_status="failed", live_update_error=message, activity="Live model update failed; next turn uses the pending model")
            self.store.event(task["id"], "model/live_update_failed", {"status": "failed", "message": message, "model": model, "effort": effort})
            return

        status = response.get("status") if isinstance(response, Mapping) else None
        if status == "applied":
            self._live_model_supported = True
            fields = {"live_model": model, "live_update_status": "applied", "live_update_error": None}
            if effort is not None:
                fields["live_effort"] = effort
            self.store.update_task(task["id"], **fields, activity=f"Live model setting published: {model}")
            self.store.update_control(control["id"], status="published", completed_at=time.time())
            self.store.event(task["id"], "model/live_published", {"model": model, "effort": effort, "turn_id": task["turn_id"], "observed": False})
        elif status == "targetUnavailable":
            self._live_model_supported = True
            message = "The current turn was no longer available for a live model update; the next turn uses the pending model."
            self.store.update_task(task["id"], live_update_status="targetUnavailable", live_update_error=None, activity="Live model target unavailable; next turn uses the pending model")
            self.store.update_control(control["id"], status="target_unavailable", completed_at=time.time())
            self.store.event(task["id"], "model/live_target_unavailable", {"model": model, "effort": effort, "turn_id": task["turn_id"], "message": message})
        else:
            message = "Codex app-server returned an unknown turn/settings/update status."
            self.store.update_task(task["id"], live_update_status="failed", live_update_error=message, activity="Live model update returned an unknown status")
            self.store.update_control(control["id"], status="failed", error=message, completed_at=time.time())
            self.store.event(task["id"], "model/live_update_failed", {"status": "failed", "message": message, "returned_status": status})

    def _deliver_answered_requests(self) -> None:
        for request in self.store.requests(status="answered"):
            if request.get("run_id") != self.run_id:
                self.store.update_request(request["id"], status="expired", expired_at=time.time())
                continue
            try:
                task = self.store.get_task(request["task_id"])
            except ValueError:
                self.store.update_request(request["id"], status="expired", expired_at=time.time())
                continue
            params = request.get("params") or {}
            context = self._thread_context(params.get("threadId"), params.get("turnId"))
            if not context or context[0].get("id") != task.get("id"):
                self.store.update_request(request["id"], status="expired", expired_at=time.time())
                continue
            try:
                self.server.respond(request["rpc_id"], result=request.get("response"))
            except Exception as exc:
                message = _error_message(exc)
                self.store.update_request(request["id"], status="failed", error=message, completed_at=time.time())
                self.store.event(task["id"], "request/delivery_failed", {"request_id": request["id"], "message": message})
                continue
            self.store.update_request(request["id"], status="delivered", delivered_at=time.time())
            self.store.event(task["id"], "request/delivered", {"request_id": request["id"], "thread_id": params.get("threadId")})
            self._restore_approval_status_if_clear(task["id"])

    def _drain_events(self, limit: int = 128) -> None:
        for _ in range(limit):
            event = self.server.next_event(timeout=0)
            if event is None:
                return
            self._handle_event(event)
            # A notification can make a queued live-model or interrupt
            # control actionable. Service it before draining later terminal
            # notifications so a fast turn completion cannot hide the result.
            self._process_controls()
            self._deliver_answered_requests()

    def _handle_event(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        if not isinstance(method, str):
            return
        if "id" in event:
            self._handle_server_request(event)
            return
        params = event.get("params") if isinstance(event.get("params"), Mapping) else {}
        if method == "thread/started":
            self._thread_started(params.get("thread") or {})
        elif method == "thread/status/changed":
            self._thread_status_changed(params)
        elif method == "thread/settings/updated":
            self._thread_settings_updated(params)
        elif method == "turn/started":
            self._turn_started(params)
        elif method == "turn/completed":
            self._turn_completed(params)
        elif method == "item/started":
            self._item_started(params)
        elif method == "item/completed":
            self._item_completed(params)
        elif method == "item/agentMessage/delta":
            self._agent_message_delta(params)
        elif method == "thread/tokenUsage/updated":
            self._token_usage_updated(params)
        elif method == "model/rerouted":
            self._model_rerouted(params)
        elif method == "turn/plan/updated":
            self._plan_updated(params)
        elif method == "serverRequest/resolved":
            self._server_request_resolved(params)
        elif method.startswith("item/reasoning/"):
            # Never persist raw or summarized reasoning notifications.
            return
        elif method == "error":
            self.store.event(None, "app_server/error", {"message": _safe_display(_text(params.get("message")))})

    def _task_by_thread(self, thread_id: Any) -> dict[str, Any] | None:
        if not isinstance(thread_id, str):
            return None
        task_id = self._thread_to_task.get(thread_id)
        if task_id:
            try:
                return self.store.get_task(task_id)
            except ValueError:
                return None
        for task in self.store.tasks():
            if task.get("thread_id") == thread_id and task.get("run_id") == self.run_id:
                self._thread_to_task[thread_id] = task["id"]
                return task
        return None

    def _agent_by_thread(self, thread_id: Any) -> dict[str, Any] | None:
        if not isinstance(thread_id, str):
            return None
        return next((agent for agent in self.store.agents() if agent.get("id") == thread_id), None)

    def _thread_context(
        self,
        thread_id: Any,
        turn_id: Any = None,
        *,
        allow_new_agent_turn: bool = False,
        allow_orphaned_agent: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        """Return an event owner only while its managed run/turn is current.

        Managed-thread events must match the turn ID installed from the
        ``turn/start`` response. Native agent events are tied to the active
        root turn and to the current parent/child turn chain.
        """

        task = self._task_by_thread(thread_id)
        if task and task.get("thread_id") == thread_id:
            if task.get("run_id") != self.run_id or task.get("status") not in ACTIVE:
                return None
            if turn_id is not None and (not isinstance(turn_id, str) or turn_id != task.get("turn_id")):
                return None
            return task, None

        agent = self._agent_by_thread(thread_id)
        if not agent or agent.get("run_id") != self.run_id:
            return None
        try:
            task = self.store.get_task(agent.get("task_id"))
        except (ValueError, TypeError):
            return None
        if task.get("run_id") != self.run_id or (
            task.get("status") not in ACTIVE and not (allow_orphaned_agent and agent.get("orphaned_at") is not None)
        ):
            return None
        root_turn_id = agent.get("root_turn_id")
        if not isinstance(root_turn_id, str) or root_turn_id != task.get("turn_id"):
            return None

        # A nested child is valid only while each recorded parent turn is
        # still the current turn for that thread.
        child = agent
        seen: set[str] = set()
        while True:
            parent_id = child.get("parent_thread_id")
            if not isinstance(parent_id, str) or parent_id in seen:
                return None
            seen.add(parent_id)
            if parent_id == task.get("thread_id"):
                if child.get("parent_turn_id") != task.get("turn_id"):
                    return None
                break
            parent = self._agent_by_thread(parent_id)
            if (
                not parent
                or parent.get("run_id") != self.run_id
                or parent.get("task_id") != task.get("id")
                or parent.get("root_turn_id") != task.get("turn_id")
                or not isinstance(parent.get("turn_id"), str)
                or child.get("parent_turn_id") != parent.get("turn_id")
            ):
                return None
            child = parent

        if turn_id is not None:
            if not isinstance(turn_id, str):
                return None
            current_turn_id = agent.get("turn_id")
            if turn_id != current_turn_id:
                if not allow_new_agent_turn or not (
                    current_turn_id is None
                    or agent.get("pending_turn_start")
                    or agent.get("status") in _TERMINAL_TURNS
                ):
                    return None
        return task, agent

    def _active_native_agents(self, task_id: str | None = None) -> list[dict[str, Any]]:
        active = []
        for agent in self.store.agents():
            if agent.get("run_id") != self.run_id:
                continue
            if task_id is not None and agent.get("task_id") != task_id:
                continue
            if agent.get("turn_active") or agent.get("status") in {"running", "pendingInit", "active"}:
                active.append(agent)
        return active

    def _begin_orphaned_agent_shutdown(self, task: dict[str, Any]) -> None:
        for agent in self._active_native_agents(task["id"]):
            if agent.get("orphaned_at") is None:
                updated = self.store.upsert_agent(agent["id"], orphaned_at=time.time(), turn_active=True)
                self._orphaned_agent_deadlines[agent["id"]] = time.monotonic() + max(self.shutdown_grace, 0.1)
                thread_id, turn_id = updated.get("id"), updated.get("turn_id")
                if isinstance(thread_id, str) and isinstance(turn_id, str):
                    try:
                        self.server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                        self.store.upsert_agent(thread_id, interrupt_requested=True, activity="Parent turn ended; interrupt requested")
                    except Exception as exc:
                        self.store.upsert_agent(thread_id, interrupt_requested=False, activity="Parent turn ended; child stop is unconfirmed")
                        self.store.event(task["id"], "agent/interrupt_failed", {"thread_id": thread_id, "message": _error_message(exc)})

    def _check_native_lifecycle(self) -> None:
        now = time.monotonic()
        for agent in self._active_native_agents():
            try:
                task = self.store.get_task(agent.get("task_id"))
            except (ValueError, TypeError):
                continue
            if task.get("status") in ACTIVE:
                continue
            if agent.get("orphaned_at") is None:
                self._begin_orphaned_agent_shutdown(task)
                continue
            deadline = self._orphaned_agent_deadlines.get(agent["id"])
            if deadline is not None and now >= deadline:
                raise RpcError("A native Codex subagent remained active after its parent turn completed.")

    def _mark_active_agents_lost(self, message: str) -> None:
        for agent in self._active_native_agents():
            self.store.upsert_agent(agent["id"], status="lost", turn_active=False, activity="Lost after app-server shutdown")
            self.store.event(agent.get("task_id"), "agent/lost", {"thread_id": agent["id"], "reason": message})

    def _thread_started(self, thread: dict[str, Any]) -> None:
        if not isinstance(thread, Mapping):
            return
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            return
        parent_id = thread.get("parentThreadId")
        if isinstance(parent_id, str):
            parent_context = self._thread_context(parent_id)
            if parent_context is None:
                return
            parent_task, parent_agent = parent_context
            parent_turn_id = parent_agent.get("turn_id") if parent_agent else parent_task.get("turn_id")
            if not isinstance(parent_turn_id, str):
                return
            status = self._status_name(thread.get("status"))
            self.store.upsert_agent(
                thread_id,
                native=True,
                task_id=parent_task["id"],
                parent_thread_id=parent_id,
                parent_turn_id=parent_turn_id,
                root_turn_id=parent_task.get("turn_id"),
                run_id=self.run_id,
                role=thread.get("agentRole"),
                nickname=thread.get("agentNickname"),
                model=thread.get("model"),
                effort=thread.get("reasoningEffort"),
                status=status,
                turn_active=status == "active",
                activity="Native Codex subagent started",
            )
            self.store.event(parent_task["id"], "agent/started", {"thread_id": thread_id, "role": thread.get("agentRole"), "nickname": thread.get("agentNickname")})
        else:
            context = self._thread_context(thread_id)
            if context:
                task, _ = context
                self.store.update_task(task["id"], thread_status=self._status_name(thread.get("status")))

    @staticmethod
    def _status_name(status: Any) -> str | None:
        if isinstance(status, Mapping):
            value = status.get("type")
            return value if isinstance(value, str) else None
        return status if isinstance(status, str) else None

    def _thread_status_changed(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        status = self._status_name(params.get("status"))
        context = self._thread_context(thread_id)
        if not context:
            return
        task, agent = context
        if agent:
            fields: dict[str, Any] = {"status": status}
            if status == "active":
                fields["turn_active"] = True
            elif status == "idle":
                fields["turn_active"] = False
            self.store.upsert_agent(agent["id"], **fields)
            if status == "idle":
                self._orphaned_agent_deadlines.pop(agent["id"], None)
            self.store.event(task["id"], "agent/status", {"thread_id": agent["id"], "status": status})
        else:
            self.store.update_task(task["id"], thread_status=status)
            self.store.event(task["id"], "thread/status", {"status": status})

    def _thread_settings_updated(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        settings = params.get("settings") if isinstance(params.get("settings"), Mapping) else {}
        context = self._thread_context(thread_id)
        if context:
            task, agent = context
            fields: dict[str, Any] = {}
            if isinstance(settings.get("model"), str):
                fields["model"] = settings["model"]
            if isinstance(settings.get("effort"), str):
                fields["effort"] = settings["effort"]
            if agent:
                if fields:
                    self.store.upsert_agent(agent["id"], **fields)
                self.store.event(task["id"], "agent/settings", {"thread_id": agent["id"], **fields})
            else:
                task_fields = {f"thread_{key}": value for key, value in fields.items()}
                if task_fields:
                    self.store.update_task(task["id"], **task_fields)
                self.store.event(task["id"], "thread/settings", task_fields)

    def _turn_started(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
        turn_id = turn.get("id")
        context = self._thread_context(thread_id, turn_id, allow_new_agent_turn=True)
        if not context:
            return
        task, agent = context
        if agent:
            self.store.upsert_agent(agent["id"], turn_id=turn_id, pending_turn_start=False, turn_active=True,
                                    status="running", activity="Native subagent turn started")
            self._orphaned_agent_deadlines.pop(agent["id"], None)
            self.store.event(task["id"], "agent/turn_started", {"thread_id": agent["id"], "turn_id": turn_id})
        else:
            # The turn ID is installed from the turn/start response before the
            # event queue is drained. Never let a delayed event revive work.
            fields = {"activity": "Running"} if task.get("status") == "running" else {}
            if fields:
                self.store.update_task(task["id"], **fields)
            self.store.event(task["id"], "turn/started", {"thread_id": thread_id, "turn_id": turn_id})

    def _turn_completed(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
        turn_id = turn.get("id")
        status = turn.get("status")
        if status not in _TERMINAL_TURNS:
            return
        context = self._thread_context(thread_id, turn_id, allow_orphaned_agent=True)
        if not context:
            return
        task, agent = context
        if agent:
            self.store.upsert_agent(agent["id"], status=status, turn_active=False,
                                    activity=f"Native subagent turn {status}")
            self._orphaned_agent_deadlines.pop(agent["id"], None)
            self.store.event(task["id"], "agent/turn_completed", {"thread_id": agent["id"], "turn_id": turn_id, "status": status})
            self._expire_thread_requests(task["id"], agent["id"], turn_id)
            if task.get("status") not in ACTIVE:
                self._begin_orphaned_agent_shutdown(task)
            return
        result = _text(task.get("result"))
        if not result and isinstance(thread_id, str) and isinstance(turn_id, str):
            result = self._fallback_messages.get((thread_id, turn_id), "")
        if status == "completed":
            self.store.update_task(task["id"], status="completed", result=_bounded_text(result, _MAX_RESULT_BYTES), error=None, activity="Completed")
        elif status == "interrupted":
            self.store.update_task(task["id"], status="interrupted", result=_bounded_text(result, _MAX_RESULT_BYTES), activity="Interrupted")
        else:
            error = turn.get("error")
            message = error.get("message") if isinstance(error, Mapping) else "Codex reported a failed turn."
            self.store.update_task(task["id"], status="failed", result=_bounded_text(result, _MAX_RESULT_BYTES), error=_safe_display(_text(message), 1000), activity="Failed")
        self.store.event(task["id"], "turn/completed", {"turn_id": turn_id, "status": status})
        self._expire_task_requests(task["id"], turn_id)
        self._expire_task_controls(task["id"], turn_id)
        self._begin_orphaned_agent_shutdown(self.store.get_task(task["id"]))

    def _item_started(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
        item_id = item.get("id")
        item_type = item.get("type")
        context = self._thread_context(thread_id, turn_id)
        if not context:
            return
        task, agent = context
        if isinstance(thread_id, str) and isinstance(turn_id, str) and isinstance(item_id, str) and item_type == "agentMessage":
            self._item_phases[(thread_id, turn_id, item_id)] = item.get("phase")
        activity = self._item_activity(item)
        if activity:
            if agent:
                label = _safe_display(f"Agent {agent.get('nickname') or agent.get('role') or agent['id']}: {activity}")
                self.store.upsert_agent(agent["id"], activity=activity)
                self.store.update_task(task["id"], activity=label)
                event_kind = "agent/item_started"
                event_data = {"thread_id": agent["id"], "item_id": item_id, "type": item_type, "activity": activity}
            else:
                self.store.update_task(task["id"], activity=activity)
                event_kind = "item/started"
                event_data = {"item_id": item_id, "type": item_type, "activity": activity}
            self.store.event(task["id"], event_kind, event_data)
        if item_type in {"collabAgentToolCall", "subAgentActivity"}:
            self._track_native_agents(task, item, thread_id, turn_id)
        elif item_type in {"commandExecution", "fileChange", "mcpToolCall"}:
            # Do not persist tool output or argument objects.
            summary = {"item_id": item_id, "type": item_type}
            if item_type == "commandExecution":
                summary["command"] = _safe_display(_text(item.get("command")))
            elif item_type == "mcpToolCall":
                summary["server"] = _safe_display(_text(item.get("server")), 80)
                summary["tool"] = _safe_display(_text(item.get("tool")), 80)
            summary["thread_id"] = agent["id"] if agent else thread_id
            self.store.event(task["id"], "agent/tool_started" if agent else "tool/started", summary)
        elif item_type == "agentMessage":
            phase = item.get("phase")
            self.store.event(task["id"], "agent/message_started" if agent else "message/started",
                             {"thread_id": agent["id"] if agent else thread_id, "item_id": item_id,
                              "phase": phase if phase == "commentary" else "other"})

    @staticmethod
    def _item_activity(item: Mapping[str, Any]) -> str | None:
        item_type = item.get("type")
        if item_type == "commandExecution":
            command = _safe_display(_text(item.get("command")))
            return f"Running: {command}" if command else "Running a command"
        if item_type == "mcpToolCall":
            server = _safe_display(_text(item.get("server")), 80)
            tool = _safe_display(_text(item.get("tool")), 80)
            label = ".".join(part for part in (server, tool) if part)
            return f"Calling tool {label}" if label else "Calling a tool"
        if item_type == "fileChange":
            return "Applying file changes"
        if item_type == "collabAgentToolCall":
            return "Coordinating native Codex agents"
        if item_type == "subAgentActivity":
            return "Native Codex agent activity"
        if item_type == "agentMessage":
            return "Preparing a response"
        return None

    def _track_native_agents(
        self,
        task: dict[str, Any],
        item: Mapping[str, Any],
        parent_thread: Any = None,
        parent_turn: Any = None,
    ) -> None:
        parent_thread = parent_thread if isinstance(parent_thread, str) else task.get("thread_id")
        parent_turn = parent_turn if isinstance(parent_turn, str) else task.get("turn_id")
        if item.get("type") == "subAgentActivity":
            child_id = item.get("agentThreadId")
            if isinstance(child_id, str):
                agent_path = _text(item.get("agentPath"))
                role = Path(agent_path).name if agent_path else None
                kind = _text(item.get("kind")) or "activity"
                terminal = kind.lower() in {"completed", "finished", "closed", "shutdown", "interrupted"}
                self.store.upsert_agent(
                    child_id,
                    native=True,
                    task_id=task["id"],
                    parent_thread_id=parent_thread,
                    parent_turn_id=parent_turn,
                    root_turn_id=task.get("turn_id"),
                    run_id=self.run_id,
                    role=role,
                    status="completed" if terminal else "running",
                    turn_active=not terminal,
                    pending_turn_start=not terminal,
                    activity=f"Native Codex agent {kind}",
                )
                self.store.event(task["id"], "agent/activity", {"thread_id": child_id, "role": role, "kind": kind})
            return

        receiver_ids = item.get("receiverThreadIds")
        states = item.get("agentsStates") if isinstance(item.get("agentsStates"), Mapping) else {}
        if not isinstance(receiver_ids, list):
            receiver_ids = list(states)
        for child_id in receiver_ids:
            if not isinstance(child_id, str):
                continue
            state = states.get(child_id) if isinstance(states.get(child_id), Mapping) else {}
            status = state.get("status")
            active = status in {"running", "pendingInit"} or item.get("status") == "inProgress"
            state_message = _safe_display(_text(state.get("message")), _MAX_ACTIVITY_CHARS)
            self.store.upsert_agent(
                child_id,
                native=True,
                task_id=task["id"],
                parent_thread_id=parent_thread,
                parent_turn_id=parent_turn,
                root_turn_id=task.get("turn_id"),
                run_id=self.run_id,
                model=item.get("model"),
                effort=item.get("reasoningEffort"),
                status=status or ("running" if item.get("type") == "collabAgentToolCall" else None),
                turn_active=active,
                pending_turn_start=active,
                activity=state_message or ("Native Codex agent dispatched" if active else "Native Codex agent state observed"),
            )
            self.store.event(task["id"], "agent/dispatched", {"thread_id": child_id, "model": item.get("model"), "status": status})

    def _item_completed(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
        item_id = item.get("id")
        item_type = item.get("type")
        context = self._thread_context(thread_id, turn_id)
        if not context:
            return
        task, agent = context
        if item_type == "agentMessage":
            text = _text(item.get("text"))
            phase = item.get("phase")
            if agent and phase == "final_answer":
                self.store.upsert_agent(agent["id"], output_summary=_bounded_text(_safe_display(text, 4000), 4000))
                self.store.event(task["id"], "agent/output", {"thread_id": agent["id"], "excerpt": _safe_display(text, 320)})
            elif not agent and phase == "final_answer":
                self.store.update_task(task["id"], result=_bounded_text(text, _MAX_RESULT_BYTES))
                self._fallback_messages.pop((thread_id, turn_id), None)
            elif not agent and phase is None and text and isinstance(thread_id, str) and isinstance(turn_id, str):
                self._fallback_messages[(thread_id, turn_id)] = _bounded_text(text, _MAX_RESULT_BYTES)
        activity = self._item_activity(item)
        if item_type == "commandExecution":
            activity = "Command completed" if item.get("status") == "completed" else "Command did not complete"
        elif item_type == "fileChange":
            activity = "File changes completed" if item.get("status") == "completed" else "File changes did not complete"
        elif item_type == "mcpToolCall":
            activity = "Tool call completed" if item.get("status") == "completed" else "Tool call did not complete"
        if activity:
            if agent:
                self.store.upsert_agent(agent["id"], activity=activity)
                self.store.update_task(task["id"], activity=_safe_display(f"Agent {agent.get('nickname') or agent.get('role') or agent['id']}: {activity}"))
            else:
                self.store.update_task(task["id"], activity=activity)
        summary = {"item_id": item_id, "type": item_type}
        if item_type in {"commandExecution", "fileChange", "mcpToolCall"}:
            summary["status"] = item.get("status")
        if item_type == "commandExecution":
            summary["command"] = _safe_display(_text(item.get("command")))
        elif item_type == "mcpToolCall":
            summary["server"] = _safe_display(_text(item.get("server")), 80)
            summary["tool"] = _safe_display(_text(item.get("tool")), 80)
        elif item_type == "agentMessage":
            summary["phase"] = phase if phase in {"commentary", "final_answer"} else "other"
        summary["thread_id"] = agent["id"] if agent else thread_id
        self.store.event(task["id"], "agent/item_completed" if agent else "item/completed", summary)
        if item_type in {"collabAgentToolCall", "subAgentActivity"}:
            self._track_native_agents(task, item, thread_id, turn_id)

    def _agent_message_delta(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item_id = params.get("itemId")
        if not all(isinstance(value, str) for value in (thread_id, turn_id, item_id)):
            return
        # Only explicitly marked commentary is displayable. Providers may omit
        # phase, so unknown-phase text is kept out of persisted activity.
        if self._item_phases.get((thread_id, turn_id, item_id)) != "commentary":
            return
        context = self._thread_context(thread_id, turn_id)
        delta = _text(params.get("delta"))
        if not context or not delta:
            return
        task, agent = context
        key = (thread_id, turn_id, item_id)
        text = self._commentary.get(key, "") + delta
        self._commentary[key] = text[-_MAX_ACTIVITY_CHARS:]
        now = time.monotonic()
        if now - self._last_activity_write.get(thread_id, 0) < 0.35:
            return
        excerpt = _safe_display(self._commentary[key])
        activity = f"Commentary: {excerpt}"
        if agent:
            self.store.upsert_agent(agent["id"], activity=activity)
            self.store.update_task(task["id"], activity=_safe_display(f"Agent {agent.get('nickname') or agent.get('role') or agent['id']}: {activity}"))
            event_kind = "agent/message_commentary"
            data = {"thread_id": agent["id"], "item_id": item_id, "excerpt": excerpt}
        else:
            self.store.update_task(task["id"], activity=activity)
            event_kind = "message/commentary"
            data = {"item_id": item_id, "excerpt": excerpt}
        self.store.event(task["id"], event_kind, data)
        self._last_activity_write[thread_id] = now

    def _token_usage_updated(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        context = self._thread_context(thread_id, turn_id)
        usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), Mapping) else {}
        if not context:
            return
        task, agent = context
        total = usage.get("total") if isinstance(usage.get("total"), Mapping) else {}
        last = usage.get("last") if isinstance(usage.get("last"), Mapping) else {}
        clean_usage = {"last": dict(last), "total": dict(total), "model_context_window": usage.get("modelContextWindow")}
        if agent:
            self.store.upsert_agent(agent["id"], usage=clean_usage)
            event_kind = "agent/usage_updated"
            data = {"thread_id": agent["id"], "turn_id": turn_id, **clean_usage}
        else:
            self.store.update_task(task["id"], usage=clean_usage)
            event_kind = "usage/updated"
            data = {"turn_id": turn_id, **clean_usage}
        self.store.event(task["id"], event_kind, data)

    def _model_rerouted(self, params: Mapping[str, Any]) -> None:
        if not isinstance(params.get("turnId"), str):
            self.server.respond(rpc_id, error={"code": -32001, "message": "The request did not identify its active turn."})
            return
        context = self._thread_context(params.get("threadId"), params.get("turnId"))
        if not context:
            return
        task, agent = context
        observed = params.get("toModel")
        if isinstance(observed, str):
            if agent:
                self.store.upsert_agent(agent["id"], observed_model=observed)
            else:
                self.store.update_task(task["id"], observed_model=observed, observed_turn_id=params.get("turnId"))
        self.store.event(
            task["id"],
            "agent/model_rerouted" if agent else "model/rerouted",
            {"thread_id": agent["id"] if agent else params.get("threadId"), "turn_id": params.get("turnId"),
             "from_model": params.get("fromModel"), "to_model": observed, "reason": params.get("reason")},
        )

    def _plan_updated(self, params: Mapping[str, Any]) -> None:
        context = self._thread_context(params.get("threadId"), params.get("turnId"))
        plan = params.get("plan")
        if not context or not isinstance(plan, list):
            return
        task, agent = context
        clean_plan = []
        for entry in plan[:_MAX_PLAN_STEPS]:
            if not isinstance(entry, Mapping):
                continue
            step = _safe_display(_text(entry.get("step") or entry.get("text") or entry.get("description")), 180)
            if not step:
                continue
            clean_plan.append({"step": step, "status": _safe_display(_text(entry.get("status")), 40)})
        if not clean_plan:
            return
        current = next((entry for entry in clean_plan if entry["status"] not in {"completed", "done"}), clean_plan[0])
        if agent:
            activity = f"Plan: {current['step']}"
            self.store.upsert_agent(agent["id"], plan=clean_plan, activity=activity)
            self.store.update_task(task["id"], activity=_safe_display(f"Agent {agent.get('nickname') or agent.get('role') or agent['id']}: {activity}"))
            event_kind = "agent/plan_updated"
            data = {"thread_id": agent["id"], "steps": clean_plan}
        else:
            self.store.update_task(task["id"], plan=clean_plan, activity=f"Plan: {current['step']}")
            event_kind = "plan/updated"
            data = {"steps": clean_plan}
        self.store.event(task["id"], event_kind, data)

    def _handle_server_request(self, request: Mapping[str, Any]) -> None:
        method = request.get("method")
        rpc_id = request.get("id")
        params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
        if method not in _APPROVAL_METHODS:
            self.server.respond(rpc_id, error={"code": _METHOD_NOT_FOUND, "message": f"Unsupported app-server request: {method}"})
            return
        if not isinstance(params.get("turnId"), str):
            self.server.respond(rpc_id, error={"code": -32001, "message": "The request did not identify its active turn."})
            return
        context = self._thread_context(params.get("threadId"), params.get("turnId"))
        if not context:
            self.server.respond(rpc_id, error={"code": -32001, "message": "No active managed task owns this request."})
            return
        task, agent = context
        request_row = self.store.add_request(rpc_id, method, dict(params), self.run_id, task["id"])
        self.store.update_task(task["id"], status="waiting_approval", activity="Waiting for user approval/input")
        self.store.event(task["id"], "request/pending", {"request_id": request_row["id"], "method": method,
                                                            "thread_id": params.get("threadId"),
                                                            "agent_thread_id": agent["id"] if agent else None})

    def _server_request_resolved(self, params: Mapping[str, Any]) -> None:
        rpc_id = params.get("requestId")
        thread_id = params.get("threadId")
        if rpc_id is None or not isinstance(thread_id, str):
            return
        matched = next((
            request for request in self.store.requests()
            if request.get("run_id") == self.run_id
            and request.get("rpc_id") == rpc_id
            and request.get("status") in {"pending", "answered", "delivered", "sent"}
            and (request.get("params") or {}).get("threadId") == thread_id
        ), None)
        if not matched:
            return
        self.store.update_request(matched["id"], status="resolved", resolved_at=time.time())
        task_id = matched.get("task_id")
        if task_id:
            self.store.event(task_id, "request/resolved", {"request_id": matched["id"], "thread_id": thread_id})
            self._restore_approval_status_if_clear(task_id)

    def _restore_approval_status_if_clear(self, task_id: str) -> None:
        try:
            task = self.store.get_task(task_id)
        except ValueError:
            return
        if task.get("run_id") != self.run_id or task.get("status") != "waiting_approval":
            return
        unresolved = [
            request for request in self.store.requests()
            if request.get("task_id") == task_id
            and request.get("run_id") == self.run_id
            and request.get("status") in {"pending", "answered", "sending"}
        ]
        if not unresolved:
            self.store.update_task(task_id, status="running", activity="Approval request resolved; waiting for Codex")

    def _expire_task_requests(self, task_id: str, turn_id: str | None) -> None:
        for request in self.store.requests():
            if request.get("task_id") != task_id or request.get("run_id") != self.run_id:
                continue
            if request.get("status") not in {"pending", "answered"}:
                continue
            self.store.update_request(request["id"], status="expired", expired_at=time.time())
            self.store.event(task_id, "request/expired", {"request_id": request["id"], "reason": "turn_completed"})

    def _expire_thread_requests(self, task_id: str, thread_id: str, turn_id: str) -> None:
        for request in self.store.requests():
            if request.get("task_id") != task_id or request.get("run_id") != self.run_id:
                continue
            if request.get("status") not in {"pending", "answered"}:
                continue
            params = request.get("params") or {}
            if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                continue
            self.store.update_request(request["id"], status="expired", expired_at=time.time())
            self.store.event(task_id, "request/expired", {"request_id": request["id"], "reason": "agent_turn_completed"})

    def _expire_task_controls(self, task_id: str, turn_id: str | None) -> None:
        for control in self.store.controls():
            if control.get("task_id") != task_id or control.get("run_id") != self.run_id:
                continue
            if control.get("status") not in {"pending", "sending"}:
                continue
            if turn_id and control.get("turn_id") not in {None, turn_id}:
                continue
            self.store.update_control(control["id"], status="expired", completed_at=time.time())

    def _mark_task_failed(self, task_id: str, message: str) -> None:
        self.store.update_task(task_id, status="failed", error=message, activity="Failed to start turn")
        self.store.event(task_id, "task/failed", {"message": message})

    def _mark_task_lost(self, task_id: str, message: str) -> None:
        self.store.update_task(task_id, status="lost", error=message, activity="Lost; explicit continuation required")
        self.store.event(task_id, "task/lost", {"reason": message})

    def _mark_active_lost(self, message: str) -> None:
        for task in self._active_tasks():
            self._mark_task_lost(task["id"], message)

    def _stop_active_tasks(self) -> int:
        if self._stopping:
            return self._selected_exit_code()
        self._stopping = True
        self._set_runner("stopping")
        for task in self._active_tasks():
            if not task.get("thread_id") or not task.get("turn_id"):
                self._mark_task_lost(task["id"], "Runner stopped before the turn lifecycle was confirmed.")
                continue
            try:
                self.server.request("turn/interrupt", {"threadId": task["thread_id"], "turnId": task["turn_id"]})
            except Exception as exc:
                self.store.event(task["id"], "task/interrupt_failed", {"message": _error_message(exc)})
                continue
            self.store.update_task(task["id"], status="interrupting", activity="Runner stopping; interrupt requested")
            self.store.event(task["id"], "task/interrupt_sent", {"turn_id": task["turn_id"], "reason": "runner_stop"})

        for agent in self._active_native_agents():
            if not agent.get("id") or not agent.get("turn_id"):
                continue
            try:
                self.server.request("turn/interrupt", {"threadId": agent["id"], "turnId": agent["turn_id"]})
                self.store.upsert_agent(agent["id"], interrupt_requested=True, activity="Runner stopping; interrupt requested")
            except Exception as exc:
                self.store.event(agent.get("task_id"), "agent/interrupt_failed", {"thread_id": agent["id"], "message": _error_message(exc)})

        deadline = time.monotonic() + self.shutdown_grace
        while time.monotonic() < deadline and (self._active_tasks() or self._active_native_agents()):
            self._deliver_answered_requests()
            event = self.server.next_event(timeout=min(self.poll_interval, max(0, deadline - time.monotonic())))
            if event is not None:
                self._handle_event(event)
        self._mark_active_lost("Runner stopped before Codex confirmed a terminal turn status.")
        if self._active_native_agents():
            return 1
        return self._selected_exit_code()
