from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from codex_orchestrator.store import Store


ROOT = Path(__file__).resolve().parents[1]
FAKE_APP_SERVER = ROOT / "tests" / "fixtures" / "fake_app_server.py"

RUNNER_HARNESS = r"""
import sys
import threading
from codex_orchestrator.engine import Engine
from codex_orchestrator.rpc import AppServer
from codex_orchestrator.store import Store

project, fixture, trace, *flags = sys.argv[1:]
controlled = "--controlled" in flags
enable_live = "--enable-live-models" in flags
fixture_flags = [flag for flag in flags if flag not in {"--controlled", "--enable-live-models"}]
server = AppServer(
    command=[sys.executable, fixture, "--orchestration", "--trace", trace, *fixture_flags],
    cwd=project,
    request_timeout=3,
)
engine = Engine(
    Store(project), server=server, max_workers=2, poll_interval=0.02,
    shutdown_grace=0.1, enable_live_models=enable_live,
)
if controlled:
    stop_event = threading.Event()
    def wait_for_stop():
        sys.stdin.readline()
        stop_event.set()
    threading.Thread(target=wait_for_stop, daemon=True).start()
    status = engine.run(once=True, stop_event=stop_event)
else:
    status = engine.run(once=True)
if engine.last_error:
    print(engine.last_error, file=sys.stderr)
raise SystemExit(status)
"""


