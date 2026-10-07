from __future__ import annotations

import threading
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_orchestrator.engine import Engine
from codex_orchestrator.rpc import RpcError
from codex_orchestrator.store import Store


class FakeServer:
    def __init__(self, store=None, auto_complete=True, live_status="applied", live_error=None,
                 dispatch_error=None, missing_turn_id=False):
        self.store = store
        self.auto_complete = auto_complete
        self.live_status = live_status
        self.live_error = live_error
        self.dispatch_error = dispatch_error
        self.missing_turn_id = missing_turn_id
        self.events = deque()
        self.calls = []
        self.responses = []
        self.alive = False
        self.thread_number = 0
        self.turn_number = 0
        self.active_turns = 0
        self.max_active_turns = 0
        self.current_ids = {}
        self.on_turn_started = None
        self.on_idle = None
        self._idle_called = False

    def start(self):
        self.alive = True
        return self

    def request(self, method, params=None, timeout=None):
        params = params or {}
        self.calls.append((method, dict(params)))
        if method == "model/list":
            return {"data": [{"id": "gpt-6-luna", "model": "gpt-6-luna"}], "nextCursor": None}
        if method == "thread/start":
            self.thread_number += 1
            thread_id = f"thread-{self.thread_number}"
            self.current_ids[thread_id] = None
            self.events.append({
                "method": "thread/started",
                "params": {"thread": {"id": thread_id, "sessionId": "session", "parentThreadId": None,
                                       "agentRole": None, "agentNickname": None, "status": {"type": "idle"},
                                       "model": params.get("model"), "reasoningEffort": None}},
            })
            return {"thread": {"id": thread_id}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            self.turn_number += 1
            turn_id = f"turn-{self.turn_number}"
            thread_id = params["threadId"]
            self.current_ids[thread_id] = turn_id
            self.active_turns += 1
            self.max_active_turns = max(self.max_active_turns, self.active_turns)
            self.events.append({
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress", "items": []}},
            })
            if self.on_turn_started:
                self.on_turn_started(thread_id, turn_id)
            if self.auto_complete:
                self.complete_turn(thread_id, turn_id, f"result-{self.turn_number}")
            if self.dispatch_error:
                raise self.dispatch_error
            if self.missing_turn_id:
                return {"turn": {"status": "inProgress"}}
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        if method == "turn/settings/update":
            if self.live_error:
                raise self.live_error
            if self.auto_complete or (self.live_status == "applied" and self.live_error is None):
                self.complete_turn(params["threadId"], params["turnId"], "result-after-live-update")
            return {"status": self.live_status}
        if method == "turn/interrupt":
            self.events.append({
                "method": "turn/completed",
                "params": {"threadId": params["threadId"],
                           "turn": {"id": params["turnId"], "status": "interrupted", "items": []}},
            })
            return {}
        raise AssertionError(f"Unexpected RPC method: {method}")

    def complete_turn(self, thread_id, turn_id, result):
        self.events.append({
            "method": "item/completed",
            "params": {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 1,
                       "item": {"id": f"message-{turn_id}", "type": "agentMessage", "phase": "final_answer", "text": result}},
        })
        self.events.append({
            "method": "turn/completed",
            "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": []}},
        })

    def next_event(self, timeout=0.1):
        if self.events:
            event = self.events.popleft()
            if event.get("method") == "turn/started" and self.on_turn_started:
                self.on_turn_started(event["params"]["threadId"], event["params"]["turn"]["id"])
            if event.get("method") == "turn/completed":
                self.active_turns = max(0, self.active_turns - 1)
            return event
        if self.on_idle:
            self.on_idle()
        return None

    def respond(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))
        if result is not None:
            params = self._find_turn_params(request_id)
            if params:
                self.complete_turn(params["threadId"], params["turnId"], "approved result")

    def _find_turn_params(self, request_id):
        for request in self.store.requests() if self.store else []:
            if request.get("rpc_id") == request_id:
                return request.get("params")
        return None

    def close(self):
        self.alive = False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.store = Store(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, server, **kwargs):
        return Engine(self.store, server=server, poll_interval=0.001, **kwargs)

    def test_dependencies_wait_for_explicit_completion_and_receive_result(self):
        first = self.store.add_task("Inspect the endpoint", role="explorer")
        second = self.store.add_task("Implement the endpoint", role="worker", depends_on=[first["id"]])
        server = FakeServer(self.store)

        result = self.engine(server, max_workers=2).run(once=True)

        self.assertEqual(result, 0)
        first_after = self.store.get_task(first["id"])
        second_after = self.store.get_task(second["id"])
        self.assertEqual(first_after["status"], "completed")
        self.assertEqual(first_after["result"], "result-1")
        self.assertEqual(second_after["status"], "completed")
        turns = [params for method, params in server.calls if method == "turn/start"]
        self.assertEqual(len(turns), 2)
        self.assertIn("Dependency " + first["id"], turns[1]["input"][0]["text"])
        self.assertIn("result-1", turns[1]["input"][0]["text"])
        self.assertEqual(server.max_active_turns, 1)

    def test_read_only_tasks_parallel_but_writers_are_exclusive(self):
        readers = [self.store.add_task(f"Inspect {index}", role="explorer") for index in range(2)]
        read_server = FakeServer(self.store)
        self.assertEqual(self.engine(read_server, max_workers=2).run(once=True), 0)
        self.assertEqual(read_server.max_active_turns, 2)

        self.store = Store(self.project)
        writer_a = self.store.add_task("Write A", role="worker")
        writer_b = self.store.add_task("Write B", role="worker")
        reader = self.store.add_task("Inspect after writes", role="explorer")
        write_server = FakeServer(self.store)
        self.assertEqual(self.engine(write_server, max_workers=3).run(once=True), 0)
        self.assertEqual(write_server.max_active_turns, 1)
        self.assertEqual([self.store.get_task(task["id"])["status"] for task in (writer_a, writer_b, reader)],
                         ["completed", "completed", "completed"])

    def test_restart_marks_uncertain_active_work_lost_and_expires_old_requests(self):
        task = self.store.add_task("Do not replay this", role="worker")
        self.store.update_task(task["id"], status="waiting_approval", run_id="previous-run",
                               thread_id="old-thread", turn_id="old-turn")
        request = self.store.add_request(41, "item/commandExecution/requestApproval",
                                         {"threadId": "old-thread", "turnId": "old-turn"},
                                         "previous-run", task["id"])
        server = FakeServer(self.store)

        result = self.engine(server).run(once=True)

        self.assertEqual(result, 1)
        self.assertEqual(self.store.get_task(task["id"])["status"], "lost")
        self.assertEqual(self.store.requests()[0]["status"], "expired")
        self.assertFalse(any(method in {"thread/start", "thread/resume", "turn/start"} for method, _ in server.calls))
        events = self.store.events(task["id"])
        self.assertTrue(any(event["kind"] == "task/lost" for event in events))
        self.assertEqual(request["status"], "pending")

    def test_approval_waits_for_explicit_store_response(self):
        task = self.store.add_task("Run a command", role="worker")
        server = FakeServer(self.store, auto_complete=False)
        server.store = self.store
        observed_pending = []

        def on_turn_started(thread_id, turn_id):
            if any(event.get("method") == "item/commandExecution/requestApproval" for event in server.events):
                return
            server.events.append({
                "id": 41,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": thread_id, "turnId": turn_id, "command": "git status"},
            })

        def on_idle():
            pending = self.store.requests("pending")
            if pending:
                observed_pending.append((pending[0]["status"], list(server.responses)))
                self.store.respond(pending[0]["id"], decision="accept")
                server._idle_called = True

        server.on_turn_started = on_turn_started
        server.on_idle = on_idle

        result = self.engine(server).run(once=True)

        self.assertEqual(result, 0)
        self.assertEqual(observed_pending, [("pending", [])])
        self.assertEqual(server.responses, [(41, {"decision": "accept"}, None)])
        self.assertEqual(self.store.requests()[0]["status"], "delivered")
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")

    def _queue_live_update(self, task_id, model="gpt-6-astra", effort="high"):
        task = self.store.get_task(task_id)
        self.store.set_task_model(task_id, model, effort, live=True)
        return task

    def _run_live_update(self, status="applied", error=None):
        task = self.store.add_task("Work while model changes", role="worker")
        server = FakeServer(self.store, auto_complete=False, live_status=status, live_error=error)
        generated = {"value": False}

        def update_once(thread_id, turn_id):
            if generated["value"]:
                return
            current = self.store.get_task(task["id"])
            if current.get("turn_id") != turn_id:
                return
            self._queue_live_update(task["id"])
            generated["value"] = True
            if status != "applied" or error is not None:
                # The task still reaches an explicit terminal turn after the
                # requested setting is reported as unavailable/unsupported.
                server.complete_turn(thread_id, turn_id, "final result")

        server.on_turn_started = update_once
        return task, server, Engine(self.store, server=server, poll_interval=0.001,
                                    enable_live_models=True).run(once=True)

    def test_live_model_publication_is_not_misreported_as_observed(self):
        task, server, result = self._run_live_update("applied")

        self.assertEqual(result, 0)
        task_after = self.store.get_task(task["id"])
        self.assertEqual(task_after["current_model"], "gpt-6-luna")
        self.assertEqual(task_after["live_model"], "gpt-6-astra")
        self.assertEqual(task_after["live_update_status"], "applied")
        self.assertIsNone(task_after.get("observed_model"))
        update = next(params for method, params in server.calls if method == "turn/settings/update")
        self.assertEqual(update["model"], "gpt-6-astra")
        published = [event for event in self.store.events(task["id"]) if event["kind"] == "model/live_published"]
        self.assertEqual(published[0]["data"]["observed"], False)

    def test_live_model_target_unavailable_keeps_next_turn_policy_pending(self):
        task, server, result = self._run_live_update("targetUnavailable")

        self.assertEqual(result, 0)
        task_after = self.store.get_task(task["id"])
        self.assertEqual(task_after["current_model"], "gpt-6-luna")
        self.assertEqual(task_after["model"], "gpt-6-astra")
        self.assertEqual(task_after["live_update_status"], "targetUnavailable")
        self.assertIsNone(task_after.get("live_model"))

    def test_live_model_method_not_found_falls_back_to_pending_next_turn(self):
        task, server, result = self._run_live_update(
            "applied", RpcError("method not found", code=-32601, error={"code": -32601, "message": "method not found"})
        )

        self.assertEqual(result, 0)
        task_after = self.store.get_task(task["id"])
        self.assertEqual(task_after["current_model"], "gpt-6-luna")
        self.assertEqual(task_after["model"], "gpt-6-astra")
        self.assertEqual(task_after["live_update_status"], "unsupported")
        self.assertIsNone(task_after.get("observed_model"))
        self.assertTrue(any(method == "turn/settings/update" for method, _ in server.calls))

    def test_explicit_reroute_is_recorded_as_observed_model(self):
        task = self.store.add_task("Handle model fallback", role="worker")
        server = FakeServer(self.store, auto_complete=False)

        def reroute(thread_id, turn_id):
            if self.store.get_task(task["id"]).get("turn_id") == turn_id and not any(m == "reroute-sent" for m, _ in server.calls):
                server.calls.append(("reroute-sent", {}))
                server.events.append({"method": "model/rerouted", "params": {"threadId": thread_id, "turnId": turn_id,
                                                                                "fromModel": "gpt-6-luna", "toModel": "gpt-6-astra",
                                                                                "reason": "test"}})
                server.complete_turn(thread_id, turn_id, "done")

        server.on_turn_started = reroute
        self.assertEqual(self.engine(server).run(once=True), 0)
        task_after = self.store.get_task(task["id"])
        self.assertEqual(task_after["current_model"], "gpt-6-luna")
        self.assertEqual(task_after["observed_model"], "gpt-6-astra")

    def test_runner_stop_interrupts_and_does_not_mark_completion_without_turn_event(self):
        task = self.store.add_task("Stop this task", role="worker")
        stop_event = threading.Event()
        server = FakeServer(self.store, auto_complete=False)
        server.on_idle = stop_event.set

        result = self.engine(server).run(stop_event=stop_event)

        self.assertEqual(result, 1)
        self.assertTrue(any(method == "turn/interrupt" for method, _ in server.calls))
        self.assertEqual(self.store.get_task(task["id"])["status"], "interrupted")

    def test_blocked_dependency_unblocks_after_explicit_continuation(self):
        dependency = self.store.add_task("Recover dependency", role="explorer")
        dependent = self.store.add_task("Run after dependency", role="worker", depends_on=[dependency["id"]])
        self.store.update_task(dependency["id"], status="failed", error="first attempt failed")

        first = FakeServer(self.store)
        self.assertEqual(self.engine(first).run(once=True), 1)
        self.assertEqual(self.store.get_task(dependent["id"])["status"], "blocked")

        self.store.continue_task(dependency["id"], "Retry the dependency")
        second = FakeServer(self.store)
        self.assertEqual(self.engine(second).run(once=True), 0)
        self.assertEqual(self.store.get_task(dependency["id"])["status"], "completed")
        self.assertEqual(self.store.get_task(dependent["id"])["status"], "completed")
        self.assertTrue(any(event["kind"] == "task/unblocked" for event in self.store.events(dependent["id"])))

    def test_ambiguous_turn_dispatch_quarantines_runner_before_second_writer(self):
        first = self.store.add_task("Write first", role="worker")
        second = self.store.add_task("Write second", role="worker")
        server = FakeServer(self.store, dispatch_error=RpcError("request timed out"))

        result = self.engine(server, max_workers=2).run(once=True)

        self.assertEqual(result, 1)
        self.assertEqual(self.store.get_task(first["id"])["status"], "lost")
        self.assertEqual(self.store.get_task(second["id"])["status"], "queued")
        self.assertEqual(sum(method == "turn/start" for method, _ in server.calls), 1)
        self.assertFalse(server.alive)

    def test_delayed_previous_turn_completion_cannot_finish_continuation(self):
        task = self.store.add_task("Retry this task", role="worker")
        first_server = FakeServer(self.store)
        self.assertEqual(self.engine(first_server).run(once=True), 0)
        previous_turn = self.store.get_task(task["id"])["turn_id"]
        self.store.continue_task(task["id"], "Continue after explicit failure review")

        server = FakeServer(self.store)
        server.turn_number = 1
        original_request = server.request

        def inject_old_completion(method, params=None, timeout=None):
            params = params or {}
            if method == "thread/resume":
                server.events.append({"method": "turn/completed", "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": previous_turn, "status": "completed", "items": []},
                }})
            return original_request(method, params, timeout)

        server.request = inject_old_completion
        self.assertEqual(self.engine(server).run(once=True), 0)
        after = self.store.get_task(task["id"])
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["turn_id"], "turn-2")
        self.assertEqual(after["result"], "result-2")

    def test_native_child_approval_usage_output_and_writer_lease(self):
        parent = self.store.add_task("Coordinate a native agent", role="worker")
        queued = self.store.add_task("Write after child exits", role="worker")
        self.store.update_task(parent["id"], status="running", run_id="run-native", thread_id="root-thread",
                               turn_id="root-turn", current_sandbox="workspace-write")
        server = FakeServer(self.store)
        engine = Engine(self.store, server=server, poll_interval=0.001)
        engine.run_id = "run-native"
        engine._thread_to_task["root-thread"] = parent["id"]
        engine._thread_started({"id": "child-thread", "parentThreadId": "root-thread",
                                "agentRole": "explorer", "agentNickname": "Scout",
                                "status": {"type": "idle"}})
        engine._turn_started({"threadId": "child-thread", "turn": {"id": "child-turn", "status": "inProgress"}})
        engine._item_completed({"threadId": "child-thread", "turnId": "child-turn", "item": {
            "id": "child-answer", "type": "agentMessage", "phase": "final_answer", "text": "Child result"}})
        engine._token_usage_updated({"threadId": "child-thread", "turnId": "child-turn",
                                     "tokenUsage": {"last": {"inputTokens": 4}, "total": {"inputTokens": 9}}})

        child = next(agent for agent in self.store.agents() if agent["id"] == "child-thread")
        self.assertEqual(child["output_summary"], "Child result")
        self.assertEqual(child["usage"]["total"]["inputTokens"], 9)
        self.assertEqual(self.store.get_task(parent["id"])["result"], "")

        engine._handle_server_request({"id": 71, "method": "item/commandExecution/requestApproval",
                                       "params": {"threadId": "child-thread", "turnId": "child-turn", "command": "git status"}})
        request = self.store.requests()[0]
        self.assertEqual(request["task_id"], parent["id"])
        self.assertEqual(self.store.get_task(parent["id"])["status"], "waiting_approval")
        self.store.respond(request["id"], decision="accept")
        engine._deliver_answered_requests()
        self.assertEqual(server.responses, [(71, {"decision": "accept"}, None)])
        engine._handle_event({"method": "serverRequest/resolved", "params": {"requestId": 71, "threadId": "child-thread"}})
        self.assertEqual(self.store.requests()[0]["status"], "resolved")
        self.assertEqual(self.store.get_task(parent["id"])["status"], "running")
        server.events.clear()

        engine._turn_completed({"threadId": "root-thread", "turn": {"id": "root-turn", "status": "completed"}})
        self.assertTrue(any(method == "turn/interrupt" and params.get("threadId") == "child-thread"
                            for method, params in server.calls))
        starts_before_child_exit = sum(method == "turn/start" for method, _ in server.calls)
        engine._schedule_ready_tasks()
        self.assertEqual(sum(method == "turn/start" for method, _ in server.calls), starts_before_child_exit)

        interrupt_event = server.next_event(timeout=0)
        self.assertEqual(interrupt_event["method"], "turn/completed")
        engine._handle_event(interrupt_event)
        engine._schedule_ready_tasks()
        self.assertEqual(sum(method == "turn/start" for method, _ in server.calls), starts_before_child_exit + 1)
        self.assertIn(self.store.get_task(queued["id"])["status"], {"running", "completed"})

    def test_server_request_resolved_clears_waiting_status_without_delivery(self):
        task = self.store.add_task("Wait on approval", role="worker")
        self.store.update_task(task["id"], status="waiting_approval", run_id="run-resolve",
                               thread_id="thread-resolve", turn_id="turn-resolve")
        server = FakeServer(self.store)
        engine = Engine(self.store, server=server)
        engine.run_id = "run-resolve"
        engine._thread_to_task["thread-resolve"] = task["id"]
        request = self.store.add_request(99, "item/commandExecution/requestApproval",
                                         {"threadId": "thread-resolve", "turnId": "turn-resolve"},
                                         "run-resolve", task["id"])

        engine._handle_event({"method": "serverRequest/resolved", "params": {
            "requestId": 99, "threadId": "thread-resolve"}})

        self.assertEqual(self.store.requests()[0]["status"], "resolved")
        self.assertEqual(self.store.get_task(task["id"])["status"], "running")

    def test_live_model_feature_disabled_gate_is_reported_as_unsupported(self):
        task, server, result = self._run_live_update(
            "applied", RpcError("turn settings updates require the step_model_switching feature", code=-32600)
        )

        self.assertEqual(result, 0)
        after = self.store.get_task(task["id"])
        self.assertEqual(after["live_update_status"], "unsupported")
        self.assertIn("--live-models", after["live_update_error"])

    def test_live_model_gate_is_opt_in_per_runner(self):
        task = self.store.add_task("Request live model while disabled", role="worker")
        server = FakeServer(self.store, auto_complete=False)
        queued = {"value": False}

        def on_turn_started(thread_id, turn_id):
            if queued["value"] or self.store.get_task(task["id"]).get("turn_id") != turn_id:
                return
            self._queue_live_update(task["id"])
            queued["value"] = True
            server.complete_turn(thread_id, turn_id, "done")

        server.on_turn_started = on_turn_started
        result = Engine(self.store, server=server, poll_interval=0.001).run(once=True)
        self.assertEqual(result, 0)
        after = self.store.get_task(task["id"])
        self.assertEqual(after["live_update_status"], "unsupported")
        self.assertFalse(any(method == "turn/settings/update" for method, _ in server.calls))


if __name__ == "__main__":
    unittest.main()
