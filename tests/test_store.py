from __future__ import annotations

import concurrent.futures
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from codex_orchestrator.engine import Engine
from codex_orchestrator.store import RunnerLock, Store


ROOT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.project = Path(self._temporary.name)
        self.store = Store(self.project)

    def tearDown(self):
        self._temporary.cleanup()

    def test_tasks_and_dependencies_persist_and_reject_unknown_references_atomically(self):
        first = self.store.add_task("Inspect files", role="explorer")
        second = self.store.add_task(
            "Implement the change", role="worker", depends_on=[first["id"], first["id"]],
            parent_id=first["id"],
        )
        before_tasks = self.store.tasks()
        before_events = self.store.events()

        with self.assertRaisesRegex(ValueError, "Unknown task"):
            self.store.add_task("Should not persist", depends_on=["missing-task"])
        with self.assertRaisesRegex(ValueError, "Unknown task"):
            self.store.add_task("Missing parent", parent_id="missing-parent")

        reopened = Store(self.project)
        self.assertEqual(reopened.tasks(), before_tasks)
        self.assertEqual(reopened.events(), before_events)
        stored = reopened.get_task(second["id"])
        self.assertEqual(stored["depends_on"], [first["id"]])
        self.assertEqual(stored["parent_id"], first["id"])

    def test_event_cursor_traverses_bursts_larger_than_page_limit_without_skipping(self):
        self.store.event(None, "cursor/start", {})
        cursor = self.store.events(limit=1)[0]["id"]
        for index in range(7):
            self.store.event(None, "cursor/burst", {"index": index})

        seen = []
        while True:
            page = self.store.events(after=cursor, limit=2)
            if not page:
                break
            seen.extend(page)
            cursor = page[-1]["id"]

        self.assertEqual([event["data"]["index"] for event in seen], list(range(7)))
        self.assertEqual([event["id"] for event in seen], sorted(event["id"] for event in seen))

    def test_failed_role_and_live_model_mutations_leave_state_and_events_unchanged(self):
        task = self.store.add_task("Queued task", model="initial-model")
        role_before = self.store.get_role("worker")
        task_before = self.store.get_task(task["id"])
        events_before = self.store.events()

        with self.assertRaisesRegex(ValueError, "nonempty model ID"):
            self.store.set_role("worker", model="bad model")
        with self.assertRaisesRegex(ValueError, "active turn"):
            self.store.set_task_model(task["id"], "should-not-stick", effort="high", live=True)

        self.assertEqual(self.store.get_role("worker"), role_before)
        self.assertEqual(self.store.get_task(task["id"]), task_before)
        self.assertEqual(self.store.controls(), [])
        self.assertEqual(self.store.events(), events_before)

    def test_role_defaults_and_per_task_overrides_resolve_with_task_precedence(self):
        default_task = self.store.add_task("Uses current role policy")
        override_task = self.store.add_task(
            "Keeps explicit policy", model="task-model", effort="low"
        )
        self.assertEqual(self.store.task_policy(default_task)["model"], "gpt-6-luna")
        self.assertEqual(self.store.task_policy(default_task)["effort"], "max")

        self.store.set_role("worker", model="role-model-v2", effort="xhigh")
        self.assertEqual(self.store.task_policy(self.store.get_task(default_task["id"]))["model"], "role-model-v2")
        self.assertEqual(self.store.task_policy(self.store.get_task(default_task["id"]))["effort"], "xhigh")
        policy = self.store.task_policy(self.store.get_task(override_task["id"]))
        self.assertEqual(policy["model"], "task-model")
        self.assertEqual(policy["effort"], "low")
        snapshot = {task["id"]: task for task in self.store.snapshot()["tasks"]}
        self.assertEqual(snapshot[default_task["id"]]["next_model"], "role-model-v2")
        self.assertEqual(snapshot[override_task["id"]]["next_effort"], "low")

    def test_live_model_control_is_bound_to_the_active_run_and_turn(self):
        task = self.store.add_task("Active task")
        self.store.update_task(
            task["id"], status="running", run_id="run-42", turn_id="turn-9",
            current_model="gpt-6-luna", current_effort="max",
        )
        changed = self.store.set_task_model(task["id"], "gpt-6.1-sol", effort="high", live=True)
        controls = self.store.controls()

        self.assertEqual(changed["live_update_status"], "pending")
        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertEqual(
            {key: control[key] for key in ("task_id", "kind", "status", "run_id", "turn_id", "model", "effort")},
            {
                "task_id": task["id"], "kind": "model", "status": "pending",
                "run_id": "run-42", "turn_id": "turn-9", "model": "gpt-6.1-sol", "effort": "high",
            },
        )

    def test_continue_only_requeues_terminal_tasks_and_preserves_thread(self):
        queued = self.store.add_task("Queued task")
        with self.assertRaisesRegex(ValueError, "Only a terminal task"):
            self.store.continue_task(queued["id"], "Continue too early")
        self.assertEqual(self.store.get_task(queued["id"])["status"], "queued")

        active = self.store.add_task("Running task")
        self.store.update_task(active["id"], status="running", thread_id="thread-1", turn_id="turn-1")
        with self.assertRaisesRegex(ValueError, "Only a terminal task"):
            self.store.continue_task(active["id"], "Do not overlap")

        self.store.update_task(active["id"], status="completed", result="First result")
        continued = self.store.continue_task(active["id"], "Second turn instructions")
        self.assertEqual(continued["status"], "queued")
        self.assertEqual(continued["prompt"], "Second turn instructions")
        self.assertEqual(continued["thread_id"], "thread-1")
        self.assertIsNone(continued["turn_id"])
        self.assertEqual(continued["result"], "")

    def test_claim_rechecks_cancellation_and_dependency_state_atomically(self):
        cancelled = self.store.add_task("Cancel before claim")
        self.store.interrupt_task(cancelled["id"])
        self.assertIsNone(self.store.claim_task(cancelled["id"], "run-1", self.store.task_policy(cancelled)))
        self.assertEqual(self.store.get_task(cancelled["id"])["status"], "cancelled")

        prerequisite = self.store.add_task("Prerequisite")
        dependent = self.store.add_task("Wait for prerequisite", depends_on=[prerequisite["id"]])
        policy = self.store.task_policy(dependent)
        self.assertIsNone(self.store.claim_task(dependent["id"], "run-1", policy))
        self.assertEqual(self.store.get_task(dependent["id"])["status"], "queued")
        self.store.update_task(prerequisite["id"], status="completed")

        claimed = self.store.claim_task(dependent["id"], "run-1", policy)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["current_sandbox"], policy["sandbox"])
        self.assertEqual(claimed["run_id"], "run-1")

    def test_simultaneous_claims_allow_only_one_scheduler_winner(self):
        task = self.store.add_task("Single claim")
        policy = self.store.task_policy(task)
        barrier = threading.Barrier(3)

        def claim(run_id):
            barrier.wait()
            return self.store.claim_task(task["id"], run_id, policy)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(claim, "run-a")
            second = executor.submit(claim, "run-b")
            barrier.wait()
            results = [first.result(timeout=5), second.result(timeout=5)]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0]["run_id"], {"run-a", "run-b"})
        self.assertEqual(winners[0]["current_sandbox"], policy["sandbox"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "running")

    def test_approval_decisions_questions_and_expired_requests_are_validated(self):
        command = self.store.add_request(
            "rpc-1", "item/commandExecution/requestApproval", {"command": ["git", "status"]},
            "run-1",
        )
        with self.assertRaisesRegex(ValueError, "Choose accept, decline, or cancel"):
            self.store.respond(command["id"], decision="acceptForSession")
        self.assertEqual(self.store.requests("pending")[0]["id"], command["id"])
        answered = self.store.respond(command["id"], decision="decline")
        self.assertEqual(answered["response"], {"decision": "decline"})
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.respond(command["id"], decision="accept")

        question = self.store.add_request(
            "rpc-2", "item/tool/requestUserInput",
            {"questions": [{"id": "q1"}, {"id": "q2"}]}, "run-1",
        )
        with self.assertRaisesRegex(ValueError, "each question ID exactly once"):
            self.store.respond(question["id"], answers={"q1": {"answers": ["one"]}})
        with self.assertRaisesRegex(ValueError, "shape"):
            self.store.respond(question["id"], answers={"q1": {"answers": []}, "q2": {"answers": ["two"]}})
        answered_question = self.store.respond(
            question["id"], answers={"q1": {"answers": ["one"]}, "q2": {"answers": ["two"]}}
        )
        self.assertEqual(answered_question["status"], "answered")

        expired = self.store.add_request(
            "rpc-3", "item/fileChange/requestApproval", {}, "run-1"
        )
        self.store.update_request(expired["id"], status="expired")
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.respond(expired["id"], decision="accept")

    def test_restart_expires_old_pending_approval_before_it_can_be_answered(self):
        request = self.store.add_request(
            "rpc-old", "item/commandExecution/requestApproval", {}, "previous-run"
        )
        engine = Engine(self.store)
        engine.run_id = "new-run"
        engine._expire_old_requests()
        expired = self.store.requests()[0]
        self.assertEqual(expired["id"], request["id"])
        self.assertEqual(expired["status"], "expired")
        self.assertIn("expired_at", expired)
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.respond(request["id"], decision="accept")

    def test_concurrent_policy_and_task_model_updates_do_not_mix_fields(self):
        barrier = threading.Barrier(4)

        def update_role(model, effort, instructions):
            barrier.wait()
            return self.store.set_role(
                "worker", model=model, effort=effort, instructions=instructions
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(update_role, "role-a", "high", "instructions-a"),
                executor.submit(update_role, "role-b", "low", "instructions-b"),
                executor.submit(update_role, "role-c", "medium", "instructions-c"),
            ]
            barrier.wait()
            for future in futures:
                future.result(timeout=5)
        role = self.store.get_role("worker")
        self.assertIn((role["model"], role["effort"], role["instructions"]), {
            ("role-a", "high", "instructions-a"),
            ("role-b", "low", "instructions-b"),
            ("role-c", "medium", "instructions-c"),
        })

        task = self.store.add_task("Concurrent model update")
        barrier = threading.Barrier(3)

        def update_task(model, effort):
            barrier.wait()
            return self.store.set_task_model(task["id"], model, effort)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(update_task, "task-model-a", "high"),
                executor.submit(update_task, "task-model-b", "low"),
            ]
            barrier.wait()
            for future in futures:
                future.result(timeout=5)
        updated = self.store.get_task(task["id"])
        self.assertIn((updated["model"], updated["effort"]), {
            ("task-model-a", "high"), ("task-model-b", "low")
        })

    def test_runner_lock_rejects_a_second_process_and_releases_for_next_owner(self):
        code = (
            "import sys; from codex_orchestrator.store import RunnerLock; "
            "lock=RunnerLock(sys.argv[1]); "
            "lock.__enter__(); print('acquired', flush=True)"
        )
        with RunnerLock(self.store.directory):
            second = subprocess.run(
                [sys.executable, "-c", code, str(self.store.directory)],
                cwd=ROOT, capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertNotIn("acquired", second.stdout)

        released = subprocess.run(
            [sys.executable, "-c", code, str(self.store.directory)],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertIn("acquired", released.stdout)


if __name__ == "__main__":
    unittest.main()
