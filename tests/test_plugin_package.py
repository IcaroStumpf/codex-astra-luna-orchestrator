import json
import tomllib
import unittest
from pathlib import Path
from codex_orchestrator import __version__


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PluginPackageTests(unittest.TestCase):
    def test_manifests_match_the_python_distribution(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        portable = read_json(ROOT / "plugin.json")
        codex = read_json(ROOT / ".codex-plugin" / "plugin.json")

        self.assertEqual(project["name"], "codex-orchestrator")
        self.assertEqual(project["version"], __version__)
        self.assertEqual(project["scripts"]["codex-orchestrator"], "codex_orchestrator.cli:main")
        self.assertEqual((portable["name"], portable["version"]), (project["name"], project["version"]))
        self.assertEqual((codex["name"], codex["version"]), (portable["name"], portable["version"]))
        self.assertEqual(portable["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(codex["mcpServers"], "./.mcp.json")
        self.assertEqual(portable["license"], project["license"]["text"])
        repository = "https://github.com/IcaroStumpf/codex-astra-luna-orchestrator"
        for manifest in (portable, codex):
            interface = manifest.get("extensions", {}).get("com.openai", {}).get("interface", manifest.get("interface", {}))
            self.assertEqual(manifest["author"]["name"], "IcaroStumpf")
            self.assertEqual(manifest["homepage"], repository)
            self.assertEqual(manifest["repository"], repository)
            self.assertEqual(interface["developerName"], "IcaroStumpf")
            self.assertEqual(interface["websiteURL"], repository)

    def test_portable_and_codex_mcp_configs_launch_the_same_stdio_server(self):
        portable = read_json(ROOT / "mcp.json")
        codex = read_json(ROOT / ".mcp.json")
        portable_server = portable["mcpServers"]["codex-orchestrator"]
        codex_server = codex["mcpServers"]["codex-orchestrator"]

        self.assertEqual(portable["$schema"], "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json")
        self.assertEqual(portable_server["type"], "stdio")
        self.assertEqual(portable_server["command"], codex_server["command"])
        self.assertEqual(portable_server["args"], codex_server["args"])
        self.assertEqual(codex_server["command"], "codex-orchestrator")
        self.assertEqual(codex_server["args"], ["mcp"])

    def test_repo_marketplace_resolves_to_the_plugin_root(self):
        marketplace = read_json(ROOT / ".agents" / "plugins" / "marketplace.json")
        entry = marketplace["plugins"][0]
        plugin_root = (ROOT / entry["source"]["path"]).resolve()
        portable = read_json(ROOT / "plugin.json")

        self.assertEqual(entry["source"]["source"], "local")
        self.assertEqual(plugin_root, ROOT.resolve())
        self.assertEqual(entry["name"], portable["name"])
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertIn("authentication", entry["policy"])

    def test_plugin_exposes_the_skill_without_declaring_hooks(self):
        portable = read_json(ROOT / "plugin.json")
        codex = read_json(ROOT / ".codex-plugin" / "plugin.json")
        skill = ROOT / "skills" / "orchestrate" / "SKILL.md"
        portable_openai = portable["extensions"]["com.openai"]

        self.assertTrue(skill.is_file())
        self.assertNotIn("hooks", portable_openai)
        self.assertNotIn("hooks", codex)
        self.assertEqual(portable_openai["interface"]["displayName"], codex["interface"]["displayName"])


if __name__ == "__main__":
    unittest.main()
