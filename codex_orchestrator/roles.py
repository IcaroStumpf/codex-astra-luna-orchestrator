"""Role responsibilities are independent from the model assigned to them."""

from __future__ import annotations

import re

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ROLE_SPECS = {
    "orchestrator": ("Coordinate and integrate bounded tasks", "workspace-write",
                     "Own decomposition, integration and final verification. Delegate only independent bounded work."),
    "explorer": ("Map repository code and behavior", "read-only",
                 "Inspect the repository without edits. Return relevant paths, evidence, and implementation boundaries."),
    "worker": ("Implement a bounded change", "workspace-write",
               "Implement only the assigned change. Preserve unrelated work and report validation and remaining risks."),
    "tester": ("Reproduce and validate behavior", "workspace-write",
               "Run meaningful targeted verification. Distinguish failed checks, unavailable checks, and passing evidence."),
    "reviewer": ("Independently review correctness", "read-only",
                 "Review without editing. Prioritize actionable correctness and regression findings with file references."),
    "researcher": ("Verify external technical facts", "read-only",
                   "Use authoritative primary sources for current facts. Return source links and compatibility implications."),
    "architect": ("Resolve design boundaries and tradeoffs", "read-only",
                  "Inspect constraints and propose a concrete design with tradeoffs and migration implications. Do not edit."),
    "debugger": ("Reproduce and fix a scoped defect", "workspace-write",
                 "Trace the failure, reproduce it when possible, implement a focused fix and verify the original failure path."),
    "documenter": ("Document verified behavior", "workspace-write",
                   "Write concise documentation based on inspected behavior. Verify examples and avoid unsupported promises."),
}


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model):
        raise ValueError("Model must be a nonempty model ID without whitespace.")
    return model


def validate_role(role: dict) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", role.get("name", "")):
        raise ValueError("Role name must use lowercase letters, digits, underscores or hyphens.")
    validate_model(role.get("model", ""))
    if role.get("effort") not in EFFORTS:
        raise ValueError(f"Effort must be one of: {', '.join(EFFORTS)}")
    if role.get("sandbox") not in ("read-only", "workspace-write"):
        raise ValueError("Role sandbox must be read-only or workspace-write.")
    if not isinstance(role.get("instructions"), str) or not role["instructions"].strip():
        raise ValueError("Role instructions must not be empty.")
    return role


def default_roles() -> list[dict]:
    roles = []
    for name, (description, sandbox, instructions) in ROLE_SPECS.items():
        model = "gpt-6-astra" if name in ("orchestrator", "reviewer", "architect") else "gpt-6-luna"
        effort = "low" if name == "reviewer" else "medium" if name in ("orchestrator", "architect") else "max"
        roles.append(dict(name=name, description=description, sandbox=sandbox,
                          instructions=instructions, model=model, effort=effort))
    return roles
