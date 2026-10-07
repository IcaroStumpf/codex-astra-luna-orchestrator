# Dynamic orchestration

This fork is an installable Codex plugin with a companion terminal
runtime. Existing profile installers remain usable. The new runtime uses the
installed, signed-in Codex CLI through its app-server protocol; it does not
implement inference, handle login tokens, or require a separate OpenAI API key.

## Outcomes and acceptance criteria

- Install the Python 3.11+ CLI and discover the plugin through a repository
  marketplace. Keep Windows, macOS, and Linux workflows.
- Queue role-scoped tasks, express dependencies, run independent tasks
  concurrently, and inspect their results. Serialize workspace-writing tasks.
- View task state, active tools, requested/observed models, usage, pending
  approvals, and native child-agent activity from the terminal or plugin.
- Change role defaults or a task's model while the runner stays alive. Support
  next-turn policy and explicit, capability-gated active-turn publication;
  distinguish requested, published, and observed settings.
- Interrupt and continue a task using the same Codex thread. Preserve history
  across runner restarts without automatically replaying uncertain work.
- Offer useful roles with configurable models and reasoning effort. Avoid
  increasing delegation just because another role is available.
- Retain Codex sandbox and approval handling. Never auto-accept an approval.
- Verify lifecycle, model changes, dependency ordering, concurrency,
  persistence, protocol errors, plugin packaging, and both installer platforms.

## Components

`codex_orchestrator` is a standard-library Python package. A SQLite database
under the selected project's `.orchestrator/` stores tasks, model policies,
events, pending requests, and runner metadata. A foreground `serve` process
owns one Codex app-server and schedules tasks. Separate CLI invocations and
the plugin's stdio MCP server communicate through the database. Installation
does not start a background service or alter global Codex permissions.

Version 0.4 adds declarative workflow definitions and atomic submission of named
graphs. Workflow IDs group tasks independently of runner IDs; the existing
scheduler executes their dependency edges. Schema v2 adds workflow records and
migrates existing v1 tasks without discarding history. Dispatch pause is durable
and checked atomically at task claim. Steering captures run/thread/turn identity
and never automatically replays uncertain delivery. Terminal views and reports
derive from observed state and separate managed/native token counters.

The runner holds an operating-system lock. Only one runner may dispatch work
for a project. It treats an explicit completed turn as completion; elapsed time,
an empty event queue, or a missing result is not success. Uncertain in-flight
work after a crash becomes `lost` and requires an explicit continuation.

A managed task is a Codex thread with a role and a bounded objective. A native
subagent is a child spawned by Codex inside that thread. The board distinguishes
them and records parent relationships. Native child events are observational;
changing managed task policy does not rewrite already-running native children.

## Model switching boundary

Codex accepts model and effort overrides on `turn/start`. Policy edits affect
queued tasks and subsequent turns. Codex CLI 0.160.1 additionally exposes the
experimental `turn/settings/update` method. An explicit live change uses that
method: `applied` means published for subsequent captures, not retroactive
replacement of already captured steps or a guarantee of another inference.
`targetUnavailable` and unsupported methods keep the next-turn policy intact.
Children and separate Codex CLI/desktop sessions are unaffected. Model listing
reports the installed Codex catalog, not a guarantee of account access.
The installed CLI gates this method behind `step_model_switching`; the runner's
`--live-models` option enables it for the child process only.

## Evidence and inspiration

- [Codex app-server](https://learn.chatgpt.com/docs/app-server): thread/turn
  lifecycle, model overrides, streamed events, and approval requests.
- [Plugin packaging](https://developers.openai.com/plugins/build/plugins):
  portable manifest, skills, MCP, and repository marketplace.
- [Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator):
  visible task lifecycle, evidence-based status and runtime separation.
- [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex): Codex-native plugin
  packaging, role policy separate from model choice, and project-local state.

Implementation in this fork is original. [Reference findings](reference-projects.md)
record the concrete influence and license attribution.
