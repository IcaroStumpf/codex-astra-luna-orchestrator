"""Durable controls for active tasks and project dispatch."""

from __future__ import annotations

import json
import time

from .store import identifier, now


def steer_task(store, task_id: str, prompt: str) -> dict:
    """Queue text for the task's currently active Codex turn.

    The run, thread, and turn IDs are captured together so a delayed control
    cannot be applied to a later continuation of the task.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Steering prompt must not be empty.")

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        task = store._read(db, "tasks", "id", task_id)
        if task.get("status") not in {"running", "waiting_approval"}:
            if task.get("status") == "interrupting":
                raise ValueError("Cannot steer a task while interruption is pending.")
            raise ValueError("Only a running task can be steered.")
        run_id = task.get("run_id")
        thread_id = task.get("thread_id")
        turn_id = task.get("turn_id")
        if not all(isinstance(value, str) and value for value in (run_id, thread_id, turn_id)):
            raise ValueError("Steering requires a confirmed active runner, thread, and turn.")

        control = {
            "id": identifier(),
            "task_id": task_id,
            "kind": "steer",
            "status": "pending",
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "prompt": prompt,
            "created_at": now(),
        }
        db.execute("INSERT INTO controls VALUES (?, ?)", (control["id"], json.dumps(control)))
        store._event(db, task_id, "task/steer_requested", {
            "control_id": control["id"],
            "thread_id": thread_id,
            "turn_id": turn_id,
        })
        return control


def set_dispatch(store, paused: bool) -> dict:
    """Persist whether this project's runner should dispatch queued work."""

    if not isinstance(paused, bool):
        raise ValueError("Dispatch pause state must be a boolean.")

    timestamp = time.time()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM settings WHERE key='dispatch'").fetchone()
        state = json.loads(row[0]) if row else {}
        if not isinstance(state, dict):
            raise ValueError("Stored dispatch setting is invalid.")
        state.update(paused=paused, updated_at=timestamp)
        if paused:
            state["paused_at"] = timestamp
        else:
            state["resumed_at"] = timestamp
        db.execute("INSERT OR REPLACE INTO settings VALUES ('dispatch', ?)", (json.dumps(state),))
        store._event(
            db,
            None,
            "dispatch/paused" if paused else "dispatch/resumed",
            {"paused": paused, "updated_at": timestamp},
        )
        return state
