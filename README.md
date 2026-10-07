# Codex Orchestrator

Run visible, resumable agent teams from your terminal or a Codex plugin. Queue
a feature, bugfix, or review workflow; watch each task and native subagent;
redirect active work; inspect results and export a report.

The runtime uses your installed, signed-in Codex CLI, with project-local state
and no third-party Python runtime dependencies. This fork extends
[donvito's profile installer](https://github.com/donvito/codex-astra-luna-orchestrator)
with ideas from [Agent Orchestrator and oh-my-codex](guides/reference-projects.md).

[Install](#install-or-upgrade) · [First workflow](#run-your-first-team-workflow) ·
[Visibility](#see-what-everyone-is-doing) · [Controls](#control-work-in-progress) ·
[Models](#choose-and-change-models) · [Plugin](#use-it-from-codex) ·
[Custom workflows](#create-your-own-workflow) · [Original profiles](guides/profile-installer.md)

## What you can do

| Capability | How to use it |
|---|---|
| Reusable teams | Submit a `feature`, `bugfix`, or `review` workflow with your goal |
| Custom task graphs | Submit JSON with roles, dependencies, and optional model overrides |
| Durable tasks | Queue, interrupt, and continue a task on its saved Codex thread |
| Live visibility | `watch --tree`, filtered by workflow, role, task, or state |
| Native subagents | `agents` shows observed parent/child activity alongside managed tasks |
| Active guidance | `task steer ID "Correction"` adds instructions to the current turn |
| Dispatch control | `queue pause` / `queue resume` control admission of new work |
| Model policy | Change role defaults or one task's next-turn model and reasoning effort |
| Live model publication | Opt in with `serve --live-models`, then `task model ... --live` |
| Usage and reports | Inspect reported thread tokens; export results and history as JSON or Markdown |
| Shell automation | `task wait` / `workflow wait` return completion and input-needed exit codes |
| Codex plugin | `$orchestrate` and MCP tools share the same local task state |

Requires **Python 3.11+**. Windows, macOS, and Linux are covered by CI with
Python 3.11 and 3.14. **Mid-turn model publication is experimental in Codex**;
ordinary task execution, steering, and next-turn model changes do not require
that feature flag. [Details below](#why-is-live-model-switching-experimental).

## Install or upgrade

Install Codex and sign in with `codex login`. Clone this repository, then install
with the **absolute path** of your chosen Python interpreter:

```powershell
git clone https://github.com/IcaroStumpf/codex-astra-luna-orchestrator.git
cd codex-astra-luna-orchestrator
$Interpreter = 'C:\absolute\path\to\python.exe'
& $Interpreter -m pip install .
```

On macOS/Linux, use `/absolute/path/to/python -m pip install .`. Put that
interpreter's `Scripts`/`bin` directory on PATH so both the terminal and Codex can
launch `codex-orchestrator`. From the checkout, you can also use
`& $Interpreter -m codex_orchestrator --help` without installation.

Upgrade by pulling this checkout and reinstalling:

```powershell
git pull --ff-only
& $Interpreter -m pip install --upgrade .
```

Stop the old runner before upgrading, then restart it. Version 0.4 upgrades
the local state schema when opened and preserves existing tasks; older runtime
versions cannot read the upgraded schema.

The original `setup.sh` / `setup.ps1` installers configure Codex profiles. The
Python installation above installs the new runtime. Both are available; see
the [profile installer guide](guides/profile-installer.md).

## Run your first team workflow

Enter the repository you want agents to work on. Commands default to that
directory; alternatively put `--project /absolute/project` before the command.
On Windows, use a path such as `--project 'D:\Projects\my-app'`.

```text
codex-orchestrator init
codex-orchestrator doctor
codex-orchestrator workflow templates
codex-orchestrator workflow submit feature "Add CSV export to the invoice list; preserve authorization; test empty and large lists" --name invoice-export
```

Submission returns a workflow `id`, its step-to-task mapping, and queued tasks.
Copy that ID wherever the examples use `WORKFLOW_ID`. The whole graph is
validated and queued atomically; submission does not start inference. Add
`--dry-run` to preview it without queuing tasks.

Start the runner in that terminal:

```text
codex-orchestrator serve
```

In a second terminal, in the same target repository:

```text
codex-orchestrator watch --tree --workflow WORKFLOW_ID
codex-orchestrator workflow show WORKFLOW_ID
codex-orchestrator workflow wait WORKFLOW_ID --timeout 600
codex-orchestrator report --workflow WORKFLOW_ID --output invoice-export.md
```

`serve` stays available for new work. `serve --once` drains runnable work across
the **whole project**, not only one workflow. Read-only tasks can overlap up to
`--workers N` (default 2); workspace-writing tasks run exclusively in the shared
checkout.

| Template | Steps |
|---|---|
| `feature` | Explore → implement → validate → review → integrate findings |
| `bugfix` | Trace → fix → verify → review → integrate findings |
| `review` | Map the change → independent review → consolidate report |

The final integration step addresses findings within scope and reports open
issues. Task completion records completed Codex turns; read the final evidence
before accepting the outcome or publishing changes. Workflow templates do not
automatically commit or push.

## See what everyone is doing

```text
codex-orchestrator status
codex-orchestrator watch --tree
codex-orchestrator agents --task TASK_ID
codex-orchestrator status --role reviewer --state running
codex-orchestrator watch --workflow WORKFLOW_ID --active
codex-orchestrator task show TASK_ID
codex-orchestrator events --task TASK_ID
codex-orchestrator workflow list
```

A **managed task** is a runtime assignment backed by a Codex thread. A **native
subagent** is a child Codex creates inside that thread. The tree shows their
relationships and observed activity; it does not attach to unrelated desktop
or CLI sessions. A dependency means *wait for this task's result*;
`task add --parent ID` records organization only.

The board exposes state, activity, errors, current/pending model settings, and
pending requests. Missing native model/usage fields remain unknown.
`status --json` supports scripts; `watch --json` emits one snapshot per line.
Repeat `--state` for several states. Combine `--workflow`, `--task`, and `--role`
to narrow a view.

For a single assignment, build your own task chain:

```text
codex-orchestrator task add "Map the login flow" --role explorer
codex-orchestrator task add "Review the findings" --role reviewer --after TASK_ID
```

`task add` prints the new task ID. Repeat `--after` for several dependencies;
the runner supplies completed dependency results to the downstream task.

## Control work in progress

```text
codex-orchestrator task steer TASK_ID "Keep the change inside the export module; do not refactor authentication"
codex-orchestrator queue pause
codex-orchestrator queue status
codex-orchestrator queue resume
codex-orchestrator task interrupt TASK_ID
codex-orchestrator task continue TASK_ID "Continue with the corrected requirement and rerun the regression check"
```

Steering adds input to the active turn. Check its delivery in `events`; a queued
request is not proof of acceptance. If that turn already ended, use an explicit
continuation. Steering does not change model policy.

Pausing prevents new task claims and persists across runner restarts. Active
work, approvals, and interruption continue. Use `task interrupt` to stop a turn.
Wait for a terminal state before continuing; continuation reuses the saved
thread and resolves current model policy. Cancelling queued work does not start
Codex.

## Choose and change models

Responsibilities and model choices are separate:

| Role | Responsibility | Workspace access |
|---|---|---|
| `orchestrator` | Decomposition, integration, final verification | Write |
| `explorer` | Map code and behavior | Read-only |
| `worker` | Implement a bounded change | Write |
| `tester` | Reproduce and validate | Write |
| `reviewer` | Independent correctness review | Read-only |
| `researcher` | Verify technical facts from primary sources | Read-only |
| `architect` | Design boundaries and tradeoffs | Read-only |
| `debugger` | Trace, reproduce, fix, verify | Write |
| `documenter` | Document verified behavior | Write |

```text
codex-orchestrator roles
codex-orchestrator models
codex-orchestrator model set worker gpt-6-luna --effort medium
codex-orchestrator task model TASK_ID gpt-6.1-sol --effort medium
codex-orchestrator role add security-reviewer --model gpt-6-astra --effort low --sandbox read-only --instructions "Review concrete security regressions; report evidence without editing"
```

Use IDs from your installed model catalog; listing does not prove account
access. Task overrides win over role defaults. Role changes affect future
dispatches; ordinary `task model` affects the next turn. Import an existing
profile's choices with `init --config /absolute/project/.codex/config.toml`;
it does not rewrite that profile or its permissions.

### Why is live model switching experimental?

Codex CLI 0.160.1 labels `step_model_switching` **under development**. Its
generated `turn/settings/update` schema explicitly calls that API experimental.
The qualifier applies to the upstream active-turn API, not every runtime feature.

Start the runner with its process-local opt-in:

```text
codex-orchestrator serve --live-models
```

From another terminal:

```text
codex-orchestrator task model TASK_ID gpt-6-astra --effort low --live
codex-orchestrator events --task TASK_ID
```

`applied` means settings were published for later inference steps. It cannot
rewrite a step already running, guarantee another inference, or change native
children or your host Codex conversation. Codex may reject destinations with
incompatible tool/review requirements. Unsupported or rejected requests remain
visible, and the model stays saved for the next turn. Interrupt and continue
explicitly when a new turn is needed. Ordinary next-turn selection uses the
[documented app-server turn API](https://learn.chatgpt.com/docs/app-server).

## Approvals and questions

The runtime does not accept requests automatically:

```text
codex-orchestrator approvals
codex-orchestrator respond REQUEST_ID --decision accept
```

Inspect the request first. `decline` and `cancel` are also available. Questions
use `--answers` with JSON, for example in PowerShell:

```powershell
codex-orchestrator respond REQUEST_ID --answers '{"question-id":{"answers":["Selected answer"]}}'
```

MCP tools can inspect requests; approval responses remain CLI actions for the
user. Codex still enforces its configured sandbox and permission policy.

## Usage, reports, and shell automation

```text
codex-orchestrator usage --workflow WORKFLOW_ID
codex-orchestrator report --task TASK_ID --format json --output task-report.json
codex-orchestrator report --workflow WORKFLOW_ID --format markdown --output workflow-report.md
codex-orchestrator task wait TASK_ID --timeout 300
```

Usage displays reported thread counters and missing coverage. Managed and native
totals stay separate; they are not billing totals or dollar estimates. A thread
can span several models, so the last selected model cannot describe all its
historical usage.

Reports contain task results, dependencies, native activity, request/control
summaries, and a bounded recent event window. They stay local and may include
prompts and project details. Existing files are preserved unless you pass
`--force`. Omit `--output` to print to stdout.

Wait commands observe a separate runner and return JSON:

| Exit | Meaning |
|---:|---|
| `0` | All selected managed turns completed |
| `1` | Failed, interrupted, lost, cancelled, or blocked work remains |
| `3` | User input or approval is pending |
| `4` | The remaining queue is paused |
| `124` | Wait timed out; work was not cancelled |

`--timeout 0` checks once. `Ctrl+C` stops a wait/watch client, not the separate
runner. `Ctrl+C` in the runner attempts to interrupt its active work.

## Create your own workflow

Save this as `review-change.json`:

```json
{
  "version": 1,
  "name": "review-change",
  "description": "Map a change, then independently review it",
  "tasks": [
    {
      "key": "map",
      "title": "Map the change",
      "role": "explorer",
      "prompt": "Inspect relevant files and tests for this goal: {goal}"
    },
    {
      "key": "review",
      "title": "Review the evidence",
      "role": "reviewer",
      "prompt": "Review this goal using dependency findings; report concrete defects: {goal}",
      "depends_on": ["map"]
    }
  ]
}
```

```text
codex-orchestrator workflow submit ./review-change.json "Review the CSV export change" --dry-run
codex-orchestrator workflow submit ./review-change.json "Review the CSV export change"
```

Keys identify steps inside the definition; submission creates durable task IDs.
Only `{goal}` is substituted, as text. Invalid roles, models, efforts, cycles,
and missing dependencies are rejected before any task is queued. Optional
`model` and `effort` fields pin individual steps; omit them for role defaults at
dispatch. Definitions are bounded to 32 steps. See [workflow details](guides/workflows.md).

## Use it from Codex

Install the Python runtime above, then add this checkout as a plugin marketplace:

```text
codex plugin marketplace add /absolute/path/to/codex-astra-luna-orchestrator
```

Open the plugin browser in your Codex client, install **Codex Orchestrator**, and
start a new chat. Keep the installed CLI on Codex's PATH. For example:

```text
$orchestrate
In D:\Projects\my-app, use a feature workflow to add CSV invoice export.
Keep authorization unchanged. Show progress and review the final evidence.
```

The plugin can list/submit workflows, inspect managed/native tasks, steer active
work, pause/resume dispatch, change model policy, and read usage/reports. Every
MCP call uses an explicit absolute target project path and shares state with the
CLI. Installation does not install the CLI, start a background service, or add
hooks. A foreground runner executes queued work; the skill can launch it when
execution is part of the requested task.

## State, recovery, and development

State lives in `.orchestrator/state.sqlite3`. Add `.orchestrator/` to your target
project's `.gitignore`. One operating-system lock admits one runner per project.
After a crash, uncertain work becomes `lost` and is not replayed automatically;
inspect the workspace and task before explicitly continuing. A stale heartbeat
means liveness is unknown, not that work completed.

Run checks with your chosen interpreter's absolute path:

```powershell
& $Interpreter -m unittest discover -s tests -v
```

- [Runtime and recovery guide](guides/dynamic-cli.md)
- [Workflow definitions and execution](guides/workflows.md)
- [Architecture](guides/dynamic-orchestration-design.md) and [design references](guides/reference-projects.md)
- [Original profile installation and configuration](guides/profile-installer.md)
- [Token usage measurement for existing Codex sessions](guides/token-usage.md)

Licensed under [Apache-2.0](LICENSE).
