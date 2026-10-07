from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_orchestrator.store import Store
from codex_orchestrator.views import (
    build_report,
    filter_snapshot,
    render_report_markdown,
    render_tree,
    usage_report,
)


class SnapshotViewTests(unittest.TestCase):
    def test_filter_snapshot_intersects_filters_and_keeps_input_unchanged(self):
        source = {
            "project": "/tmp/project",
            "tasks": [
                {"id": "a", "workflow_id": "flow-1", "role": "worker", "status": "running"},
                {"id": "b", "workflow_id": "flow-1", "role": "tester", "status": "completed"},
                {"id": "c", "workflow_id": "flow-2", "role": "worker", "status": "queued"},
            ],
            "agents": [{"id": "agent-a", "task_id": "a"}, {"id": "agent-b", "task_id": "b"}],
            "requests": [{"id": "request-a", "task_id": "a"}, {"id": "request-b", "task_id": "b"}],
            "events": [{"id": 1, "task_id": "a"}, {"id": 2, "task_id": "c"}],
        }

        filtered = filter_snapshot(source, workflow_id="flow-1", role="worker", active_only=True)

        self.assertEqual([task["id"] for task in filtered["tasks"]], ["a"])
        self.assertEqual([agent["id"] for agent in filtered["agents"]], ["agent-a"])
        self.assertEqual([request["id"] for request in filtered["requests"]], ["request-a"])
        self.assertEqual([event["id"] for event in filtered["events"]], [1])
        self.assertEqual(source["tasks"][1]["status"], "completed")
        self.assertEqual(len(source["agents"]), 2)
        self.assertEqual(filtered["filters"]["states"], None)

    def test_render_tree_nests_managed_and_native_agents_and_survives_orphans_and_cycles(self):
        snapshot = {
            "project": "sample",
            "tasks": [
                {"id": "root", "title": "Root", "status": "running", "role": "worker",
                 "thread_id": "root-thread", "parent_id": None, "depends_on": [],
                 "workflow_id": "flow-7", "workflow_step": "coordinate"},
                {"id": "child", "title": "Child", "status": "queued", "role": "tester",
                 "thread_id": None, "parent_id": "root", "depends_on": ["root"]},
                {"id": "missing-parent-child", "title": "Orphan task", "status": "blocked", "role": "worker",
                 "parent_id": "missing", "depends_on": []},
                {"id": "cycle-a", "title": "Cycle A", "status": "queued", "role": "worker", "parent_id": "cycle-b"},
                {"id": "cycle-b", "title": "Cycle B", "status": "queued", "role": "worker", "parent_id": "cycle-a"},
            ],
            "agents": [
                {"id": "agent-a", "task_id": "root", "parent_thread_id": "root-thread", "nickname": "Scout",
                 "role": "explorer", "status": "running", "activity": "Reading files"},
                {"id": "agent-b", "task_id": "root", "parent_thread_id": "agent-a", "role": "tester", "status": "idle"},
                {"id": "cycle-agent-a", "task_id": "root", "parent_thread_id": "cycle-agent-b", "role": "worker"},
                {"id": "cycle-agent-b", "task_id": "root", "parent_thread_id": "cycle-agent-a", "role": "worker"},
                {"id": "unattached", "task_id": "gone", "parent_thread_id": "unknown-thread", "role": "explorer"},
            ],
        }

        rendered = render_tree(snapshot)

        self.assertIn("task root: Root", rendered)
        self.assertIn("workflow=flow-7", rendered)
        self.assertIn("step=coordinate", rendered)
        self.assertIn("task child: Child", rendered)
        self.assertLess(rendered.index("task root: Root"), rendered.index("task child: Child"))
        self.assertIn("native Scout (agent-a)", rendered)
        self.assertIn("native tester (agent-b)", rendered)
        self.assertIn("missing parent=missing", rendered)
        self.assertIn("cycle/orphan", rendered)
        self.assertIn("unattached", rendered)
        self.assertLess(len(rendered), 6000)

    def test_usage_report_normalizes_counters_and_reports_coverage_per_thread(self):
        report = usage_report({
            "tasks": [
                {"id": "task-a", "title": "A", "thread_id": "managed-thread", "role": "worker",
                 "status": "completed", "usage": {"last": {"inputTokens": 4},
                                                        "total": {"inputTokens": 10, "totalTokens": 12}}},
                {"id": "task-b", "title": "B", "thread_id": None, "role": "tester", "status": "queued"},
            ],
            "agents": [
                {"id": "native-thread", "task_id": "task-a", "parent_thread_id": "managed-thread",
                 "native": True, "role": "explorer", "status": "completed",
                 "usage": {"last": {"cached_input_tokens": 1},
                           "total": {"cached_input_tokens": 3, "output_tokens": 2}}},
                # A duplicate thread observation must not create a second row.
                {"id": "managed-thread", "task_id": "task-a", "native": True, "role": "worker",
                 "usage": {"total": {"totalTokens": 999}}},
            ],
        })

        rows = {row["thread_id"]: row for row in report["threads"]}
        self.assertEqual(len(report["threads"]), 3)
        self.assertEqual(rows["managed-thread"]["total"], {"input_tokens": 10, "total_tokens": 12})
        self.assertEqual(rows["native-thread"]["total"], {"cached_input_tokens": 3, "output_tokens": 2})
        self.assertFalse(rows[None]["usage_available"])
        self.assertEqual(rows[None]["missing_reason"], "thread_not_started")
        self.assertEqual(report["coverage"]["reported"], 2)
        self.assertEqual(report["coverage"]["missing"], 1)
        self.assertEqual(report["coverage"]["by_kind"]["managed"]["reported_total_sums"],
                         {"input_tokens": 10, "total_tokens": 12})
        self.assertEqual(report["coverage"]["by_kind"]["native"]["reported_total_sums"],
                         {"cached_input_tokens": 3, "output_tokens": 2})
        self.assertEqual(report["aggregation"], "reported_thread_counter_sums_by_kind_and_role")
        self.assertIn("may overlap", report["note"])
        self.assertNotIn("combined", report)


class RunReportTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.project = Path(self._temporary.name) / "project"
        self.project.mkdir()
        self.store = Store(self.project)

    def tearDown(self):
        self._temporary.cleanup()

    def test_build_report_is_scoped_self_contained_and_caps_recent_events(self):
        dependency = self.store.add_task("Inspect source", role="explorer", title="Discovery")
        task = self.store.add_task(
            "Review the result\n```python\nprint('untrusted')\n````",
            role="reviewer", title="Review | #1", depends_on=[dependency["id"]], parent_id=dependency["id"],
        )
        self.store.update_task(dependency["id"], workflow_id="flow-dependency", result="Found relevant code")
        self.store.update_task(task["id"], workflow_id="flow-review", status="running", run_id="run-1",
                               thread_id="review-thread", turn_id="turn-1", current_model="gpt-6-luna")
        self.store.set_task_model(task["id"], "gpt-6.1-sol", live=True)
        request = self.store.add_request(7, "item/commandExecution/requestApproval",
                                         {"threadId": "review-thread", "turnId": "turn-1"}, "run-1", task["id"])
        self.store.update_request(request["id"], status="resolved", response={"decision": "accept"}, resolved_at=10)
        for index in range(4):
            self.store.event(task["id"], "report/test", {"index": index})

        with patch("codex_orchestrator.views._MAX_REPORT_EVENTS", 2):
            report = build_report(self.store, task_id=task["id"])

        self.assertEqual(report["scope"]["task_id"], task["id"])
        self.assertEqual([entry["id"] for entry in report["tasks"]], [task["id"]])
        self.assertEqual(report["tasks"][0]["prompt"], task["prompt"])
        self.assertEqual(report["dependencies"][0]["parent"]["id"], dependency["id"])
        self.assertEqual(report["dependencies"][0]["depends_on"][0]["result"], "Found relevant code")
        self.assertEqual(report["requests"][0]["decision"], "accept")
        self.assertEqual(len(report["controls"]), 1)
        self.assertEqual([event["data"].get("index") for event in report["events"] if event["kind"] == "report/test"],
                         [2, 3])
        self.assertTrue(report["events_truncated"])
        self.assertEqual(report["event_limit"], 2)

    def test_markdown_fences_hostile_prompt_and_escapes_heading_metadata(self):
        task = self.store.add_task("First line\n````\n#### injected heading", title="title | # <tag>")
        self.store.update_task(task["id"], result="A result with `ticks`")
        report = build_report(self.store, task_id=task["id"])

        markdown = render_report_markdown(report)

        self.assertIn("title \\| \\# \\<tag\\>", markdown)
        self.assertIn("`````text\nFirst line\n````\n#### injected heading\n`````", markdown)
        self.assertIn("older events omitted: False", markdown)
        self.assertIn("A result with `ticks`", markdown)
        self.assertNotIn("\x1b", markdown)

    def test_unknown_workflow_cannot_produce_a_successful_empty_report(self):
        from codex_orchestrator.workflows import load_definition
        run = self.store.submit_workflow(load_definition("review"), "Inspect a change")
        with self.assertRaisesRegex(ValueError, "Unknown workflow"):
            build_report(self.store, workflow_id="mistyped-id")
        report = build_report(self.store, workflow_id=run["id"])
        self.assertEqual({task["id"] for task in report["tasks"]}, set(run["task_ids"]))


if __name__ == "__main__":
    unittest.main()
