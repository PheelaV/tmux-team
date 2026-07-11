# External ACP TUI Runtime

Read this reference only when the team uses `--agent-runtime acp` or an operator is replacing an ACP provider session.
The default Codex runtime does not need it.

## Boundary

Each ACP role remains visible in a Toad pane. Toad owns the configured ACP child command and provider session;
tmux-team sends compact wake prompts through a private role Unix socket. Durable task bodies remain exclusively in the
SQLite inbox. ACP roles do not create `tt-app-server`.

The prototype does not implement ACP sleep/resume. Provider-specific flags belong in `--acp-agent-command`;
`--acp-provider`, `--model`, and `--effort` are provenance metadata unless the provider protocol proves otherwise.

## Preflight And Bootstrap

Verify Python 3.14+, Toad, and the provider before bootstrap. For Cursor:

```bash
python3.14 --version
toad --version
agent status
tmux-team bootstrap --project-root . --agent-runtime acp \
  --acp-tui-bin toad --acp-agent-command "agent acp" --acp-provider cursor \
  --goal "USER_GOAL"
```

Bootstrap must wait for every role's versioned `ping`/`status` handshake, then record its control socket and runtime
session ID. A provider-triggered TUI crash is a bootstrap failure, not a healthy team.

## Provider Permissions

Permission policy belongs to the provider. For autonomous Cursor roles, `agent --force acp` is the explicit allow-all
choice. For constrained operation, use project-local `.cursor/cli.json`; do not silently modify the user's global
`~/.cursor/cli-config.json`.

The startup loop invokes both `command` and `tmux-team`, so a Cursor allowlist needs at least:

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

Add only the project commands and scoped read/write paths required by each role, such as `Shell(git)`, `Shell(uv)`,
`Read(**/*)`, `Write(src/**/*)`, or `Write(tests/**/*)`. Keep secrets and destructive commands denied. Because roles may
use separate worktrees, ensure the project-local policy is available in every role worktree before bootstrap.

## Runtime Switch Contract

Replacing a provider/model command creates a new provider session and requires a durable handoff.

Before switching:

1. Stop claiming new work and reach an idle safe point. Do not switch during a tool call, approval, or partial mutation.
2. Update scratchpad memory with the current task, decisions, blockers, artifacts, and exact next action.
3. Reconcile active todos as completed, open, or superseded.
4. Run `tmux-team runtime prepare <role> --summary "..."`. The capsule must not contain task bodies, credentials,
   hidden reasoning, or a full transcript.
5. Use `tmux-team runtime switch` with that capsule. Active turns are refused unless explicit cooperative cancellation
   reaches idle.

After replacement:

1. Load the main skill and this reference.
2. Read scratchpad memory and the handoff capsule.
3. Inspect Git status and diff directly.
4. Recover active todos and claim durable inbox work.
5. Verify prior claims against durable evidence, then continue without repeating completed work.

The replacement reuses the pane and socket path, records old/new session lineage, and leaves the role `draining` after
failure. Same-session model or effort changes are valid only when ACP capability/config responses prove they were
applied.
