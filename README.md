# Codex Orchestrator

A Codex plugin and terminal runtime for agent teams: durable tasks, dependency
ordering, a live task board, configurable roles, and model changes while the
runner stays alive. This fork builds on
[donvito's profile installer](https://github.com/donvito/codex-astra-luna-orchestrator)
and takes design inspiration from
[Agent Orchestrator and oh-my-codex](guides/reference-projects.md).

## Dynamic CLI and plugin

Requires Python 3.11+ and an installed, signed-in Codex CLI. The Python runtime
has no third-party runtime dependencies. It uses your local `codex app-server`;
it does not read credentials or require a separate API key.

Clone this fork, then install the CLI with your chosen interpreter's **absolute
path**. For example, in PowerShell:

```powershell
git clone https://github.com/IcaroStumpf/codex-astra-luna-orchestrator.git
cd codex-astra-luna-orchestrator
$Interpreter = 'C:\absolute\path\to\python.exe'
& $Interpreter -m pip install .
```

On macOS/Linux, use `/absolute/path/to/python -m pip install .`. Ensure that
interpreter's Scripts/bin directory is on PATH so `codex-orchestrator` is
available to both your terminal and Codex. From a source checkout you can also
run `& $Interpreter -m codex_orchestrator --help` without installing.

Initialize your target project and queue work:

```text
codex-orchestrator --project /absolute/project init
codex-orchestrator --project /absolute/project doctor
codex-orchestrator --project /absolute/project roles
codex-orchestrator --project /absolute/project task add "Map the login flow and report relevant files" --role explorer
codex-orchestrator --project /absolute/project serve
```

Keep `serve` running in that terminal. Use `serve --live-models` to enable
Codex's experimental active-step model switching for this runner. In another terminal:

```text
codex-orchestrator --project /absolute/project watch
codex-orchestrator --project /absolute/project task show TASK_ID
codex-orchestrator --project /absolute/project task add "Review the findings" --role reviewer --after TASK_ID
codex-orchestrator --project /absolute/project model set worker gpt-6-luna --effort medium
codex-orchestrator --project /absolute/project task model TASK_ID gpt-6.1-sol --effort medium --live
```

Use Windows absolute paths such as `D:\Projects\my-project` on Windows.
`serve --once` drains runnable work and exits. `watch --json` emits JSON snapshots.
You can import model choices from an existing profile with
`init --config /absolute/project/.codex/config.toml`; it does not rewrite that
configuration or its permissions.

The default roles are orchestrator, explorer, worker, tester, reviewer,
researcher, architect, debugger, and documenter. Inspect or customize them
through `roles`, `model set`, and `role add`. Model IDs remain configurable;
`models` queries the installed Codex catalog and does not guarantee account
access to every listed model.

To make the plugin discoverable, add this repository as a marketplace:

```text
codex plugin marketplace add /absolute/path/to/codex-astra-luna-orchestrator
```

Open the plugin browser in your Codex client, install **Codex Orchestrator**, and
start a new chat. The plugin exposes `$orchestrate` and local MCP tools that use
the same task state as the CLI. The CLI must already be on Codex's PATH. Plugin
installation does not start a runner or install hooks. This is a local plugin;
no public-directory publication is performed by setup.

See [the runtime guide](guides/dynamic-cli.md) for approvals, model-switch
semantics, interruption/continuation, state recovery, and development checks.

## Original profile installer

Install a Codex profile with GPT-6 Astra, GPT-6.1 Sol, or GPT-6 Luna as the orchestrator,
GPT-6 Luna execution subagents, and an independent reviewer.

The Sol profiles use `gpt-6.1-sol` for both the orchestrator and reviewer.

## Orchestration topology

The diagram shows the Astra (Pro) and Sol orchestrators. Plus uses a Luna
root, as shown in the profile table below. Execution roles use GPT-6 Luna;
the reviewer uses Astra for Pro/Plus and Sol for Sol profiles.

```text
           GPT-6 Astra / GPT-6.1 Sol
             root / orchestrator
                      |
      +---------------+---------------+
      |               |               |
   explorer          worker         researcher
  GPT-6 Luna       GPT-6 Luna       GPT-6 Luna
      |               |
      +-------+-------+
              |
           tester
         GPT-6 Luna
              |
          reviewer
      GPT-6 Astra / GPT-6.1 Sol
              |
              v
           root agent
      integrate + verify
```

## Setup

1. Clone this repository and enter it:

   ```sh
   git clone https://github.com/IcaroStumpf/codex-astra-luna-orchestrator.git
   cd codex-astra-luna-orchestrator
   ```

2. Run the installer for your platform:

   macOS/Linux:

   ```sh
   ./setup.sh
   ```

   Windows PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   PowerShell 7:

   ```powershell
   pwsh -File .\setup.ps1
   ```

3. When prompted, enter an **existing target repository other than this one**,
   choose a profile by number or name (Enter selects Pro), and confirm which
   components to install. For example:

   ```text
   Target repository path: ../my-project
   Select Profile [1-6] (default 1): 5
   ```

The installer copies the selected configuration to `.codex/`, its skill to
`.agents/`, and project instructions to `AGENTS.md`. Existing component files
are updated only after confirmation; existing `AGENTS.md` content is preserved.
Codex loads project-scoped configuration only for trusted projects.

### Installed target project

If you install all three components into `../my-project`, the installer adds
these paths alongside the project's existing files:

```text
my-project/
├── .codex/
│   ├── config.toml
│   └── agents/
│       ├── explorer.toml
│       ├── researcher.toml
│       ├── reviewer.toml
│       ├── tester.toml
│       └── worker.toml
├── .agents/
│   └── skills/
│       └── astra-orchestrator/
│           └── SKILL.md
└── AGENTS.md
```

`profiles/<profile>/codex/` becomes `.codex/`, and
`profiles/<profile>/agents/` becomes `.agents/`. The root `AGENTS.md` is
copied to the target, or its instructions are appended if that file exists.

## How to use the skill

From the target repository, launch Codex CLI. For the example above:

```sh
cd ../my-project
codex
```

For complex work, Codex may select the skill automatically, or you can invoke
it explicitly:

```text
$astra-orchestrator
Implement the invoice export endpoint. Use the explorer to map the path,
a worker to implement it, and the tester and reviewer to verify it.
```

The skill keeps the `astra-orchestrator` name in every profile so the shared
`AGENTS.md` works; the Sol profiles use Sol according to their configuration.

## Profiles

| Choice | Profile | Root | Execution roles | Reviewer | Concurrent subagents |
|---|---|---|---|---|---:|
| 1 (default) | `pro` | Astra medium | Luna max | Astra low | 4 |
| 2 | `plus` | Luna max | Luna medium | Astra low | 4 |
| 3 | `pro-max-2-subagents` | Astra medium | Luna max | Astra low | 2 |
| 4 | `plus-max-2-subagents` | Luna max | Luna medium | Astra low | 2 |
| 5 | `GPT6-SolMax-LunaMax` | Sol max | Luna max | Sol max | 4 |
| 6 | `GPT6-SolMedium-LunaMax` | Sol medium | Luna max | Sol medium | 4 |

All models above are GPT-6. Execution roles are explorer, worker, tester,
and researcher; named roles pin their models and reasoning levels independently
of the default subagent settings. Each ready-to-copy profile lives under
`profiles/<profile>/`.

For manual project setup, copy the selected profile's `codex/` and `agents/`
to the target repository as `.codex/` and `.agents/`, and add this repository's
`AGENTS.md`. For personal/global setup, copy its `codex/agents/` into
`~/.codex/agents/`, its `agents/skills/astra-orchestrator/` into
`~/.agents/skills/`, and **merge**, rather than replace, its `codex/config.toml`
settings into `~/.codex/config.toml`. Do not overwrite other existing Codex
settings.

## Key directory structure

```text
.
├── profiles/
│   ├── pro/
│   ├── plus/
│   ├── pro-max-2-subagents/
│   ├── plus-max-2-subagents/
│   ├── GPT6-SolMax-LunaMax/
│   └── GPT6-SolMedium-LunaMax/
├── guides/
├── scripts/
│   └── token_usage.py
├── tests/
│   ├── test_profiles.py
│   └── test_token_usage.py
├── AGENTS.md
├── setup.sh
├── setup.ps1
├── README.md
└── LICENSE
```

Each profile contains `codex/config.toml`, `codex/agents/*.toml`, and
`agents/skills/astra-orchestrator/SKILL.md`.

## Guides

- [Pro orchestration and manual configuration](guides/full-orchestration.md)
- [Plus profile and global setup](guides/plus-plan.md)
- [Fast iteration](guides/fast-iteration.md) and [routine coding](guides/routine-coding.md)
- [Complex repository work](guides/complex-repo-work.md)
- [Token usage and measurement](guides/token-usage.md)

## License

Licensed under the [Apache License 2.0](LICENSE).
