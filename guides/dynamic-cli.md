# Dynamic CLI guide

All examples use `codex-orchestrator` from the selected Python environment's
Scripts/bin directory. Prefix a command with `--project /absolute/project`
when you are not in the target project. Windows paths work without translation.
Do not use this repository itself as the target of an installer smoke test.

## Tasks and agents

A **managed task** is a bounded assignment backed by a Codex conversation
thread. Its role describes its responsibility; its model and reasoning effort
are separate choices. A **native subagent** is a child Codex creates inside
that conversation. The board shows native parent/child relationships when the
installed app-server emits those events. Separate desktop/CLI sessions are not
attached to this runner and are not silently controlled by it.
For an ordinary interactive Codex CLI conversation, its built-in
[`/model` picker](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
changes that conversation's model; `/status` verifies the selection. The
orchestrator controls below address managed tasks instead.

`task add` returns an ID. Pass it to `task show`, `task interrupt`, `task model`,
or `task continue`. Multiple `--after ID` arguments create dependencies; a task
starts only after all those dependencies complete. The runner passes their
results into the dependent task. `--parent ID` records an organizational
relationship; it does not imply a dependency or create a native child thread.

The runner admits parallel read-only tasks up to `serve --workers N` (default
2). Workspace-writing tasks run exclusively so workers cannot edit the same
workspace concurrently. Role instructions further constrain scope; they are
not a filesystem sandbox. Codex remains responsible for sandbox enforcement.

## Visibility and controls

`status` shows a one-time board; `watch` refreshes it. `status --json` and
`watch --json` expose structured state for scripts. `task show ID` includes the
result and error. `events --task ID` shows lifecycle history; `events --after N`
reads a bounded window after an event cursor. Raw reasoning is not collected.

Status is based on Codex turn events. A completed turn means Codex completed
that turn, not that a reviewer has proven the entire user objective correct.
The parent still integrates and verifies results. Stale heartbeat data is
labeled as uncertain, never treated as proof of task completion or death.

Model selection follows this order: task override, then current role policy.
`model set ROLE MODEL --effort EFFORT` changes role defaults for future turns.
`task model ID MODEL` changes that task's next turn. Running turns retain their
captured settings, and the board shows the pending change.

Start the runner with `serve --live-models` to enable Codex's experimental
`step_model_switching` feature for that process, without editing global config.
`task model ID MODEL --live` then requests active-turn publication.
On compatible Codex versions, `applied` means later steps can capture the new
settings. Already captured inference steps are unchanged; another inference
is not guaranteed. Native children retain their own settings. Unsupported
methods and `targetUnavailable` preserve the next-turn choice without claiming
a live change. Inspect `status` and `events` for the outcome. A published choice
is distinguished from an explicitly observed model reroute.
Codex can also reject a destination that changes tool or review requirements
already admitted for the running turn. That rejection is shown as a failed
live request; the chosen model remains saved for the next turn. Interrupt and
continue explicitly if you need to start a new turn with different settings.

The live API and its feature gate were verified against Codex CLI 0.160.1:
`TurnSettingsUpdateParams` and `TurnSettingsUpdateResponse`. The regular
[app-server API](https://learn.chatgpt.com/docs/app-server) supports model
overrides at turn start. Older CLI versions can use that path.

## Approvals and questions

The runner never accepts a request automatically. While work waits:

```text
codex-orchestrator approvals
codex-orchestrator respond REQUEST_ID --decision accept
```

Read the request first. `decline` and `cancel` are also available. Questions
take `--answers` with a JSON object such as
`{"question-id":{"answers":["Selected answer"]}}`. Quote the JSON according to
your shell. Requests are scoped to their runner and turn; stale responses are
not replayed into a new turn. Unknown request types are rejected explicitly.

The MCP tools expose task controls but do not expose approval-granting tools.
Use the CLI to make that decision yourself. Existing managed Codex policy can
still reject an operation or a requested permission mode.

## Interrupt, continue, and recover

```text
codex-orchestrator task interrupt TASK_ID
codex-orchestrator task show TASK_ID
codex-orchestrator task continue TASK_ID "Continue with the corrected requirement"
```

Wait for the task to reach a terminal state before continuing. Continuation
reuses its saved Codex thread and resolves current model policy again.
Cancelling queued work does not start a Codex thread.

Task state is local in `.orchestrator/state.sqlite3`. Keep this directory out of
version control: it can contain task prompts, results, and command metadata.
The repository's `.gitignore` already excludes it; add the same exclusion in
target projects. No remote telemetry or background service is installed.

One operating-system lock permits one runner per project. When a runner starts
after a crash, uncertain in-flight work becomes `lost`; it is not automatically
replayed. Inspect the task, workspace, and saved thread before explicitly
continuing. You can use Codex's native resume command with the stored thread ID
for additional inspection. Normal shutdown attempts to interrupt active work.

## Development

Use the absolute path of your Python 3.11+ interpreter to run:

```text
/absolute/path/to/python -m unittest discover -s tests -v
```

The suite exercises fake app-server processes, protocol failures, model policy,
task state, CLI/MCP commands, plugin packaging, and the original profiles. Git
Bash is needed for shell-installer coverage on Windows; PowerShell coverage is
included when `pwsh` is installed. Live model/account access is not inferred
from fake-server tests. `doctor` checks the actual installed protocol and model
catalog without generating a response.
