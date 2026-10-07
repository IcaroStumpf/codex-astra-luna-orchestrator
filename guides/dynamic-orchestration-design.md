# Dynamic orchestration

This fork is becoming an installable Codex plugin with a companion terminal
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
- Change role defaults or a task's model while the runner stays alive. Apply
  changes at the next turn; show pending changes separately from running work.
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

The runner holds an operating-system lock. Only one runner may dispatch work
for a project. It treats an explicit completed turn as completion; elapsed time,
an empty event queue, or a missing result is not success. Uncertain in-flight
work after a crash becomes `lost` and requires an explicit continuation.

A managed task is a Codex thread with a role and a bounded objective. A native
subagent is a child spawned by Codex inside that thread. The board distinguishes
them and records parent relationships. Native child events are observational;
changing managed task policy does not rewrite already-running native children.

## Model switching boundary

Codex accepts model and effort overrides on `turn/start`. `turn/steer` cannot
change the model. Policy edits therefore affect queued tasks and subsequent
turns. Running work keeps its observed model until completion or an explicit
interrupt. This runtime does not claim to remotely change a separate Codex CLI
or desktop session. Model listing reports the installed Codex catalog, not a
guarantee of account access.

## Evidence and inspiration

- [Codex app-server](https://learn.chatgpt.com/docs/app-server): thread/turn
  lifecycle, model overrides, streamed events, and approval requests.
- [Plugin packaging](https://developers.openai.com/plugins/build/plugins):
  portable manifest, skills, MCP, and repository marketplace.
- [Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator):
  reference project under evaluation for visible task lifecycle and runtime
  separation.
- [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex): reference project
  under evaluation for Codex-native role workflows and terminal visibility.

Implementation in this fork is original. Reference findings and their concrete
influence will be recorded as the integration is verified.
