# Reference projects

These projects are design references for this Codex CLI and plugin. The links below identify upstream facts; the implementation notes describe this repository. No code is copied from either project.

## Upstream facts

[Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator) coordinates agent sessions, workspaces, and lifecycle. Its [architecture guide](https://github.com/OrchestratorInc/agent-orchestrator/blob/main/docs/architecture.md) documents a long-running Go daemon, SQLite persistence, adapters, project worktrees, a thin CLI, and live event delivery. It separates external observation from durable lifecycle facts and derives display status from those facts. Its CLI is described in [the CLI guide](https://github.com/OrchestratorInc/agent-orchestrator/blob/main/docs/cli/README.md). The upstream project is licensed under [Apache-2.0](https://github.com/OrchestratorInc/agent-orchestrator/blob/main/LICENSE).

[oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) packages Codex workflows as a global CLI, plugin, skills, hooks, and project-scoped state. Its [routing reference](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/docs/reference/omx-config-schema-routing.md) separates role, model tier, and posture and documents per-role overrides and precedence. Its [agent-tier guide](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/skills/ultrawork/references/agent-tiers.md) relates depth and cost to task shape. The [README](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/README.md) identifies macOS/Linux as its primary supported path and native Windows as less supported. It is licensed under [MIT](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/LICENSE).

## Design applied here

This project keeps the CLI and plugin as the user-facing entry points. The CLI and stdio MCP server share one Python implementation and project-local state. `codex-orchestrator --project PATH serve` is the foreground scheduler; tool calls queue work and return, while the runner dispatches it. State lives in `.orchestrator/state.sqlite3`, with durable task, role, setting, event, agent, request, and control records. The runtime uses the installed Codex CLI app-server protocol for model discovery and task execution. The package targets Python 3.11+ and uses the standard library; SQLite is supplied by Python.

The reusable ideas are:

- **Durable lifecycle:** retain task status, dependencies, role and model policy, results, and event history so a task can be inspected or continued after a CLI turn ends.
- **Visible observations:** capture agent/thread activity and pending user requests in the same project state. Treat heartbeat and model-publication information as observations with limits; do not imply that a queued action or stale signal proves completion.
- **Small role catalog and dynamic routing:** keep role instructions separate from model and reasoning effort. Load defaults from project state and discover model IDs from the installed Codex catalog instead of hard-coding a model list.
- **Explicit live controls:** distinguish future role defaults and next-turn task policy from the experimental request to publish a model change during an active managed turn. An `applied` response means publication for subsequent captures; it does not rewrite captured steps or guarantee another inference. Unsupported or unavailable targets remain next-turn changes, and child sessions are unaffected.
- **Thin plugin boundary:** skills explain when to use managed tasks, while CLI and MCP expose the shared task lifecycle. User approvals remain explicit CLI decisions; plugin installation does not install the runtime or start a runner.

## Scope and pitfalls

This implementation deliberately stays a local CLI, project SQLite store, stdio MCP server, and foreground runner. It does not include Agent Orchestrator's daemon, web or desktop clients, SSE fan-out, or worktree isolation. Managed tasks therefore share the target project checkout; assign non-overlapping file scopes when tasks may run concurrently, and have the parent integrate and verify the result.

Do not copy oh-my-codex's hooks or role/model tables wholesale. This plugin must respect Codex's current approvals and app-server capabilities, discover models dynamically, and label experimental live updates accurately. Upstream oh-my-codex documents weaker native Windows support, so its shell assumptions are not a portable implementation template.

Both upstream licenses are permissive, but this guide records design inspiration rather than legal review. If code is reused later, review its license and preserve the required copyright and license notices. OpenAI's [plugin deployment guidance](https://developers.openai.com/plugins/deploy/submission) also distinguishes local stdio testing from public directory deployment, which requires a reachable HTTPS MCP endpoint.
