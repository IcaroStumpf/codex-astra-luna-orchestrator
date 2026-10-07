from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_orchestrator.controls import set_dispatch, steer_task
from codex_orchestrator.engine import Engine
from codex_orchestrator.rpc import RpcError
from codex_orchestrator.store import Store


class ControlServer:
    """Small app-server stand-in for durable control lifecycle tests."""

    def __init__(self, *, reject_steer: bool = False, complete_on_start: bool = False):
        self.reject_steer = reject_steer
        self.complete_on_start = complete_on_start
        self.events = deque()
        self.calls: list[tuple[str, dict]] = []
        self.alive = False
        self.thread_number = 0
        self.turn_number = 0
        self.on_turn_started = None

    def start(self):
        self.alive = True
        return self

    def close(self):
        self.alive = False

    def request(self, method, params=None, timeout=None):
        params = params or {}
        self.calls.append((method, dict(params)))
        if method == "model/list":
            return {"data": [{"id": "fixture-model"}], "nextCursor": None}
        if method == "thread/start":
            self.thread_number += 1
            return {"thread": {"id": f"thread-{self.thread_number}"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            self.turn_number += 1
            turn_id = f"turn-{self.turn_number}"
            thread_id = params["threadId"]
            self.events.append({"method": "turn/started", "params": {
                "threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"},
            }})
            if self.complete_on_start:
                self.complete_turn(thread_id, turn_id)
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        if method == "turn/steer":
            thread_id = params["threadId"]
            turn_id = params["expectedTurnId"]
            self.complete_turn(thread_id, turn_id)
            if self.reject_steer:
                raise RpcError(
                    "activeTurnNotSteerable: review",
                    code=-32600,
                    error={"code": -32600, "message": "activeTurnNotSteerable: review"},
                )
            return {"turnId": turn_id}
        if method == "turn/interrupt":
            self.events.append({"method": "turn/completed", "params": {
                "threadId": params["threadId"],
                "turn": {"id": params["turnId"], "status": "interrupted", "items": []},
            }})
            return {}
        raise AssertionError(f"Unexpected app-server method: {method}")

    def complete_turn(self, thread_id: str, turn_id: str):
        self.events.append({"method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": []},
        }})

    def next_event(self, timeout=0.1):
        if not self.events:
            return None
        event = self.events.popleft()
        if event.get("method") == "turn/started" and self.on_turn_started:
            self.on_turn_started(event["params"]["threadId"], event["params"]["turn"]["id"])
        return event


