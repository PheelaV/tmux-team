# ACP Runtime Handoff

Provider conversation identifiers are execution details, not tmux-team's durable
role identity. A role can continue across providers or model-specific ACP
commands only by transferring a bounded, provider-neutral handoff capsule.

## Two Switch Modes

Same-session model changes should use ACP `session/set_config_option` when the
active TUI advertises a compatible model or reasoning option. The current Toad
control socket does not expose that operation, so tmux-team does not pretend to
support it yet.

Changing ACP command, provider, or a launch-only model starts a new provider
session:

```text
active role
  -> safe idle boundary
  -> handoff capsule
  -> role draining
  -> replace ACP TUI process in the same pane
  -> new ACP session
  -> update config and session lineage
  -> recovery prompt
  -> role active
```

## Handoff Capsule

Capsules are Markdown files under:

```text
<runtime>/handoffs/<role>/<timestamp>.md
```

They contain:

- role and current runtime state;
- previous provider, model, effort, ACP command, and session identifier;
- active message identifiers, states, priorities, senders, and summaries;
- open role todos;
- scratchpad path and bounded excerpt;
- worktree, pane, Git status, and diff summary;
- operator-supplied summary and optional body;
- explicit next action.

They never contain inbox task bodies, hidden reasoning, credentials, or a full
conversation transcript.

The capsule supplements durable state; it does not become message transport,
scratchpad memory, or a second queue. Exact code state remains in the worktree,
and active assignments remain in SQLite.

## Safe Boundary

- Refuse preparation or switching while the ACP TUI is `busy` or `asking`.
- An explicit cancel option may request cooperative cancellation and wait for
  the role to become idle.
- Set the role to `draining` before replacing its process so ordinary new work
  is blocked.
- Urgent durable work may still arrive and remains recoverable from SQLite.
- Never silently create repeated replacement sessions after a launch failure.
- A failed switch leaves the role `draining` for operator recovery.

## Session Lineage

Append one JSON object per successful switch to:

```text
<runtime>/handoffs/<role>/lineage.jsonl
```

Each entry records:

- switch timestamp and actor;
- old provider/model/effort/command/session;
- new provider/model/effort/command/session;
- handoff capsule path.

The current session remains in `team.toml`. Lineage is operator provenance, not
the authoritative task state.

## Recovery Prompt

After the new TUI reports ready, tmux-team sends one compact prompt instructing
the role to:

1. load the `start-tmux-team` skill;
2. read scratchpad memory;
3. read the handoff capsule;
4. inspect Git status and diff;
5. recover active todos and inbox work;
6. verify continuity before changing files;
7. continue without repeating completed work.

The prompt points to durable artifacts and does not embed task bodies.

## Config Update

A successful switch atomically updates only the selected role's runtime
capabilities:

- `acp_agent_command`;
- `acp_provider`;
- optional `acp_model` and `acp_effort`;
- `runtime_session_id`;
- `previous_runtime_session_id`;
- `last_handoff_file`.

All unrelated team, role, policy, pane, worktree, and scratchpad fields must be
preserved.

## Initial CLI

```text
tmux-team runtime show ROLE
tmux-team runtime prepare ROLE --summary TEXT [--body-file PATH]
tmux-team runtime switch ROLE --acp-agent-command COMMAND \
  [--provider NAME] [--model NAME] [--effort LEVEL] \
  --handoff-file PATH [--cancel-active] [--dry-run]
```

`runtime switch` is initially limited to visible `acp_tui` roles. Codex
app-server lifecycle and same-session ACP config changes remain separate paths.
