---
name: orchestrate
description: Use Codex Orchestrator for multi-step work that needs durable tasks, dependencies, role routing, progress visibility, or resumption; use native Codex delegation for small bounded tasks.
---

# Codex Orchestrator

Keep Codex as the executor. Choose native child-agent delegation for a small, clear task. Use managed tasks when work needs durable status, dependencies, role or model selection, event visibility, or continuation across turns. Stay within the user's requested scope; the parent owns integration and final verification.

## Start and inspect work

- The MCP tools require the absolute project path on every request. Pass the same target repository path to every `orchestrator_*` call; do not infer it from the plugin checkout or current working directory.
- Read the current role catalog with `orchestrator_roles` before assigning work. Role defaults are live configuration; do not copy model IDs or reasoning defaults from this skill. Use `codex-orchestrator --project <absolute-project-path> models` to inspect the current model catalog before choosing a model.
- Turn a large request into small, independently reviewable tasks. Use `orchestrator_add_task` with a focused prompt and role; its MCP fields are `depends_on` and `parent_id`. The equivalent CLI flags are `--after` and `--parent`. Start queued work with `codex-orchestrator --project <absolute-project-path> serve --once` when execution is intended.
- Use `orchestrator_status` to see current task state and `orchestrator_task` for a task's detail. Use `codex-orchestrator --project <absolute-project-path> events --task <task-id>` when its activity needs investigation. Use `orchestrator_continue_task` to give a managed task new instructions. Use `orchestrator_interrupt_task` when the user asks to stop it or the task must be stopped to respect a scope change.
- Installing the plugin does not install the CLI package. When the user asks to set up the runtime, install the package separately with the selected absolute Python 3.11+ interpreter. Otherwise, if the CLI or MCP server is unavailable, report the limitation; if task creation succeeded but no foreground runner is active, say that the task is queued and needs `codex-orchestrator --project <absolute-project-path> serve`. Never configure Codex hooks automatically.

## Role and model changes

- `orchestrator_set_model` updates a role default for future dispatch when given `role`, or a task's model policy when given `task_id`. Model IDs are supplied by the current model catalog; avoid hard-coded fallbacks.
- Select models at task creation or dispatch. Changing a role default does not change a running task. Only when the user asks to change an active managed task, inspect its status and make a targeted live request with `orchestrator_set_model` (`task_id` plus `live: true`) or `codex-orchestrator --project <absolute-project-path> task model <task-id> <model-id> --live`.
- Active-turn publication requires a runner started with `serve --live-models`, which enables Codex's experimental `step_model_switching` feature only for that process. Without it, use next-turn changes or explain that the live capability is disabled.
- Report live-model results precisely. `applied` means the setting was published for subsequent captures; it does not change already-captured steps or promise that another inference will occur. A target-unavailable or unsupported-RPC result leaves the new setting for a later turn. Codex can reject a model that changes already-admitted tool/review requirements; report that error without silently retrying other models. A live request applies only to the managed task's primary session; it does not change child sessions or the host's current Codex session.
- Pending approvals are user decisions. You may inspect them with `codex-orchestrator --project <absolute-project-path> approvals`, but do not accept, decline, or cancel an approval on the user's behalf. Ask the user to respond with `codex-orchestrator --project <absolute-project-path> respond <approval-id> --decision accept|decline|cancel` or the applicable `--answers` form.

## Complete the task

Keep each worker on its assigned scope and have it report concrete evidence. Inspect task status and results before integrating. The parent integrates changes, resolves conflicts, and verifies the requested outcome; a worker's completion claim alone is not proof that the whole request is done.
