import shutil
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "GPT6-SolMax-LunaMax": "max",
    "GPT6-SolMedium-LunaMax": "medium",
}
LUNA_ROLES = ("explorer", "researcher", "tester", "worker")
PREVIOUS_PROFILES = {
    "pro": ("gpt-6-astra", "medium", "max", 4),
    "plus": ("gpt-6-luna", "max", "medium", 4),
    "pro-max-2-subagents": ("gpt-6-astra", "medium", "max", 2),
    "plus-max-2-subagents": ("gpt-6-luna", "max", "medium", 2),
}


def installer_commands():
    """Locate the platform installers, including Git Bash on native Windows."""
    shell = shutil.which("sh")
    if shell is None and os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).parent.parent / "usr" / "bin" / "sh.exe"
            if candidate.is_file():
                shell = str(candidate)
    commands = [("shell", [shell, str(ROOT / "setup.sh")])] if shell else []
    if shutil.which("pwsh"):
        commands.append(("powershell", ["pwsh", "-NoProfile", "-File", str(ROOT / "setup.ps1")]))
    if not commands:
        raise unittest.SkipTest("No shell or PowerShell installer runtime is available")
    return commands


def run_installer(installer, command, target, choice):
    target_input = Path(target).as_posix() if installer == "shell" else target
    inputs = f"{target_input}\n{choice}\n\n\n\n"
    env = os.environ.copy()
    if installer == "shell":
        env["PATH"] = str(Path(command[0]).parent) + os.pathsep + env.get("PATH", "")
    # Bytes preserve LF on Windows; text=True would feed CRLF to POSIX read.
    result = subprocess.run(command, input=inputs.encode("utf-8"), capture_output=True, env=env, timeout=30)
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result


class SolProfileTests(unittest.TestCase):
    def test_profile_models_and_roles(self):
        for profile, effort in PROFILES.items():
            with self.subTest(profile=profile):
                directory = ROOT / "profiles" / profile
                config = tomllib.loads((directory / "codex" / "config.toml").read_text())
                self.assertEqual(config["model"], "gpt-6.1-sol")
                self.assertEqual(config["model_reasoning_effort"], effort)
                self.assertEqual(config["sandbox_mode"], "workspace-write")
                self.assertTrue(config["agents"]["enabled"])
                self.assertEqual(config["agents"]["default_subagent_model"], "gpt-6-luna")
                self.assertEqual(config["agents"]["default_subagent_reasoning_effort"], "max")
                self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], 4)
                for role in LUNA_ROLES:
                    agent = tomllib.loads(
                        (directory / "codex" / "agents" / f"{role}.toml").read_text()
                    )
                    self.assertEqual(agent["name"], role)
                    self.assertEqual(agent["model"], "gpt-6-luna")
                    self.assertEqual(agent["model_reasoning_effort"], "max")
                    expected_mode = "workspace-write" if role in ("worker", "tester") else "read-only"
                    self.assertEqual(agent["sandbox_mode"], expected_mode)
                reviewer = tomllib.loads(
                    (directory / "codex" / "agents" / "reviewer.toml").read_text()
                )
                self.assertEqual(reviewer["model"], "gpt-6.1-sol")
                self.assertEqual(reviewer["model_reasoning_effort"], effort)
                self.assertEqual(reviewer["sandbox_mode"], "read-only")
                skill = (
                    directory / "agents" / "skills" / "astra-orchestrator" / "SKILL.md"
                ).read_text()
                self.assertIn(f"root: `gpt-6.1-sol` at `{effort}` reasoning", skill)
                self.assertIn(
                    "explorer, worker, tester, researcher: `gpt-6-luna` at `max` reasoning",
                    skill,
                )

    def test_installers_select_profiles(self):
        installers = installer_commands()
        choices = (
            ("5", "GPT6-SolMax-LunaMax"),
            ("6", "GPT6-SolMedium-LunaMax"),
            ("GPT6-SolMax-LunaMax", "GPT6-SolMax-LunaMax"),
            ("GPT6-SolMedium-LunaMax", "GPT6-SolMedium-LunaMax"),
            ("gpt6-SolMax-LunaMax", "GPT6-SolMax-LunaMax"),
            ("gpt6-SolMedium-LunaMax", "GPT6-SolMedium-LunaMax"),
        )
        for installer, command in installers:
            for choice, profile in choices:
                with self.subTest(installer=installer, choice=choice), tempfile.TemporaryDirectory() as target:
                    result = run_installer(installer, command, target, choice)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Select Profile [1-6] (default 1):", result.stdout)
                    self.assertIn(f"profile: {profile}", result.stdout)
                    self.assertIn(f") {profile} -", result.stdout)
                    source = ROOT / "profiles" / profile
                    for component in (
                        Path(".codex/config.toml"),
                        Path(".codex/agents/reviewer.toml"),
                        Path(".agents/skills/astra-orchestrator/SKILL.md"),
                    ):
                        source_component = source / component.parts[0][1:]
                        self.assertEqual(
                            (Path(target) / component).read_text(),
                            (source_component / Path(*component.parts[1:])).read_text(),
                        )


