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

## Workflows, visibility and steering

- Inspect `orchestrator_workflow_templates` before choosing a reusable feature, bugfix or review graph. Use `orchestrator_submit_workflow` with a concrete goal and constraints when the graph fits; use individual tasks for custom decomposition. Custom workflow JSON requires an absolute file path in the MCP `source`. Submission is atomic and returns a workflow ID; it queues work and does not start a runner.
- Read the entire workflow with `orchestrator_workflow`, or filter `orchestrator_status` by `workflow_id`, `task_id`, role or states. CLI `watch --tree --workflow ID` shows the managed/native relationship. Native agents are observations from Codex; missing model/usage fields are unknown, not proof of a particular model or zero consumption.
- Use `orchestrator_steer_task` for additional instructions to an active turn. Inspect its returned control and the task's events for delivery. A rejected, expired or uncertain steering request is not a new queued turn; do not silently replay it. Use explicit continuation after a terminal task when that is the intended next step.
- `orchestrator_dispatch` with `paused: true` stops new claims while active work, approvals and interruption continue. Resume only when that matches the user's intent. A pause persists across runner restarts; it does not stop current inference.
- `orchestrator_usage` separates reported managed and native thread tokens. Do not sum those into a billing claim or attribute cumulative thread usage to its latest model. `orchestrator_report` returns local evidence and results without writing or sending a file. The CLI exports JSON or Markdown with `report --output PATH`.
- For shell automation, `task wait ID` and `workflow wait ID` observe the separate runner: exit 0 means all selected managed turns completed, 1 incomplete/failed, 3 pending user input, 4 paused queue, and 124 timeout. Avoid unbounded polling and do not answer approvals to make a wait complete.

## Role and model changes

- `orchestrator_set_model` updates a role default for future dispatch when given `role`, or a task's model policy when given `task_id`. Model IDs are supplied by the current model catalog; avoid hard-coded fallbacks.
- Select models at task creation or dispatch. Changing a role default does not change a running task. Only when the user asks to change an active managed task, inspect its status and make a targeted live request with `orchestrator_set_model` (`task_id` plus `live: true`) or `codex-orchestrator --project <absolute-project-path> task model <task-id> <model-id> --live`.
- Active-turn publication requires a runner started with `serve --live-models`, which enables Codex's experimental `step_model_switching` feature only for that process. Without it, use next-turn changes or explain that the live capability is disabled.
- Report live-model results precisely. `applied` means the setting was published for subsequent captures; it does not change already-captured steps or promise that another inference will occur. A target-unavailable or unsupported-RPC result leaves the new setting for a later turn. Codex can reject a model that changes already-admitted tool/review requirements; report that error without silently retrying other models. A live request applies only to the managed task's primary session; it does not change child sessions or the host's current Codex session.
- Pending approvals are user decisions. You may inspect them with `codex-orchestrator --project <absolute-project-path> approvals`, but do not accept, decline, or cancel an approval on the user's behalf. Ask the user to respond with `codex-orchestrator --project <absolute-project-path> respond <approval-id> --decision accept|decline|cancel` or the applicable `--answers` form.

## Complete the task

Keep each worker on its assigned scope and have it report concrete evidence. Inspect task status and results before integrating. The parent integrates changes, resolves conflicts, and verifies the requested outcome; a worker's completion claim alone is not proof that the whole request is done.
