from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_orchestrator.store import Store
from codex_orchestrator.workflows import (
    expand_workflow,
    load_definition,
    templates,
    validate_definition,
)


class WorkflowDefinitionTests(unittest.TestCase):
    def test_builtins_are_valid_fresh_definitions_with_bounded_final_handoff(self):
        listed = templates()
        self.assertEqual({definition["name"] for definition in listed}, {"feature", "bugfix", "review"})
        for definition in listed:
            normalized = validate_definition(definition)
            self.assertEqual(normalized["name"], definition["name"])
            self.assertEqual(len(normalized["tasks"]), len(definition["tasks"]))
            if definition["name"] != "review":
                self.assertEqual(definition["tasks"][-1]["key"], "integrate")
                self.assertIn("Do not commit", definition["tasks"][-1]["prompt"])
        listed[0]["tasks"].clear()
        self.assertTrue(all(item["tasks"] for item in templates()))

    def test_json_definition_load_expands_goal_and_rejects_duplicate_json_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.json"
            definition = {
                "version": 1,
                "name": "small-flow",
                "description": "A small user workflow.",
                "tasks": [{"key": "inspect", "title": "Inspect", "role": "explorer",
                           "prompt": "Inspect this goal: {goal}"}],
            }
            path.write_text(json.dumps(definition), encoding="utf-8")
            loaded = load_definition(str(path))
            self.assertEqual(loaded["name"], definition["name"])
            self.assertEqual(loaded["tasks"][0]["depends_on"], [])
            self.assertIn("Implement the login", expand_workflow(loaded, "Implement the login")
                          ["tasks"][0]["prompt"])

            path.write_text('{"version":1,"version":1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate JSON field"):
                load_definition(str(path))

    def test_validation_rejects_unknown_fields_duplicate_keys_bad_edges_and_cycles(self):
        base = {"version": 1, "name": "invalid", "tasks": [
            {"key": "a", "title": "A", "role": "explorer", "prompt": "Do A"},
            {"key": "b", "title": "B", "role": "worker", "prompt": "Do B", "depends_on": ["a"]},
        ]}
        invalid = [
            ({**base, "surprise": True}, "Unexpected workflow fields"),
            ({**base, "tasks": [base["tasks"][0], {**base["tasks"][1], "key": "a"}]}, "Duplicate workflow task key"),
            ({**base, "tasks": [{**base["tasks"][0], "depends_on": ["missing"]}, base["tasks"][1]]}, "unknown task key"),
            ({**base, "tasks": [{**base["tasks"][0], "depends_on": ["a"]}, base["tasks"][1]]}, "cannot depend on itself"),
            ({**base, "tasks": [{**base["tasks"][0], "depends_on": ["b"]},
                                  {**base["tasks"][1], "depends_on": ["a"]}]}, "dependency cycle"),
        ]
        for definition, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_definition(definition)


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.store = Store(self.project)

    def tearDown(self):
        self.temporary.cleanup()

    def test_submission_persists_named_dag_and_step_policy_then_derives_status(self):
        definition = {
            "version": 1,
            "name": "sample",
            "tasks": [
                {"key": "inspect", "title": "Inspect", "role": "explorer", "prompt": "Inspect: {goal}"},
                {"key": "implement", "title": "Implement", "role": "worker", "prompt": "Implement: {goal}",
                 "depends_on": ["inspect"], "model": "step-model", "effort": "low"},
            ],
        }
        run = self.store.submit_workflow(definition, "Add a durable workflow", name="release-42")
        self.assertEqual(run["name"], "release-42")
        self.assertEqual(run["definition_name"], "sample")
        self.assertRegex(run["id"], r"^[0-9a-f]{12}$")
        self.assertEqual(run["status"], "queued")
        self.assertEqual(set(run["by_key"]), {"inspect", "implement"})
        task_by_key = {task["workflow_step"]: task for task in run["tasks"]}
        self.assertEqual(task_by_key["inspect"]["workflow_id"], run["id"])
        self.assertEqual(task_by_key["inspect"]["prompt"], "Inspect: Add a durable workflow")
        self.assertEqual(task_by_key["implement"]["depends_on"], [run["by_key"]["inspect"]])
        self.assertEqual(self.store.task_policy(task_by_key["implement"])["model"], "step-model")

        self.store.set_role("explorer", model="changed-role-default", effort="high")
        self.assertEqual(self.store.task_policy(task_by_key["inspect"])["model"], "changed-role-default")
        self.assertEqual(self.store.task_policy(task_by_key["inspect"])["effort"], "high")
        self.assertEqual(self.store.get_workflow_run(run["id"])["status"], "queued")

        for task in run["tasks"]:
            self.store.update_task(task["id"], status="completed")
        self.assertEqual(self.store.get_workflow_run(run["id"])["status"], "completed")
        self.assertEqual(self.store.workflow_runs()[0]["id"], run["id"])
        self.assertEqual({item["id"] for item in self.store.snapshot()["workflows"]}, {run["id"]})

        # Labels are descriptive; durable inspection is by the stable ID.
        repeated = self.store.submit_workflow(definition, "Another run", name="release-42")
        self.assertNotEqual(repeated["id"], run["id"])

    def test_invalid_submission_and_mid_transaction_failure_leave_no_partial_state(self):
        valid = {
            "version": 1,
            "name": "atomic",
            "tasks": [{"key": "step", "title": "Step", "role": "explorer", "prompt": "Do {goal}"}],
        }
        before_tasks, before_runs, before_events = self.store.tasks(), self.store.workflow_runs(), self.store.events()
        with self.assertRaisesRegex(ValueError, "Unknown roles|Unknown role"):
            self.store.submit_workflow({**valid, "tasks": [{**valid["tasks"][0], "role": "missing"}]}, "Bad role")
        with self.assertRaisesRegex(ValueError, "cycle"):
            cycle = {**valid, "tasks": [
                {"key": "one", "title": "One", "role": "explorer", "prompt": "A", "depends_on": ["two"]},
                {"key": "two", "title": "Two", "role": "worker", "prompt": "B", "depends_on": ["one"]},
            ]}
            self.store.submit_workflow(cycle, "Bad graph")
        self.assertEqual(self.store.tasks(), before_tasks)
        self.assertEqual(self.store.workflow_runs(), before_runs)
        self.assertEqual(self.store.events(), before_events)

        original_event = self.store._event

        def fail_after_insert(db, task_id, kind, data):
            if kind == "task/created":
                raise RuntimeError("simulated event write failure")
            original_event(db, task_id, kind, data)

        self.store._event = fail_after_insert
        try:
            with self.assertRaisesRegex(RuntimeError, "simulated event"):
                self.store.submit_workflow(valid, "Force rollback")
        finally:
            del self.store._event
        self.assertEqual(self.store.tasks(), before_tasks)
        self.assertEqual(self.store.workflow_runs(), before_runs)
        self.assertEqual(self.store.events(), before_events)

    def test_dispatch_pause_is_checked_inside_atomic_claim(self):
        task = self.store.add_task("Wait while paused")
        policy = self.store.task_policy(task)
        self.store.set_setting("dispatch", {"paused": True})
        self.assertIsNone(self.store.claim_task(task["id"], "runner-paused", policy))
        self.assertEqual(self.store.get_task(task["id"])["status"], "queued")
        self.store.set_setting("dispatch", {"paused": False})
        claimed = self.store.claim_task(task["id"], "runner-resumed", policy)
        self.assertEqual(claimed["status"], "running")

    def test_v1_database_migrates_without_losing_legacy_tasks_or_events(self):
        migration_project = self.project / "migration"
        migration_project.mkdir()
        state_dir = migration_project / ".orchestrator"
        state_dir.mkdir()
        database_path = state_dir / "state.sqlite3"
        legacy_task = {
            "id": "legacy-task", "title": "Legacy", "prompt": "Keep this", "role": "worker",
            "status": "completed", "depends_on": [], "parent_id": None, "model": None, "effort": None,
            "current_model": None, "current_effort": None, "thread_id": "thread-old", "turn_id": "turn-old",
            "result": "saved result", "error": None, "activity": "Done", "created_at": 1.0,
            "updated_at": 2.0, "run_id": "runner-old",
        }
        db = sqlite3.connect(database_path)
        try:
            with db:
                db.executescript("""
                    CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                    CREATE TABLE roles (name TEXT PRIMARY KEY, data TEXT NOT NULL);
                    CREATE TABLE settings (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                        task_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL);
                    CREATE TABLE agents (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                    CREATE TABLE requests (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                    CREATE TABLE controls (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                """)
                db.execute("INSERT INTO settings VALUES ('schema_version', '1')")
                db.execute("INSERT INTO tasks VALUES (?, ?)", (legacy_task["id"], json.dumps(legacy_task)))
                db.execute("INSERT INTO events(timestamp,task_id,kind,data) VALUES (1,?,?,?)",
                           (legacy_task["id"], "task/created", "{}"))
        finally:
            db.close()

        migrated = Store(migration_project)
        self.assertEqual(migrated.get_setting("schema_version"), 2)
        self.assertEqual(migrated.get_setting("dispatch"), {"paused": False})
        task = migrated.get_task("legacy-task")
        self.assertEqual(task["result"], "saved result")
        self.assertEqual(task["thread_id"], "thread-old")
        self.assertIsNone(task["workflow_id"])
        self.assertEqual([event["kind"] for event in migrated.events()], ["task/created"])
        submitted = migrated.submit_workflow({
            "version": 1, "name": "post-migration", "tasks": [
                {"key": "step", "title": "Step", "role": "worker", "prompt": "Do {goal}"},
            ],
        }, "Use migrated state")
        self.assertEqual(submitted["status"], "queued")

    def test_future_schema_is_rejected_before_any_schema_change(self):
        future_project = self.project / "future"
        future_project.mkdir()
        state_dir = future_project / ".orchestrator"
        state_dir.mkdir()
        database_path = state_dir / "state.sqlite3"
        db = sqlite3.connect(database_path)
        try:
            with db:
                db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
                db.execute("INSERT INTO settings VALUES ('schema_version', '99')")
        finally:
            db.close()
        before = sqlite3.connect(database_path)
        try:
            schema_before = before.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        finally:
            before.close()
        with self.assertRaisesRegex(ValueError, "Unsupported state schema 99"):
            Store(future_project)
        after = sqlite3.connect(database_path)
        try:
            self.assertEqual(after.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall(), schema_before)
        finally:
            after.close()


if __name__ == "__main__":
    unittest.main()
