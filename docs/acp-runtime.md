# External ACP TUI Runtime

The experimental ACP runtime runs each managed role as a visible Toad TUI. Toad owns the provider process and ACP
session; tmux-team owns durable work state and sends compact wake prompts through a private Unix socket. Task bodies
remain in SQLite and are never typed into the terminal composer.

## Install And Preflight

ACP support currently requires Python 3.14 and the temporary Toad control-socket branch:

```bash
uv tool install --python 3.14 \
  "tmux-team[acp] @ git+https://github.com/PheelaV/tmux-team.git"

toad --version
agent status  # Cursor example
```

Bootstrap a Cursor-backed team:

```bash
tmux-team bootstrap --project-root . \
  --agent-runtime acp \
  --acp-tui-bin toad \
  --acp-agent-command "agent acp" \
  --acp-provider cursor \
  --goal "Inspect the failing test and report verified evidence."
```

The layout contains `tt-control` and visible role panes in `tt-agents`; ACP teams do not need `tt-app-server`.
Bootstrap verifies each Toad socket and provider session before reporting success.

## Permissions

Provider-specific permission flags belong in `--acp-agent-command`. Cursor's explicit autonomous mode is:

```bash
--acp-agent-command "agent --force acp"
```

For constrained operation, put policy in project-local `.cursor/cli.json` rather than changing the user's global
configuration. The tmux-team startup loop needs both commands below:

```json
{
  "approvalMode": "allowlist",
  "permissions": {
    "allow": [
      "Shell(command)",
      "Shell(tmux-team)"
    ]
  }
}
```

Add only the commands and scoped filesystem access required by the role, for example `Shell(git)`, `Shell(uv)`,
`Read(**/*)`, `Write(src/**/*)`, and `Write(tests/**/*)`. Keep secrets and destructive commands denied. If roles use
separate worktrees, make the project-local policy available in each worktree before bootstrap.

tmux-team does not modify provider-global permission files.

## Delivery And Control

ACP wake delivery is:

```text
SQLite inbox -> private Unix socket -> visible Toad TUI -> ACP session/prompt -> provider agent
```

Inspect or control a role without touching terminal input:

```bash
tmux-team acp status implementer
tmux-team acp wake implementer
tmux-team acp cancel implementer
```

## Provider Session Handoff

At an idle boundary, capture durable continuity and replace the provider process in the same pane:

```bash
tmux-team runtime prepare implementer \
  --summary "Focused fix passes; full verification remains."

tmux-team runtime switch implementer \
  --acp-agent-command "claude-agent-acp" \
  --provider claude \
  --model sonnet \
  --handoff-file .tmux-team/runtime/handoffs/implementer/<handoff>.md
```

The bounded capsule includes role, inbox metadata, todos, memory, and Git state, but not inbox bodies or a full
transcript. `runtime prepare` drains the role before confirming an idle, empty provider queue. `runtime switch` accepts
only the latest unchanged capsule prepared for that role and source session, then atomically quiesces new external
prompts before replacing the pane. Active turns are refused unless
`--cancel-active` reaches idle. Failed replacement leaves the role `draining` for explicit recovery. Inspect current
and previous session metadata with `tmux-team runtime show <role>`.

## Current Limits

- Same-session changes are limited to options advertised by the active ACP agent.
- `--acp-provider` is provenance metadata; provider behavior comes from `--acp-agent-command`.
- Replacing the provider command still requires a runtime handoff.

## Sleep And Resume

ACP sleep defaults to exact restoration:

```bash
tmux-team sleep --acp-resume-policy exact
tmux-team resume
```

Sleep requires every role to be idle with an empty external queue, verifies `resumeSupported`, quiesces new prompts,
and snapshots the provider session ID plus launch/binding metadata and a fallback capsule. Resume starts Toad with
`--session-id`, requires the loaded ID to match, and then re-wakes durable pending inbox work.

Use a fresh provider session only by explicit choice:

```bash
tmux-team resume --acp-resume-policy handoff
```

Handoff mode requires the saved capsule and sends its recovery prompt. Exact restoration never silently falls back to
handoff. Provider retention, credentials, host changes, or adapter incompatibility can still make exact load fail; the
team remains paused for operator recovery.
