# External ACP TUI Runtime

Read this reference only when the team uses `--agent-runtime acp` or an operator is replacing an ACP provider session.
The default Codex runtime does not need it.

## Boundary

Each ACP role remains visible in a Toad pane. Toad owns the configured ACP child command and provider session;
tmux-team sends compact wake prompts through a private role Unix socket. Durable task bodies remain exclusively in the
SQLite inbox. ACP roles do not create `tt-app-server`.

ACP sleep/resume supports explicit `exact` and `handoff` policies. Provider-specific flags belong in
`--acp-agent-command`; `--acp-provider`, `--model`, and `--effort` are provenance metadata unless the provider protocol
proves otherwise.

## Preflight And Bootstrap

Verify Python 3.14+, Toad, and the local provider adapter before bootstrap:

```bash
python3.14 --version
toad --version
agent status
codex-acp --version
claude-agent-acp --version
pool --version
tmux-team bootstrap --project-root . --agent-runtime acp \
  --acp-tui-bin toad --acp-provider claude \
  --goal "USER_GOAL"
```

Canonical presets are Cursor `agent acp`, Codex `codex-acp`, Claude `claude-agent-acp`, and Pool `pool acp`. They are local stdio
children, not ACP URLs. Codex ACP uses local Codex authentication; Claude ACP uses local Claude credentials/settings;
Pool uses `pool login` or Poolside-owned API URL/key configuration. `pool acp setup --editor ...` is editor registration
and is not required for Toad.
Use `--acp-agent-command` for provider flags, `npx`, pinned/local adapters, or custom providers.

Bootstrap must wait for every role's versioned `ping`/`status` handshake, then record its control socket and runtime
session ID. A provider-triggered TUI crash is a bootstrap failure, not a healthy team.

When model/cost settings matter, pass repeatable `--acp-initial-config ID=VALUE`. Bootstrap must apply and confirm each
advertised option before sending the control or role startup prompt; never allow the first turn to inherit an
unverified expensive default.

## Provider Permissions

Permission policy belongs to the provider. For autonomous Cursor roles, `agent --force acp` is the explicit allow-all
choice. For constrained operation, use project-local `.cursor/cli.json`; do not silently modify the user's global
`~/.cursor/cli-config.json`.

Codex ACP can start with `INITIAL_AGENT_MODE=agent-full-access` only when the operator explicitly accepts it. Claude
ACP reads Claude Code settings; use `.claude/settings.local.json` for deliberate worktree-local policy. Never modify
global provider policy during bootstrap.

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

## Sleep And Resume

Use `tmux-team sleep --acp-resume-policy exact` by default. Exact sleep requires each provider to advertise session
loading, drains and quiesces every role, records session/launch/socket metadata plus a fallback capsule, then tears down
managed panes. `tmux-team resume` starts Toad with the saved session ID and must verify the returned ID before waking
pending work.

Use `tmux-team resume --acp-resume-policy handoff` only when the operator explicitly accepts a fresh provider session.
It requires the saved capsule and sends the recovery prompt. Never infer or silently apply handoff after exact load
fails.

## Runtime Switch Contract

Replacing a provider/model command creates a new provider session and requires a durable handoff.

Before switching:

1. Stop claiming new work and reach an idle safe point. Do not switch during a tool call, approval, or partial mutation.
2. Update scratchpad memory with the current task, decisions, blockers, artifacts, and exact next action.
3. Reconcile active todos as completed, open, or superseded.
4. Run `tmux-team runtime prepare <role> --summary "..."`. This drains the role and binds the latest capsule to its
   digest and source session. The capsule must not contain task bodies, credentials, hidden reasoning, or a full
   transcript.
5. Use `tmux-team runtime switch` with that capsule. Active turns are refused unless explicit cooperative cancellation
   reaches idle.

After replacement:

1. Load the main skill and this reference.
2. Read scratchpad memory and the handoff capsule.
3. Inspect Git status and diff directly.
4. Recover active todos and claim durable inbox work.
5. Verify prior claims against durable evidence, then continue without repeating completed work.

The replacement accepts only that role's latest unchanged prepared capsule, atomically quiesces new external prompts,
reuses the pane and socket path, records old/new session lineage, and leaves the role `draining` after failure.
Same-session model or effort changes are valid only when ACP capability/config responses prove they were applied.
