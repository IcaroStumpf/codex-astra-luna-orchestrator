# Workflow definitions and execution

The [front-page walkthrough](../README.md#run-your-first-team-workflow) starts a
built-in team. This guide covers custom graphs and execution semantics.

## Definitions

`workflow templates` returns the complete built-in `feature`, `bugfix`, and
`review` definitions. Copy one to a UTF-8 JSON file, edit it, and submit the file
with `workflow submit PATH "Goal" --name LABEL`. Use `--dry-run` to validate the
expanded prompts and project roles before queuing anything. A preview initializes
project state if needed but creates no tasks or workflow execution.

| Definition field | Meaning |
|---|---|
| `version` | Required integer `1` for this definition format |
| `name` | Required nonempty template name, at most 64 characters |
| `description` | Optional description, at most 1,000 characters |
| `tasks` | Required list of 1–32 step objects |

| Step field | Meaning |
|---|---|
| `key` | Required unique lowercase key; letters, digits, underscores or hyphens; starts with a letter; at most 64 characters |
| `title` | Required nonempty title, at most 160 characters |
| `role` | Required existing project role |
| `prompt` | Required instructions; literal `{goal}` is replaced with the submitted goal |
| `depends_on` | Optional array of other keys in this same definition |
| `model` | Optional exact task model override |
| `effort` | Optional task reasoning-effort override |

Unknown fields, duplicate JSON keys, duplicate step keys, repeated dependencies,
missing dependencies and cycles are errors. Files are limited to 1 MiB; goals
to 20,000 characters; individual prompts to 16,000 characters and all prompts
combined to 128,000 characters. Prompt caps apply again after substitution.
No expressions, shell commands, or code execute during expansion. Other braces
are preserved as text.

Independent steps can have no dependency edge between them. A step with several
dependencies waits until all complete. Its input includes bounded result text
from those completed tasks. Keep the graph focused: final result text is capped
at 128 KiB per task and each dependency's prompt contribution at 12 KiB.

## Durable grouping

Submission stores the definition snapshot, goal, label, step-to-task mapping,
tasks and creation events in one transaction. An invalid graph queues nothing.
The returned workflow `id` is the stable lookup key; human-readable names may
repeat. `workflow list` reads executions, `workflow show ID` includes all steps,
and `watch --tree --workflow ID` narrows the terminal view.

The workflow ID is distinct from a runner ID. Restarting the runner does not
change membership. Changing a role's model affects future steps unless the
definition pinned a task override. A workflow definition records a plan, not a
snapshot of all role settings.

The scheduler serves the project's task pool. `serve --once` drains all runnable
project work, including other workflows and individual tasks. It never silently
creates extra workflow steps. Read-only tasks may overlap, but workspace-writing
tasks stay exclusive in the shared checkout; this is not worktree isolation.

## Failure, continuation and completion

A failed, interrupted, lost or cancelled dependency blocks downstream work.
Inspect that task and explicitly continue it when appropriate. Existing blocked
dependents are re-evaluated after the dependency is requeued or completed.
Completed downstream steps are not automatically invalidated or rerun if an
earlier completed step is later continued; submit a fresh workflow when the
entire result needs re-evaluation.

`workflow wait ID --timeout SECONDS` observes the existing runner. It returns
0 for completed managed turns, 1 for terminal incomplete/blocked work, 3 when
user input is required, 4 for a paused remaining queue, and 124 on timeout.
Timeout does not cancel work. A stopped runner will not progress a queued
workflow. The aggregated state may show a failure while another independent
step is still running; use the member states to inspect that distinction.

Workflow completion is not an automatic quality gate. A reviewer can finish
successfully while reporting defects. Feature and bugfix templates include a
bounded integration step that addresses findings and reports unresolved ones.
The review template stays read-only under the default role configuration.
The caller retains responsibility for accepting results and publishing changes.

## Plugin interface

MCP exposes `orchestrator_workflow_templates`, `orchestrator_submit_workflow`,
`orchestrator_workflows`, and `orchestrator_workflow`. Calls require the absolute
target `project`; `source` is a built-in name or an absolute JSON file path.
Submission returns immediately. A foreground runner still dispatches the work.

`orchestrator_status` and `orchestrator_usage` accept `workflow_id`, `task_id`,
`role`, `states`, and `active_only`. `orchestrator_report` accepts a workflow or
task ID and returns an evidence bundle without writing or publishing a file.
