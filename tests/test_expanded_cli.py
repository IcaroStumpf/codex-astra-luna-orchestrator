from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_orchestrator.controls import set_dispatch
from codex_orchestrator.store import Store
from codex_orchestrator.workflows import validate_definition

from test_runtime_integration import FAKE_APP_SERVER, ROOT, runner_command, trace_entries


LIVE_RUNNER_HARNESS = r"""
import sys
import threading
from codex_orchestrator.engine import Engine
from codex_orchestrator.rpc import AppServer
from codex_orchestrator.store import Store

project, fixture, trace = sys.argv[1:]
server = AppServer(
    command=[sys.executable, fixture, "--orchestration", "--trace", trace, "--hold-for-steer"],
    cwd=project,
    request_timeout=3,
)
engine = Engine(Store(project), server=server, max_workers=2, poll_interval=0.02, shutdown_grace=0.2)
stop_event = threading.Event()

def wait_for_stop():
    sys.stdin.readline()
    stop_event.set()

threading.Thread(target=wait_for_stop, daemon=True).start()
status = engine.run(stop_event=stop_event)
if engine.last_error:
    print(engine.last_error, file=sys.stderr)
raise SystemExit(status)
"""


def mcp_message(request_id: int, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def run_mcp(project: Path, messages: list[dict]) -> tuple[list[dict], str]:
    wire = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
    result = subprocess.run(
        [sys.executable, "-m", "codex_orchestrator", "--project", str(project), "mcp"],
        cwd=ROOT,
        input=wire,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"MCP process failed ({result.returncode}): {result.stderr}\n{result.stdout}")
    return [json.loads(line) for line in result.stdout.splitlines()], result.stderr


class ExpandedCliTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.project = Path(self._temporary.name) / "project"
        self.project.mkdir()
        self.assert_cli("init")
        self.store = Store(self.project)

    def tearDown(self):
        self._temporary.cleanup()

    def cli(self, *arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "codex_orchestrator", "--project", str(self.project), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def assert_cli(self, *arguments: str):
        result = self.cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def wait_until(self, predicate, runner: subprocess.Popen[str] | None = None, timeout: float = 8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if runner is not None and runner.poll() is not None:
                stdout, stderr = runner.communicate(timeout=2)
                self.fail(f"runner exited early ({runner.returncode}): {stderr}\n{stdout}")
            time.sleep(0.04)
        self.fail("timed out waiting for durable project state")

    def submit_review_workflow(self, goal: str):
        definition = validate_definition({
            "version": 1,
            "name": "wait-fixture",
            "tasks": [
                {"key": "first", "title": "First step", "role": "worker", "prompt": "First: {goal}"},
                {"key": "second", "title": "Second step", "role": "tester", "prompt": "Second: {goal}",
                 "depends_on": ["first"]},
            ],
        })
        return self.store.submit_workflow(definition, goal)

    def test_workflow_templates_dry_run_submit_list_and_show_keep_preview_queued_free(self):
        templates = self.assert_cli("workflow", "templates")
        self.assertEqual({template["name"] for template in json.loads(templates.stdout)},
                         {"feature", "bugfix", "review"})

        goal = "Add a bounded project search page"
        preview = self.assert_cli("workflow", "submit", "feature", goal, "--name", "Preview only", "--dry-run")
        preview_data = json.loads(preview.stdout)
        self.assertEqual(preview_data["dispatch"], "preview only")
        self.assertEqual(len(preview_data["tasks"]), 5)
        self.assertTrue(all(goal in task["prompt"] for task in preview_data["tasks"]))
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.workflow_runs(), [])

        submitted = self.assert_cli("workflow", "submit", "feature", goal, "--name", "Ticket 55")
        workflow = json.loads(submitted.stdout)
        self.assertEqual(workflow["status"], "queued")
        self.assertEqual(workflow["name"], "Ticket 55")
        listed = self.assert_cli("workflow", "list", "--json")
        self.assertEqual([run["id"] for run in json.loads(listed.stdout)], [workflow["id"]])
        shown = self.assert_cli("workflow", "show", workflow["id"])
        self.assertEqual(json.loads(shown.stdout)["tasks"], workflow["tasks"])

    def test_submitted_workflow_executes_through_fake_app_server_with_dependency_results(self):
        goal = "Add a searchable audit history"
        workflow = json.loads(self.assert_cli("workflow", "submit", "feature", goal).stdout)
        trace = self.project / "workflow-trace.jsonl"

        runner = subprocess.run(
            runner_command(self.project, trace),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(runner.returncode, 0, runner.stderr)
        final = json.loads(self.assert_cli("workflow", "show", workflow["id"]).stdout)
        self.assertEqual(final["status"], "completed")
        self.assertTrue(all(task["status"] == "completed" for task in final["tasks"]))
        starts = [entry for entry in trace_entries(trace) if entry.get("method") == "turn/start"]
        self.assertEqual(len(starts), 5)
        inputs = [entry["params"]["input"][0]["text"] for entry in starts]
        self.assertTrue(all(goal in prompt for prompt in inputs))
        self.assertIn("fixture result for fixture-turn-1", inputs[1])
        self.assertIn("fixture result for fixture-turn-2", inputs[2])
        self.assertIn("fixture result for fixture-turn-3", inputs[3])
        for dependency_turn in ("fixture-turn-2", "fixture-turn-3", "fixture-turn-4"):
            self.assertIn(dependency_turn, inputs[4])

    def test_task_and_workflow_wait_exit_codes_include_partial_paused_and_failed_runs(self):
        task_ids = {}
        for name in ("completed", "failed", "input", "paused", "timeout"):
            task_ids[name] = self.assert_cli("task", "add", f"wait case {name}").stdout.strip()

        self.store.update_task(task_ids["completed"], status="completed", result="Done")
        self.store.update_task(task_ids["failed"], status="failed", error="fixture failure")
        self.store.update_task(task_ids["input"], status="waiting_approval", run_id="run-input",
                               thread_id="thread-input", turn_id="turn-input")
        self.store.add_request("request-input", "item/commandExecution/requestApproval",
                               {"threadId": "thread-input", "turnId": "turn-input"},
                               "run-input", task_ids["input"])
        set_dispatch(self.store, True)

        cases = (("completed", 0, "completed"), ("failed", 1, "incomplete"),
                 ("input", 3, "needs_input"), ("paused", 4, "paused"))
        for name, expected_code, expected_outcome in cases:
            with self.subTest(scope="task", case=name):
                result = self.cli("task", "wait", task_ids[name], "--timeout", "0", "--interval", "0.1")
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertEqual(json.loads(result.stdout)["outcome"], expected_outcome)

        set_dispatch(self.store, False)
        self.store.update_task(task_ids["timeout"], status="running", run_id="run-timeout",
                               thread_id="thread-timeout", turn_id="turn-timeout")
        timeout = self.cli("task", "wait", task_ids["timeout"], "--timeout", "0", "--interval", "0.1")
        self.assertEqual(timeout.returncode, 124, timeout.stderr)
        self.assertEqual(json.loads(timeout.stdout)["outcome"], "timeout")

        workflow_runs = {name: self.submit_review_workflow(f"workflow {name}")
                         for name in ("completed", "failed", "input", "paused", "timeout")}
        for task in workflow_runs["completed"]["tasks"]:
            self.store.update_task(task["id"], status="completed", result="Done")
        failed_tasks = workflow_runs["failed"]["tasks"]
        self.store.update_task(failed_tasks[0]["id"], status="failed", error="earlier stage failed")
        self.store.update_task(failed_tasks[1]["id"], status="queued")
        input_task = workflow_runs["input"]["tasks"][0]
        self.store.update_task(input_task["id"], status="waiting_approval", run_id="workflow-input-run",
                               thread_id="workflow-input-thread", turn_id="workflow-input-turn")
        self.store.add_request("workflow-request", "item/fileChange/requestApproval",
                               {"threadId": "workflow-input-thread", "turnId": "workflow-input-turn"},
                               "workflow-input-run", input_task["id"])
        paused_tasks = workflow_runs["paused"]["tasks"]
        self.store.update_task(paused_tasks[0]["id"], status="completed", result="Earlier step done")
        self.store.update_task(paused_tasks[1]["id"], status="queued")

        set_dispatch(self.store, True)
        for name, expected_code, expected_outcome in (
            ("completed", 0, "completed"),
            ("failed", 1, "incomplete"),
            ("input", 3, "needs_input"),
            # The workflow has completed an earlier stage but still has queued work.
            ("paused", 4, "paused"),
        ):
            with self.subTest(scope="workflow", case=name):
                result = self.cli("workflow", "wait", workflow_runs[name]["id"],
                                  "--timeout", "0", "--interval", "0.1")
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertEqual(json.loads(result.stdout)["outcome"], expected_outcome)

        self.store.update_task(workflow_runs["timeout"]["tasks"][0]["id"], status="running",
                               run_id="workflow-timeout-run", thread_id="workflow-timeout-thread",
                               turn_id="workflow-timeout-turn")
        set_dispatch(self.store, False)
        timeout = self.cli("workflow", "wait", workflow_runs["timeout"]["id"],
                           "--timeout", "0", "--interval", "0.1")
        self.assertEqual(timeout.returncode, 124, timeout.stderr)
        self.assertEqual(json.loads(timeout.stdout)["outcome"], "timeout")

    def test_queue_pause_resume_and_active_steer_use_real_cli_and_jsonl_transport(self):
        first = self.assert_cli("task", "add", "Wait for a live steering message", "--title", "Active task")
        second = self.assert_cli("task", "add", "Dispatch only after resume", "--title", "Queued task")
        first_id, second_id = first.stdout.strip(), second.stdout.strip()
        trace = self.project / "active-controls.jsonl"
        runner = subprocess.Popen(
            [sys.executable, "-c", LIVE_RUNNER_HARNESS, str(self.project), str(FAKE_APP_SERVER), str(trace)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            self.wait_until(lambda: (
                self.store.get_task(first_id).get("status") == "running"
                and bool(self.store.get_task(first_id).get("turn_id"))
            ), runner)
            active = self.store.get_task(first_id)
            paused = json.loads(self.assert_cli("queue", "pause").stdout)
            self.assertTrue(paused["paused"])
            self.assertEqual(self.store.get_task(second_id)["status"], "queued")

            steer = self.assert_cli("task", "steer", first_id, "Stop exploring and report the partial findings")
            self.assertEqual(json.loads(steer.stdout)["status"], "pending")
            self.wait_until(lambda: (
                self.store.get_task(first_id).get("status") == "completed"
                and any(control.get("status") == "sent" for control in self.store.controls())
            ), runner)
            self.assertEqual(self.store.get_task(second_id)["status"], "queued")
            self.assertEqual(self.store.get_task(first_id)["result"],
                             "fixture steered result: Stop exploring and report the partial findings")

            requests = [entry for entry in trace_entries(trace) if entry.get("method") == "turn/steer"]
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["params"], {
                "threadId": active["thread_id"],
                "expectedTurnId": active["turn_id"],
                "input": [{"type": "text", "text": "Stop exploring and report the partial findings"}],
            })

            resumed = json.loads(self.assert_cli("queue", "resume").stdout)
            self.assertFalse(resumed["paused"])
            self.wait_until(lambda: self.store.get_task(second_id).get("status") == "completed", runner)
            self.assertEqual(len([entry for entry in trace_entries(trace) if entry.get("method") == "turn/start"]), 2)
        finally:
            if runner.poll() is None:
                try:
                    assert runner.stdin is not None
                    runner.stdin.write("stop\n")
                    runner.stdin.flush()
                    runner.wait(timeout=8)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    runner.kill()
                    runner.wait(timeout=5)
            stdout, stderr = runner.communicate(timeout=2)
        self.assertEqual(runner.returncode, 0, f"{stderr}\n{stdout}")
        self.assertEqual(stderr, "")

    def test_cli_tree_usage_and_scoped_reports_preserve_existing_output_files(self):
        definition = validate_definition({
            "version": 1,
            "name": "report-flow",
            "tasks": [
                {"key": "inspect", "title": "Inspect the root", "role": "worker", "prompt": "Inspect {goal}"},
                {"key": "verify", "title": "Verify the child", "role": "worker", "prompt": "Verify {goal}",
                 "depends_on": ["inspect"]},
                {"key": "review", "title": "Review separately", "role": "reviewer", "prompt": "Review {goal}",
                 "depends_on": ["inspect"]},
            ],
        })
        workflow = self.store.submit_workflow(definition, "Trace the report path")
        by_step = {task["workflow_step"]: task for task in workflow["tasks"]}
        root_id, child_id, reviewer_id = (by_step[key]["id"] for key in ("inspect", "verify", "review"))
        self.store.update_task(root_id, status="completed", thread_id="managed-root-thread",
                               result="Root evidence", usage={"total": {"inputTokens": 10, "totalTokens": 12}})
        self.store.update_task(child_id, status="running", run_id="run-child", thread_id="managed-child-thread",
                               turn_id="turn-child", parent_id=root_id)
        self.store.update_task(reviewer_id, status="queued")
        self.store.upsert_agent("native-child-thread", task_id=root_id, parent_thread_id="managed-root-thread",
                                native=True, role="explorer", status="completed")
        self.store.event(child_id, "test/progress", {"step": "verification"})

        filtered = self.assert_cli("status", "--workflow", workflow["id"], "--role", "worker",
                                   "--state", "running", "--active", "--json")
        filtered_tasks = json.loads(filtered.stdout)["tasks"]
        self.assertEqual([task["id"] for task in filtered_tasks], [child_id])

        tree = self.assert_cli("agents", "--workflow", workflow["id"])
        self.assertIn("Inspect the root", tree.stdout)
        self.assertIn("Verify the child", tree.stdout)
        self.assertIn("native-child-thread", tree.stdout)
        child_details = self.assert_cli("task", "show", child_id)
        self.assertEqual(json.loads(child_details.stdout)["thread_id"], "managed-child-thread")

        usage = self.assert_cli("usage", "--workflow", workflow["id"], "--role", "worker", "--json")
        usage_data = json.loads(usage.stdout)
        self.assertEqual({row["task_id"] for row in usage_data["threads"]}, {root_id, child_id})
        worker_coverage = usage_data["coverage"]["by_role"]["worker"]
        self.assertEqual(worker_coverage["reported"], 1)
        self.assertEqual(worker_coverage["missing"], 1)

        task_report = self.assert_cli("report", "--task", child_id, "--format", "json")
        task_report_data = json.loads(task_report.stdout)
        self.assertEqual([task["id"] for task in task_report_data["tasks"]], [child_id])
        self.assertEqual(task_report_data["dependencies"][0]["parent"]["id"], root_id)
        self.assertIn("verification", json.dumps(task_report_data["events"], ensure_ascii=False))
        workflow_report = self.assert_cli("report", "--workflow", workflow["id"], "--format", "json")
        self.assertEqual(len(json.loads(workflow_report.stdout)["tasks"]), 3)

        output = self.project / "existing-report.json"
        output.write_text("preserve this file\n", encoding="utf-8")
        no_overwrite = self.cli("report", "--workflow", workflow["id"], "--format", "json", "--output", str(output))
        self.assertEqual(no_overwrite.returncode, 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve this file\n")
        forced = self.assert_cli("report", "--workflow", workflow["id"], "--format", "json",
                                 "--output", str(output), "--force")
        self.assertIn("Report written to", forced.stdout)
        self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["tasks"]), 3)

    def test_mcp_workflow_controls_usage_and_report_tools_have_no_approval_action(self):
        project = str(self.project.resolve())
        messages = [
            mcp_message(1, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}}),
            mcp_message(2, "tools/list"),
            mcp_message(3, "tools/call", {"name": "orchestrator_workflow_templates", "arguments": {"project": project}}),
            mcp_message(4, "tools/call", {"name": "orchestrator_submit_workflow", "arguments": {
                "project": project, "source": "review", "goal": "Review the search changes", "name": "MCP review",
            }}),
            mcp_message(5, "tools/call", {"name": "orchestrator_workflows", "arguments": {"project": project}}),
            mcp_message(6, "tools/call", {"name": "orchestrator_usage", "arguments": {"project": project}}),
            mcp_message(7, "tools/call", {"name": "orchestrator_report", "arguments": {"project": project}}),
            mcp_message(8, "tools/call", {"name": "orchestrator_dispatch", "arguments": {"project": project, "paused": True}}),
            mcp_message(9, "tools/call", {"name": "orchestrator_status", "arguments": {"project": project}}),
            mcp_message(10, "tools/call", {"name": "orchestrator_dispatch", "arguments": {"project": project, "paused": False}}),
        ]
        responses, stderr = run_mcp(self.project, messages)

        self.assertEqual(stderr, "")
        self.assertEqual([response["id"] for response in responses], list(range(1, 11)))
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertTrue({
            "orchestrator_workflow_templates", "orchestrator_submit_workflow", "orchestrator_workflows",
            "orchestrator_workflow", "orchestrator_steer_task", "orchestrator_dispatch",
            "orchestrator_usage", "orchestrator_report",
        } <= tool_names)
        self.assertFalse(any("approv" in name.lower() or "respond" in name.lower() for name in tool_names))

        templates = responses[2]["result"]["structuredContent"]["templates"]
        self.assertEqual({template["name"] for template in templates}, {"feature", "bugfix", "review"})
        submitted = responses[3]["result"]["structuredContent"]["workflow"]
        self.assertEqual(submitted["status"], "queued")
        self.assertEqual(submitted["name"], "MCP review")
        self.assertEqual(responses[4]["result"]["structuredContent"]["workflows"][0]["id"], submitted["id"])
        self.assertIn("coverage", responses[5]["result"]["structuredContent"])
        self.assertEqual(
            {task["id"] for task in responses[6]["result"]["structuredContent"]["tasks"]},
            {task["id"] for task in submitted["tasks"]},
        )
        self.assertTrue(responses[7]["result"]["structuredContent"]["dispatch"]["paused"])
        self.assertTrue(responses[8]["result"]["structuredContent"]["dispatch"]["paused"])
        self.assertFalse(responses[9]["result"]["structuredContent"]["dispatch"]["paused"])
        self.assertEqual(self.store.get_workflow_run(submitted["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