class ControlTests(unittest.TestCase):
    def setUp(self):
        self._temporary = TemporaryDirectory()
        self.project = Path(self._temporary.name)
        self.store = Store(self.project)

    def tearDown(self):
        self._temporary.cleanup()

    def test_steer_requires_a_confirmed_active_turn_and_binds_all_ids(self):
        queued = self.store.add_task("Queued work")
        with self.assertRaisesRegex(ValueError, "Only a running task"):
            steer_task(self.store, queued["id"], "Change direction")

        active = self.store.update_task(
            queued["id"], status="waiting_approval", run_id="run-1",
            thread_id="thread-1", turn_id="turn-1",
        )
        before = dict(active)
        control = steer_task(self.store, active["id"], "Use the narrower scope")

        self.assertEqual(
            {key: control[key] for key in ("task_id", "kind", "status", "run_id", "thread_id", "turn_id", "prompt")},
            {
                "task_id": active["id"], "kind": "steer", "status": "pending",
                "run_id": "run-1", "thread_id": "thread-1", "turn_id": "turn-1",
                "prompt": "Use the narrower scope",
            },
        )
        after = self.store.get_task(active["id"])
        self.assertEqual(after["model"], before["model"])
        self.assertEqual(after["current_model"], before["current_model"])
        self.assertEqual(self.store.events(active["id"])[-1]["kind"], "task/steer_requested")

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            steer_task(self.store, active["id"], "  \n")
        self.store.update_task(active["id"], status="interrupting")
        with self.assertRaisesRegex(ValueError, "interruption is pending"):
            steer_task(self.store, active["id"], "Too late")

    def test_dispatch_pause_is_persisted_and_recorded(self):
        paused = set_dispatch(self.store, True)
        self.assertTrue(paused["paused"])
        self.assertTrue(self.store.get_setting("dispatch")["paused"])
        self.assertEqual(self.store.events()[-1]["kind"], "dispatch/paused")

        reopened = Store(self.project)
        resumed = set_dispatch(reopened, False)
        self.assertFalse(resumed["paused"])
        self.assertFalse(reopened.get_setting("dispatch")["paused"])
        self.assertEqual(reopened.events()[-1]["kind"], "dispatch/resumed")
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            set_dispatch(reopened, 1)

    def test_mismatched_steer_acknowledgement_is_uncertain_and_not_retried(self):
        task = self.store.add_task("Work")
        task = self.store.update_task(task["id"], status="running", run_id="run-1",
                                      thread_id="thread-1", turn_id="turn-1")
        control = steer_task(self.store, task["id"], "Correction")
        server = Mock()
        server.request.return_value = {"turnId": "a-different-turn"}
        engine = Engine(self.store, server=server)
        engine.run_id = "run-1"
        engine._process_controls()
        engine._process_controls()
        self.assertEqual(self.store.controls()[0]["status"], "uncertain")
        self.assertEqual(server.request.call_count, 1)
        self.assertNotIn("task/steer_sent", [event["kind"] for event in self.store.events(task["id"])])

    def _run_steer(self, *, reject: bool = False):
        task = self.store.add_task("Keep working until steered")
        server = ControlServer(reject_steer=reject)
        queued = {"done": False}

        def queue_after_turn_start(thread_id: str, turn_id: str) -> None:
            if queued["done"]:
                return
            current = self.store.get_task(task["id"])
            if current.get("turn_id") != turn_id:
                return
            steer_task(self.store, task["id"], "Stop exploring and report the partial findings")
            queued["done"] = True

        server.on_turn_started = queue_after_turn_start
        code = Engine(self.store, server=server, poll_interval=0.001).run(once=True)
        return task, server, code

    def test_engine_sends_turn_steer_with_expected_turn_and_confirms_acceptance(self):
        task, server, code = self._run_steer()

        self.assertEqual(code, 0)
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        control = self.store.controls()[0]
        self.assertEqual(control["status"], "sent")
        request = next(params for method, params in server.calls if method == "turn/steer")
        self.assertEqual(request, {
            "threadId": control["thread_id"],
            "expectedTurnId": control["turn_id"],
            "input": [{"type": "text", "text": control["prompt"]}],
        })
        self.assertEqual(control["response_turn_id"], control["turn_id"])
        self.assertTrue(any(event["kind"] == "task/steer_sent" for event in self.store.events(task["id"])))

    def test_engine_reports_codex_rejection_without_fallback_or_policy_change(self):
        task, server, code = self._run_steer(reject=True)

        self.assertEqual(code, 0)
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        control = self.store.controls()[0]
        self.assertEqual(control["status"], "rejected")
        self.assertIn("activeTurnNotSteerable", control["error"])
        self.assertEqual(sum(method == "turn/steer" for method, _ in server.calls), 1)
        self.assertFalse(any(method == "turn/start" and params.get("input", [{}])[0].get("type") == "steer"
                             for method, params in server.calls))
        self.assertIsNone(self.store.get_task(task["id"])["model"])

    def test_stale_turn_control_expires_without_a_rpc(self):
        task = self.store.add_task("Already advanced")
        self.store.update_task(task["id"], status="running", run_id="run-current",
                               thread_id="thread-current", turn_id="turn-current")
        control = steer_task(self.store, task["id"], "Old turn instruction")
        self.store.update_control(control["id"], turn_id="turn-old")
        server = ControlServer()
        engine = Engine(self.store, server=server)
        engine.run_id = "run-current"

        engine._process_controls()

        self.assertEqual(self.store.controls()[0]["status"], "expired")
        self.assertFalse(any(method == "turn/steer" for method, _ in server.calls))
        self.assertTrue(any(event["kind"] == "control/expired" for event in self.store.events(task["id"])))

    def test_interrupt_queued_before_steer_expires_the_later_steer(self):
        task = self.store.add_task("Interrupt before steering")
        self.store.update_task(task["id"], status="running", run_id="run-current",
                               thread_id="thread-current", turn_id="turn-current")
        self.store.interrupt_task(task["id"])
        steer = steer_task(self.store, task["id"], "This should not race the interrupt")
        server = ControlServer()
        engine = Engine(self.store, server=server)
        engine.run_id = "run-current"

        engine._process_controls()

        statuses = {control["kind"]: control["status"] for control in self.store.controls()}
        self.assertEqual(statuses["interrupt"], "sent")
        self.assertEqual(next(control for control in self.store.controls() if control["id"] == steer["id"])["status"], "expired")
        self.assertTrue(any(method == "turn/interrupt" for method, _ in server.calls))
        self.assertFalse(any(method == "turn/steer" for method, _ in server.calls))

    def test_runner_restart_expires_pending_and_never_replays_sending_steer(self):
        task = self.store.add_task("Recover without replay")
        self.store.update_task(task["id"], status="running", run_id="old-run",
                               thread_id="thread-old", turn_id="turn-old")
        pending = steer_task(self.store, task["id"], "Pending before restart")
        sending = steer_task(self.store, task["id"], "Possibly accepted before restart")
        self.store.update_control(sending["id"], status="sending")
        server = ControlServer()

        code = Engine(self.store, server=server, poll_interval=0.001).run(once=True)

        self.assertEqual(code, 1)
        statuses = {control["id"]: control["status"] for control in self.store.controls()}
        self.assertEqual(statuses[pending["id"]], "expired")
        self.assertEqual(statuses[sending["id"]], "uncertain")
        self.assertFalse(any(method == "turn/steer" for method, _ in server.calls))

    def test_pause_allows_active_turn_to_finish_and_once_returns_with_queue_pending(self):
        active = self.store.add_task("Finish the current turn")
        queued = self.store.add_task("Wait while dispatch is paused")
        server = ControlServer()

        def pause_after_turn_start(thread_id: str, turn_id: str) -> None:
            set_dispatch(self.store, True)
            server.complete_turn(thread_id, turn_id)

        server.on_turn_started = pause_after_turn_start
        code = Engine(self.store, server=server, max_workers=1, poll_interval=0.001).run(once=True)

        self.assertEqual(code, 1)
        self.assertEqual(self.store.get_task(active["id"])["status"], "completed")
        self.assertEqual(self.store.get_task(queued["id"])["status"], "queued")
        self.assertEqual(sum(method == "turn/start" for method, _ in server.calls), 1)

        set_dispatch(self.store, False)
        resumed_server = ControlServer(complete_on_start=True)
        resumed_code = Engine(self.store, server=resumed_server, max_workers=1, poll_interval=0.001).run(once=True)
        self.assertEqual(resumed_code, 0)
        self.assertEqual(self.store.get_task(queued["id"])["status"], "completed")
        self.assertTrue(any(method == "turn/start" for method, _ in resumed_server.calls))


if __name__ == "__main__":
    unittest.main()