class PreviousProfileTests(unittest.TestCase):
    def test_models_and_roles(self):
        for profile, (root_model, root_effort, luna_effort, limit) in PREVIOUS_PROFILES.items():
            with self.subTest(profile=profile):
                directory = ROOT / "profiles" / profile
                config = tomllib.loads((directory / "codex" / "config.toml").read_text())
                self.assertEqual(config["model"], root_model)
                self.assertEqual(config["model_reasoning_effort"], root_effort)
                self.assertEqual(config["agents"]["default_subagent_model"], "gpt-6-luna")
                self.assertEqual(config["agents"]["default_subagent_reasoning_effort"], luna_effort)
                self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], limit)
                for role in LUNA_ROLES:
                    agent = tomllib.loads(
                        (directory / "codex" / "agents" / f"{role}.toml").read_text()
                    )
                    self.assertEqual(agent["model"], "gpt-6-luna")
                    self.assertEqual(agent["model_reasoning_effort"], luna_effort)
                reviewer = tomllib.loads(
                    (directory / "codex" / "agents" / "reviewer.toml").read_text()
                )
                self.assertEqual(reviewer["model"], "gpt-6-astra")
                self.assertEqual(reviewer["model_reasoning_effort"], "low")
                skill = (
                    directory / "agents" / "skills" / "astra-orchestrator" / "SKILL.md"
                ).read_text()
                self.assertNotIn("gpt-5.6-luna", skill)
                self.assertNotIn("GPT-5.6 Luna", skill)
                self.assertIn(f"`gpt-6-luna` at `{luna_effort}` reasoning", skill)

    def test_installers_select_previous_profiles(self):
        installers = installer_commands()
        for installer, command in installers:
            for choice, profile in enumerate(PREVIOUS_PROFILES, start=1):
                with self.subTest(installer=installer, profile=profile), tempfile.TemporaryDirectory() as target:
                    result = run_installer(installer, command, target, choice)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"profile: {profile}", result.stdout)
                    menu_line = next(line for line in result.stdout.splitlines() if line.startswith(f"  {choice}) "))
                    self.assertIn("GPT-6 Luna", menu_line)
                    source = ROOT / "profiles" / profile
                    for component in (
                        Path(".codex/config.toml"),
                        Path(".codex/agents/worker.toml"),
                        Path(".agents/skills/astra-orchestrator/SKILL.md"),
                    ):
                        source_component = source / component.parts[0][1:]
                        self.assertEqual(
                            (Path(target) / component).read_text(),
                            (source_component / Path(*component.parts[1:])).read_text(),
                        )


if __name__ == "__main__":
    unittest.main()
