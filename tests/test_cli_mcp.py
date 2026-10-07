from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_orchestrator.store import Store


ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codex_orchestrator", "--project", str(project), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def mcp_message(request_id: int, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def run_mcp(project: Path, messages: list[dict]) -> tuple[list[dict], str]:
    wire = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
    result = run_cli(project, "mcp", timeout=10) if not wire else subprocess.run(
        [sys.executable, "-m", "codex_orchestrator", "--project", str(project), "mcp"],
        cwd=ROOT,
        input=wire,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"MCP process failed ({result.returncode}): {result.stderr}\n{result.stdout}")
    lines = result.stdout.splitlines()
    responses = [json.loads(line) for line in lines]
    return responses, result.stderr


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.project = Path(self._temporary.name) / "project"
        self.project.mkdir()

    def tearDown(self):
        self._temporary.cleanup()

    def cli(self, *arguments: str, timeout: float = 10):
        return run_cli(self.project, *arguments, timeout=timeout)

    def test_cli_init_tasks_status_events_watch_and_role_model_policy(self):
        initialized = self.cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertIn("Initialized", initialized.stdout)
        self.assertTrue((self.project / ".orchestrator" / "state.sqlite3").is_file())

        custom_role = self.cli(
            "role", "add", "auditor", "--model", "auditor-model", "--effort", "high",
            "--sandbox", "read-only", "--instructions", "Inspect and report risks.",
            "--description", "Independent review",
        )
        self.assertEqual(custom_role.returncode, 0, custom_role.stderr)
        self.assertEqual(json.loads(custom_role.stdout)["name"], "auditor")

        role_change = self.cli("model", "set", "worker", "worker-default-v2", "--effort", "xhigh")
        self.assertEqual(role_change.returncode, 0, role_change.stderr)
        self.assertIn("Applies to future turns", role_change.stdout)

        first = self.cli("task", "add", "Explore the code", "--role", "worker", "--title", "Discovery")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_id = first.stdout.strip()
        self.assertRegex(first_id, r"^[0-9a-f]{12}$")
        second = self.cli(
            "task", "add", "Review the result", "--role", "auditor", "--after", first_id,
            "--model", "task-override", "--effort", "low", "--title", "Review",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_id = second.stdout.strip()

        status = self.cli("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        snapshot = json.loads(status.stdout)
        tasks = {task["id"]: task for task in snapshot["tasks"]}
        self.assertEqual(tasks[first_id]["next_model"], "worker-default-v2")
        self.assertEqual(tasks[first_id]["next_effort"], "xhigh")
        self.assertEqual(tasks[second_id]["next_model"], "task-override")
        self.assertEqual(tasks[second_id]["next_effort"], "low")
        self.assertEqual(tasks[second_id]["depends_on"], [first_id])

        shown = self.cli("task", "show", second_id)
        self.assertEqual(json.loads(shown.stdout)["id"], second_id)
        recent = self.cli("events", "--task", second_id, "--after", "0", "--limit", "1")
        recent_events = json.loads(recent.stdout)
        self.assertEqual(len(recent_events), 1)
        cursor = recent_events[-1]["id"]
        self.assertEqual(json.loads(self.cli("events", "--task", second_id, "--after", str(cursor)).stdout), [])

        watch = self.cli("watch", "--count", "2", "--interval", "0.1", "--json", timeout=5)
        self.assertEqual(watch.returncode, 0, watch.stderr)
        snapshots = [json.loads(line) for line in watch.stdout.splitlines()]
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(len(item["tasks"]) == 2 for item in snapshots))

        failed = self.cli("task", "add", "No missing dependency", "--after", "does-not-exist")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("Error:", failed.stderr)
        after_failure = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(len(after_failure["tasks"]), 2)

    def test_cli_errors_live_requirement_continue_terminal_only_and_control_binding(self):
        self.assertEqual(self.cli("init").returncode, 0)
        task_id = self.cli("task", "add", "Queued work").stdout.strip()

        live_queued = self.cli("task", "model", task_id, "live-model", "--effort", "high", "--live")
        self.assertEqual(live_queued.returncode, 1)
        self.assertIn("Live changes require an active turn", live_queued.stderr)
        store = Store(self.project)
        self.assertIsNone(store.get_task(task_id)["model"])
        self.assertEqual(store.controls(), [])

        active = store.update_task(
            task_id, status="running", run_id="runner-1", turn_id="turn-2",
            thread_id="thread-1", current_model="gpt-6-luna", current_effort="max",
        )
        live = self.cli("task", "model", task_id, "live-model", "--effort", "high", "--live")
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertIn("Live request queued", live.stdout)
        control = store.controls()[0]
        self.assertEqual((control["task_id"], control["run_id"], control["turn_id"]),
                         (task_id, "runner-1", "turn-2"))

        while_active = self.cli("task", "continue", task_id, "Cannot continue active work")
        self.assertEqual(while_active.returncode, 1)
        self.assertIn("Only a terminal task can continue", while_active.stderr)
        store.update_task(task_id, status="completed", result="Done")
        continued = self.cli("task", "continue", task_id, "Continue the saved thread")
        self.assertEqual(continued.returncode, 0, continued.stderr)
        continued_task = json.loads(continued.stdout)
        self.assertEqual(continued_task["status"], "queued")
        self.assertEqual(continued_task["thread_id"], "thread-1")

        bad_watch = self.cli("watch", "--count", "0", "--json")
        self.assertEqual(bad_watch.returncode, 1)
        self.assertIn("count must be positive", bad_watch.stderr)
        missing_role = self.cli("task", "add", "Unknown role", "--role", "not-a-role")
        self.assertEqual(missing_role.returncode, 1)
        self.assertIn("Unknown role", missing_role.stderr)


class McpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.project_a = root / "project-a"
        self.project_b = root / "project-b"
        self.project_a.mkdir()
        self.project_b.mkdir()

    def tearDown(self):
        self._temporary.cleanup()

    def test_stdio_initialize_tools_calls_project_isolation_and_type_errors(self):
        a, b = str(self.project_a), str(self.project_b)
        messages = [
            mcp_message(1, "tools/list"),
            mcp_message(2, "initialize", {"protocolVersion": "2025-03-26", "clientInfo": {"name": "test", "version": "1"}}),
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            mcp_message(3, "tools/list"),
            mcp_message(4, "tools/call", {"name": "orchestrator_roles", "arguments": {"project": a}}),
            mcp_message(5, "tools/call", {"name": "orchestrator_add_task", "arguments": {
                "project": a, "prompt": "Inspect the isolated project", "role": "worker",
                "title": "A only", "depends_on": [],
            }}),
            mcp_message(6, "tools/call", {"name": "orchestrator_status", "arguments": {"project": b}}),
            mcp_message(7, "tools/call", {"name": "orchestrator_add_task", "arguments": {
                "project": a, "prompt": 42, "role": "worker",
            }}),
            mcp_message(8, "tools/call", {"name": "orchestrator_add_task", "arguments": {
                "project": a, "prompt": "Missing role",
            }}),
            mcp_message(9, "tools/call", {"name": "orchestrator_set_model", "arguments": {
                "project": a, "model": "next-model", "role": "worker", "live": "false",
            }}),
            mcp_message(10, "tools/call", {"name": "orchestrator_task", "arguments": {
                "project": a, "task_id": "missing-task",
            }}),
        ]
        responses, stderr = run_mcp(self.project_a, messages)
        self.assertEqual(stderr, "")
        self.assertEqual(len(responses), 10)
        # The complete stdout stream is protocol JSON only; notifications produce no response.
        self.assertEqual([response["id"] for response in responses], list(range(1, 11)))
        self.assertEqual(responses[0]["error"]["code"], -32002)
        initialized = responses[1]["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-03-26")
        self.assertIn("tools", initialized["capabilities"])
        tool_names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        self.assertIn("orchestrator_add_task", tool_names)
        self.assertIn("orchestrator_set_model", tool_names)

        roles = responses[3]["result"]["structuredContent"]["roles"]
        self.assertIn("worker", {role["name"] for role in roles})
        added = responses[4]["result"]["structuredContent"]["task"]
        self.assertEqual(added["title"], "A only")
        isolated_status = responses[5]["result"]["structuredContent"]
        self.assertEqual(isolated_status["tasks"], [])
        self.assertNotEqual(self.project_a / ".orchestrator" / "state.sqlite3",
                            self.project_b / ".orchestrator" / "state.sqlite3")

        for response in responses[6:]:
            self.assertTrue(response["result"]["isError"], response)
            self.assertTrue(response["result"]["content"][0]["text"])

        task_result, task_stderr = run_mcp(self.project_a, [
            mcp_message(1, "initialize", {"protocolVersion": "2025-06-18"}),
            mcp_message(2, "tools/call", {"name": "orchestrator_task", "arguments": {
                "project": a, "task_id": added["id"],
            }}),
        ])
        self.assertEqual(task_stderr, "")
        self.assertEqual(task_result[1]["result"]["structuredContent"]["task"]["id"], added["id"])


if __name__ == "__main__":
    unittest.main()