def run_cli(project: Path, *arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codex_orchestrator", "--project", str(project), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def runner_command(project: Path, trace: Path, *flags: str) -> list[str]:
    return [sys.executable, "-c", RUNNER_HARNESS, str(project), str(FAKE_APP_SERVER), str(trace), *flags]


def trace_entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.project = Path(self._temporary.name) / "project"
        self.project.mkdir()

    def tearDown(self):
        self._temporary.cleanup()

    def cli(self, *arguments: str, timeout: float = 10):
        return run_cli(self.project, *arguments, timeout=timeout)

    def test_cli_engine_jsonl_dependencies_defaults_and_thread_continuation(self):
        initialized = self.cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        changed = self.cli("model", "set", "worker", "worker-model-v1", "--effort", "high")
        self.assertEqual(changed.returncode, 0, changed.stderr)

        first = self.cli("task", "add", "Inspect project state", "--role", "worker", "--title", "Explore")
        second = self.cli(
            "task", "add", "Review completed exploration", "--role", "worker", "--after", first.stdout.strip(),
            "--model", "task-model-override", "--effort", "low", "--title", "Review",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_id, second_id = first.stdout.strip(), second.stdout.strip()

        trace = self.project / "fake-app-server.jsonl"
        runner = subprocess.run(
            runner_command(self.project, trace), cwd=ROOT, capture_output=True, text=True,
            timeout=15, check=False,
        )
        self.assertEqual(runner.returncode, 0, runner.stderr)
        self.assertEqual(runner.stdout, "")
        self.assertEqual(runner.stderr, "")

        snapshot_result = self.cli("status", "--json")
        self.assertEqual(snapshot_result.returncode, 0, snapshot_result.stderr)
        snapshot = json.loads(snapshot_result.stdout)
        tasks = {task["id"]: task for task in snapshot["tasks"]}
        self.assertEqual(tasks[first_id]["status"], "completed")
        self.assertEqual(tasks[first_id]["result"], "fixture result for fixture-turn-1")
        self.assertEqual(tasks[first_id]["current_model"], "worker-model-v1")
        self.assertEqual(tasks[first_id]["current_effort"], "high")
        self.assertEqual(tasks[second_id]["status"], "completed")
        self.assertEqual(tasks[second_id]["current_model"], "task-model-override")
        self.assertEqual(tasks[second_id]["current_effort"], "low")
        self.assertEqual(tasks[second_id]["depends_on"], [first_id])

        events = Store(self.project).events(after=0, limit=1000)
        first_completed = next(event["id"] for event in events if event["task_id"] == first_id and event["kind"] == "turn/completed")
        second_claimed = next(event["id"] for event in events if event["task_id"] == second_id and event["kind"] == "task/claimed")
        self.assertLess(first_completed, second_claimed)

        requests = trace_entries(trace)
        turn_starts = [entry for entry in requests if entry.get("method") == "turn/start"]
        self.assertEqual(len(turn_starts), 2)
        self.assertEqual([(entry["params"]["model"], entry["params"]["effort"]) for entry in turn_starts], [
            ("worker-model-v1", "high"), ("task-model-override", "low")
        ])

        updated_default = self.cli("model", "set", "worker", "worker-model-v2", "--effort", "xhigh")
        self.assertEqual(updated_default.returncode, 0, updated_default.stderr)
        continued = self.cli("task", "continue", first_id, "Continue from the saved thread")
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertEqual(json.loads(continued.stdout)["thread_id"], tasks[first_id]["thread_id"])

        resumed = subprocess.run(
            runner_command(self.project, trace), cwd=ROOT, capture_output=True, text=True,
            timeout=15, check=False,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after_continue = json.loads(self.cli("task", "show", first_id).stdout)
        self.assertEqual(after_continue["status"], "completed")
        self.assertEqual(after_continue["current_model"], "worker-model-v2")
        self.assertEqual(after_continue["current_effort"], "xhigh")

        all_requests = trace_entries(trace)
        thread_resumes = [entry for entry in all_requests if entry.get("method") == "thread/resume"]
        self.assertEqual(len(thread_resumes), 1)
        self.assertEqual(thread_resumes[0]["params"]["threadId"], tasks[first_id]["thread_id"])
        all_turn_starts = [entry for entry in all_requests if entry.get("method") == "turn/start"]
        self.assertEqual((all_turn_starts[-1]["params"]["model"], all_turn_starts[-1]["params"]["effort"]),
                         ("worker-model-v2", "xhigh"))

    def test_cli_live_control_and_approval_travel_over_real_jsonl_transport(self):
        self.assertEqual(self.cli("init").returncode, 0)
        task_id = self.cli("task", "add", "Wait for a fixture approval").stdout.strip()
        trace = self.project / "approval-app-server.jsonl"
        runner = subprocess.Popen(
            runner_command(self.project, trace, "--controlled", "--enable-live-models", "--approval"),
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        try:
            deadline = time.monotonic() + 8
            approval = None
            while time.monotonic() < deadline:
                result = self.cli("approvals")
                self.assertEqual(result.returncode, 0, result.stderr)
                pending = json.loads(result.stdout)
                if pending:
                    approval = pending[0]
                    break
                if runner.poll() is not None:
                    self.fail(f"runner exited before an approval arrived: {runner.stderr.read()}")
                time.sleep(0.05)
            self.assertIsNotNone(approval, "runner did not persist the fake server approval")

            before = json.loads(self.cli("task", "show", task_id).stdout)
            self.assertEqual(before["status"], "waiting_approval")
            self.assertTrue(before["turn_id"])

            live = self.cli("task", "model", task_id, "live-model-v2", "--effort", "high", "--live")
            self.assertEqual(live.returncode, 0, live.stderr)
            self.assertIn("Live request queued", live.stdout)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = json.loads(self.cli("task", "show", task_id).stdout)
                controls = Store(self.project).controls()
                if current.get("live_update_status") == "applied" and controls and controls[0]["status"] == "published":
                    break
                if runner.poll() is not None:
                    self.fail(f"runner exited before live model publication: {runner.stderr.read()}")
                time.sleep(0.05)
            self.assertEqual(current.get("live_update_status"), "applied")
            self.assertEqual(controls[0]["status"], "published")
            self.assertEqual(controls[0]["turn_id"], before["turn_id"])

            answered = self.cli("respond", approval["id"], "--decision", "accept")
            self.assertEqual(answered.returncode, 0, answered.stderr)
            self.assertEqual(json.loads(answered.stdout)["status"], "answered")
            runner.wait(timeout=10)
            stdout, stderr = runner.communicate(timeout=2)
            self.assertEqual(runner.returncode, 0, stderr)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")

            final = json.loads(self.cli("task", "show", task_id).stdout)
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["result"], "fixture approval result: accept")
            self.assertEqual(Store(self.project).requests()[0]["status"], "delivered")
            requests = trace_entries(trace)
            self.assertTrue(any(entry.get("method") == "turn/settings/update" for entry in requests))
            self.assertTrue(any(entry.get("method") == "fixture/approval_response" and
                                 entry["response"] == {"decision": "accept"} for entry in requests))
        finally:
            if runner.poll() is None:
                try:
                    runner.stdin.write("stop\n")
                    runner.stdin.flush()
                    runner.wait(timeout=5)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    runner.kill()
                    runner.wait(timeout=5)
            if runner.stdout is not None and not runner.stdout.closed:
                runner.stdout.close()
            if runner.stderr is not None and not runner.stderr.closed:
                runner.stderr.close()
            if runner.stdin is not None and not runner.stdin.closed:
                runner.stdin.close()

    def test_app_server_death_marks_dispatch_uncertain_instead_of_success(self):
        self.assertEqual(self.cli("init").returncode, 0)
        task_id = self.cli("task", "add", "Fake server exits during dispatch").stdout.strip()
        trace = self.project / "crash-app-server.jsonl"
        runner = subprocess.run(
            runner_command(self.project, trace, "--exit-on-turn-start"), cwd=ROOT,
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertNotEqual(runner.returncode, 0)
        task = json.loads(self.cli("task", "show", task_id).stdout)
        self.assertEqual(task["status"], "lost")
        self.assertNotEqual(task["status"], "completed")
        self.assertIn("app-server process exited", task["error"])
        requests = trace_entries(trace)
        self.assertTrue(any(entry.get("method") == "turn/start" for entry in requests))
        self.assertFalse(any(entry.get("method") == "turn/completed" for entry in requests))


if __name__ == "__main__":
    unittest.main()
