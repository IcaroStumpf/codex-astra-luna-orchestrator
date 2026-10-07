"""Reusable, validated workflow definitions for project task graphs.

Definitions are data only: they describe bounded task prompts and dependencies,
while the existing runner remains responsible for executing each Codex turn.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .roles import EFFORTS, validate_model

SCHEMA_VERSION = 1
MAX_DEFINITION_BYTES = 1024 * 1024
MAX_TASKS = 32
MAX_GOAL_CHARS = 12_000
MAX_PROMPT_CHARS = 16_000
MAX_TOTAL_PROMPT_CHARS = 128_000
MAX_DESCRIPTION_CHARS = 1_000
MAX_TITLE_CHARS = 160
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


_TEMPLATES: dict[str, dict[str, Any]] = {
    "feature": {
        "version": SCHEMA_VERSION,
        "name": "feature",
        "description": "Explore, implement, validate, review, then integrate a bounded feature change.",
        "tasks": [
            {
                "key": "explore",
                "title": "Explore the feature request",
                "role": "explorer",
                "prompt": (
                    "Inspect the repository for the requested feature. Identify relevant behavior, "
                    "files, constraints, and a bounded implementation path. Do not edit files.\n\n"
                    "User goal: {goal}"
                ),
            },
            {
                "key": "implement",
                "title": "Implement the feature",
                "role": "worker",
                "depends_on": ["explore"],
                "prompt": (
                    "Implement the requested feature using the exploration evidence. Keep the change "
                    "within the stated scope, preserve unrelated work, and report changed files plus "
                    "meaningful checks and remaining risks. Do not commit, publish, or make remote changes.\n\n"
                    "User goal: {goal}"
                ),
            },
            {
                "key": "verify",
                "title": "Validate the feature",
                "role": "tester",
                "depends_on": ["implement"],
                "prompt": (
                    "Validate the implemented feature with focused checks. Fix only test or validation "
                    "issues directly caused by this change and report what passed, failed, or was unavailable.\n\n"
                    "User goal: {goal}"
                ),
            },
            {
                "key": "review",
                "title": "Review the feature change",
                "role": "reviewer",
                "depends_on": ["verify"],
                "prompt": (
                    "Independently review the resulting change for correctness, regressions, and missing "
                    "coverage. Do not edit. Report actionable findings with file references, or state that "
                    "you found none.\n\nUser goal: {goal}"
                ),
            },
            {
                "key": "integrate",
                "title": "Integrate review findings",
                "role": "orchestrator",
                "depends_on": ["implement", "verify", "review"],
                "prompt": (
                    "Inspect the implementation, validation, and independent review results. Address any "
                    "actionable review finding within the requested scope, rerun focused checks when needed, "
                    "and give a concise final handoff. If a finding should remain open, explain why. Do not "
                    "commit, publish, or make remote changes. This workflow records turn completion; it does "
                    "not certify the overall goal automatically.\n\nUser goal: {goal}"
                ),
            },
        ],
    },
    "bugfix": {
        "version": SCHEMA_VERSION,
        "name": "bugfix",
        "description": "Trace a defect, fix and verify it, review the change, then integrate findings.",
        "tasks": [
            {
                "key": "trace",
                "title": "Trace the reported defect",
                "role": "explorer",
                "prompt": (
                    "Trace the reported defect through the repository. Find the likely cause, relevant "
                    "paths, and a focused way to reproduce or verify it. Do not edit files.\n\n"
                    "User goal: {goal}"
                ),
            },
            {
                "key": "fix",
                "title": "Fix the defect",
                "role": "debugger",
                "depends_on": ["trace"],
                "prompt": (
                    "Reproduce or otherwise confirm the defect when practical, implement a focused fix, "
                    "and report the root cause and checks. Preserve unrelated work. Do not commit, publish, "
                    "or make remote changes.\n\nUser goal: {goal}"
                ),
            },
            {
                "key": "verify",
                "title": "Verify the defect fix",
                "role": "tester",
                "depends_on": ["fix"],
                "prompt": (
                    "Run focused checks for the reported defect and likely regressions. Fix only directly "
                    "related test/validation issues; distinguish passing, failing, and unavailable checks.\n\n"
                    "User goal: {goal}"
                ),
            },
            {
                "key": "review",
                "title": "Review the defect fix",
                "role": "reviewer",
                "depends_on": ["verify"],
                "prompt": (
                    "Independently review the defect fix for correctness and regressions. Do not edit. "
                    "Report actionable findings with file references, or state that you found none.\n\n"
                    "User goal: {goal}"
                ),
            },
            {
                "key": "integrate",
                "title": "Integrate review findings",
                "role": "orchestrator",
                "depends_on": ["fix", "verify", "review"],
                "prompt": (
                    "Inspect the fix, verification, and independent review results. Address actionable "
                    "review findings within scope and rerun focused checks when needed. Provide a concise "
                    "final handoff, explaining any open finding. Do not commit, publish, or make remote "
                    "changes. Workflow completion records task turns; it does not certify the goal.\n\n"
                    "User goal: {goal}"
                ),
            },
        ],
    },
    "review": {
        "version": SCHEMA_VERSION,
        "name": "review",
        "description": "Inspect a requested change and return an independent, actionable review report.",
        "tasks": [
            {
                "key": "map",
                "title": "Map the requested change",
                "role": "explorer",
                "prompt": (
                    "Inspect the repository and identify the change or areas covered by this review. "
                    "Report relevant paths and observable behavior without editing files.\n\n"
                    "Review request: {goal}"
                ),
            },
            {
                "key": "review",
                "title": "Review correctness and regressions",
                "role": "reviewer",
                "depends_on": ["map"],
                "prompt": (
                    "Independently review the requested change using the repository evidence. Do not edit. "
                    "Prioritize actionable correctness and regression findings; include severity and file/line "
                    "references, or state that you found none.\n\nReview request: {goal}"
                ),
            },
            {
                "key": "report",
                "title": "Consolidate the review report",
                "role": "reviewer",
                "depends_on": ["review"],
                "prompt": (
                    "Consolidate the repository map and independent review into one concise final report. "
                    "Preserve every actionable finding with its severity and file reference, and state when "
                    "no finding was confirmed. Do not edit files, commit, publish, or make remote changes.\n\n"
                    "Review request: {goal}"
                ),
            },
        ],
    },
}


def templates() -> list[dict[str, Any]]:
    """Return fresh copies of the built-in workflow definitions."""

    return [validate_definition(_TEMPLATES[key]) for key in sorted(_TEMPLATES)]


def load_definition(source: str) -> dict[str, Any]:
    """Load a built-in definition by name or a JSON definition from a path."""

    if not isinstance(source, str) or not source.strip():
        raise ValueError("Workflow source must be a template name or JSON file path.")
    key = source.strip().lower()
    if key in _TEMPLATES:
        return validate_definition(_TEMPLATES[key])

    path = Path(source).expanduser()
    try:
        if not path.is_file():
            raise ValueError(f"Workflow definition file does not exist: {path}")
        with path.open("rb") as source_file:
            content_bytes = source_file.read(MAX_DEFINITION_BYTES + 1)
        if len(content_bytes) > MAX_DEFINITION_BYTES:
            raise ValueError(f"Workflow definition exceeds {MAX_DEFINITION_BYTES} bytes.")
        content = content_bytes.decode("utf-8")
        definition = json.loads(content, object_pairs_hook=_unique_object_pairs)
    except UnicodeDecodeError as exc:
        raise ValueError("Workflow definition must be UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid workflow JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}.") from exc
    except RecursionError as exc:
        raise ValueError("Workflow JSON nesting is too deep.") from exc
    if not isinstance(definition, dict):
        raise ValueError("Workflow definition must be a JSON object.")
    return validate_definition(definition)


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def validate_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a version 1 acyclic workflow definition.

    Role existence is deliberately checked by Store at submission time so
    definitions stay independent of any particular project's role catalog.
    """

    if not isinstance(definition, dict):
        raise ValueError("Workflow definition must be an object.")
    if any(not isinstance(key, str) for key in definition):
        raise ValueError("Workflow definition field names must be strings.")
    allowed = {"version", "name", "description", "tasks"}
    unexpected = set(definition) - allowed
    missing = {"version", "name", "tasks"} - set(definition)
    if unexpected:
        raise ValueError(f"Unexpected workflow fields: {', '.join(sorted(unexpected))}.")
    if missing:
        raise ValueError(f"Missing workflow fields: {', '.join(sorted(missing))}.")
    if type(definition["version"]) is not int or definition["version"] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported workflow definition version; expected {SCHEMA_VERSION}.")
    name = definition["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("Workflow name must contain 1 to 64 characters.")
    description = definition.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"Workflow description must be a string of at most {MAX_DESCRIPTION_CHARS} characters.")
    task_defs = definition["tasks"]
    if not isinstance(task_defs, list) or not 1 <= len(task_defs) <= MAX_TASKS:
        raise ValueError(f"Workflow must contain between 1 and {MAX_TASKS} tasks.")

    normalized_tasks: list[dict[str, Any]] = []
    keys: set[str] = set()
    total_prompt_chars = 0
    task_allowed = {"key", "title", "role", "prompt", "depends_on", "model", "effort"}
    for index, raw in enumerate(task_defs):
        if not isinstance(raw, dict):
            raise ValueError(f"Workflow task at index {index} must be an object.")
        if any(not isinstance(field, str) for field in raw):
            raise ValueError(f"Workflow task field names at index {index} must be strings.")
        unexpected_task = set(raw) - task_allowed
        missing_task = {"key", "title", "role", "prompt"} - set(raw)
        if unexpected_task:
            raise ValueError(f"Unexpected fields in workflow task {index}: {', '.join(sorted(unexpected_task))}.")
        if missing_task:
            raise ValueError(f"Missing fields in workflow task {index}: {', '.join(sorted(missing_task))}.")
        key = raw["key"]
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError(f"Workflow task key at index {index} must match {_KEY.pattern}.")
        if key in keys:
            raise ValueError(f"Duplicate workflow task key: {key}")
        keys.add(key)
        title = raw["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
            raise ValueError(f"Title for workflow task {key} must contain 1 to {MAX_TITLE_CHARS} characters.")
        role = raw["role"]
        if not isinstance(role, str) or not _KEY.fullmatch(role):
            raise ValueError(f"Role for workflow task {key} must be a valid lowercase role name.")
        prompt = raw["prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"Prompt for workflow task {key} must contain 1 to {MAX_PROMPT_CHARS} characters.")
        total_prompt_chars += len(prompt)
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(isinstance(dep, str) and dep for dep in dependencies):
            raise ValueError(f"depends_on for workflow task {key} must be a list of task keys.")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"Workflow task {key} has duplicate dependencies.")
        model = raw.get("model")
        if "model" in raw:
            validate_model(model)
        effort = raw.get("effort")
        if "effort" in raw and effort not in EFFORTS:
            raise ValueError(f"Unsupported reasoning effort for workflow task {key}.")
        normalized = {
            "key": key,
            "title": title.strip(),
            "role": role,
            "prompt": prompt.strip(),
            "depends_on": list(dependencies),
        }
        if model is not None:
            normalized["model"] = model
        if effort is not None:
            normalized["effort"] = effort
        normalized_tasks.append(normalized)

    if total_prompt_chars > MAX_TOTAL_PROMPT_CHARS:
        raise ValueError(f"Workflow task prompts exceed {MAX_TOTAL_PROMPT_CHARS} characters in total.")

    for task in normalized_tasks:
        for dependency in task["depends_on"]:
            if dependency not in keys:
                raise ValueError(f"Workflow task {task['key']} depends on unknown task key: {dependency}")
            if dependency == task["key"]:
                raise ValueError(f"Workflow task {task['key']} cannot depend on itself.")
    _ensure_acyclic(normalized_tasks)
    return {
        "version": SCHEMA_VERSION,
        "name": name.strip(),
        "description": description.strip(),
        "tasks": normalized_tasks,
    }


def _ensure_acyclic(tasks: list[dict[str, Any]]) -> None:
    by_key = {task["key"]: task for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError(f"Workflow contains a dependency cycle involving task {key}.")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key]["depends_on"]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for task in tasks:
        visit(task["key"])


def expand_workflow(definition: dict[str, Any], goal: str) -> dict[str, Any]:
    """Return a validated definition whose task prompts include the user goal."""

    normalized = validate_definition(definition)
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
        raise ValueError(f"Workflow goal must contain 1 to {MAX_GOAL_CHARS} characters.")
    goal = goal.strip()
    expanded = deepcopy(normalized)
    total_prompt_chars = 0
    for task in expanded["tasks"]:
        task["prompt"] = task["prompt"].replace("{goal}", goal)
        if len(task["prompt"]) > MAX_PROMPT_CHARS:
            raise ValueError(f"Expanded prompt for workflow task {task['key']} exceeds {MAX_PROMPT_CHARS} characters.")
        total_prompt_chars += len(task["prompt"])
    if total_prompt_chars > MAX_TOTAL_PROMPT_CHARS:
        raise ValueError(f"Expanded workflow prompts exceed {MAX_TOTAL_PROMPT_CHARS} characters in total.")
    return expanded
