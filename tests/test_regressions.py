import tempfile
import unittest
from pathlib import Path

from codex_orchestrator.cli import import_profile
from codex_orchestrator.store import Store


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.store = Store(self.project)

    def test_explicit_zero_cursor_starts_at_first_event_and_default_tails(self):
        for index in range(5):
            self.store.event(None, "sample", {"index": index})
        self.assertEqual([e["id"] for e in self.store.events(after=0, limit=2)], [1, 2])
        self.assertEqual([e["id"] for e in self.store.events(limit=2)], [4, 5])
        self.assertEqual([e["id"] for e in self.store.events(after=2, limit=2)], [3, 4])

    def test_profile_import_resolves_explicit_role_config_relative_to_config(self):
        config_dir = self.project / "profile"
        config_dir.mkdir()
        config = config_dir / "config.toml"
        config.write_text('[agents.worker]\nconfig_file = "custom-worker.toml"\n', encoding="utf-8")
        (config_dir / "custom-worker.toml").write_text(
            'model = "custom-model"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
        import_profile(self.store, config)
        role = self.store.get_role("worker")
        self.assertEqual((role["model"], role["effort"]), ("custom-model", "high"))

    def test_invalid_profile_does_not_partially_replace_model_policy(self):
        config = self.project / "config.toml"
        config.write_text('model = "replacement"\n[agents.worker]\nconfig_file = "missing.toml"\n', encoding="utf-8")
        before = self.store.roles()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            import_profile(self.store, config)
        self.assertEqual(self.store.roles(), before)

    def test_late_dependency_observation_cannot_revive_cancelled_task(self):
        task = self.store.add_task("Work")
        self.store.interrupt_task(task["id"])
        self.assertIsNone(self.store.update_task_if(task["id"], {"status": "queued"}, status="blocked"))
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
