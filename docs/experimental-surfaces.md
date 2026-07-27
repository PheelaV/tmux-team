# Experimental Surfaces

These notes are design direction, not required operator reading. Current user-facing commands live in [CLI Reference](cli-reference.md).

## MCP

`src/tmux_team/mcp_server.py` exposes a narrow MCP-shaped JSON-RPC facade over the same `TeamService` used by the CLI.

Current tools:

- `team_status`
- `team_inbox_next`
- `team_ack`
- `team_complete`
- `team_send`
- `team_notify`
- `team_wake`

Run it locally for experiments:

```bash
python -m tmux_team.mcp_server --config .tmux-team/team.toml
```

This is intentionally a thin adapter, not a second state model. SQLite remains authoritative and app-server wake turns remain notification, not task transport.

## External Control Protocol

`src/tmux_team/control_api.py` is a separate, versioned observation surface for external orchestrators. It does not
extend or restrict the existing role-facing MCP experiment. Run the read-only stdio server with:

```bash
tmux-team-control --config .tmux-team/team.toml
```

This is a private newline-delimited JSON-RPC control protocol, not an MCP server. A deployment that exposes these
operations to an MCP client must implement a conforming MCP adapter separately. The private protocol accepts
`control/list`, `control/call`, and `ping`; EOF or a process signal terminates it.

The server advertises only:

- `team_control_status`: bounded role state and durable message counts;
- `team_control_events_since`: normalized event metadata after an integer cursor, with `next` and `has_more`;
- `team_control_milestones`: bounded milestone-tail queries by time, subject, scope, and kind;
- `team_control_obligations`: bounded durable obligations filtered by role and state;
- `team_control_watchdog_status`: bounded watchdog health without descriptions, goals, panes, windows, or process IDs;
- `team_control_evidence`: bounded stable-commit and completed-work summaries without message bodies or paths.

Every operation response carries `api_version = "tmux-team.control/v1"`. Page sizes are limited to 100, status is
limited to 64 roles, request frames are limited to 64 KiB, strings are capped, and milestone scanning is limited to the
newest 2 MiB with explicit truncation and malformed-line counts. External responses use field allowlisting: they exclude
event payloads, milestone bodies/metadata, task body paths, app-server endpoints, thread IDs, tmux pane targets, and
worktree paths. Milestone summaries are bounded but remain untrusted worker-authored content; an external orchestrator
must not treat them as instructions.

The control process opens an existing `team.sqlite` through SQLite URI `mode=ro`, enables `query_only`, and checks the
exact tmux-team schema version. It never calls `Store.connect()`, initializes tables, migrates schema, syncs configured
roles, loads task bodies, or invokes extensions. A missing or mismatched database fails closed; use the matching normal
tmux-team CLI to initialize or migrate state before starting this reader. Status fields and its event cursor are read
from one SQLite snapshot. Event requests above the current maximum cursor return `reset_required`; callers must reset
their cursor after runtime recreation. The cursor does not yet contain a durable database-instance identity.

The milestone operation is deliberately latest-only, not a complete pageable history. Symlinked and non-regular
milestone logs are rejected through an fd-based no-follow open. Normal SQLite read-only access may still coordinate
through existing `team.sqlite-wal` and `team.sqlite-shm` sidecars on live WAL databases; `mode=ro` is a logical SQL
write boundary, not a byte-for-byte filesystem immutability claim. Production deployments must keep the runtime
directory owned by a trusted tmux-team identity.

### Opt-in goal submission

One project-neutral mutation is available only when the operator starts a separate control process with all three
explicit bindings:

```bash
tmux-team-control \
  --config .tmux-team/team.toml \
  --allow-mutations \
  --project-id opaque-project-id \
  --actor external-gateway
```

This adds only `team_control_submit_goal` to the advertised catalog. It accepts a bounded objective, optional bounded
constraints and acceptance criteria, and a required idempotency key. The recipient is fixed to the configured
`orchestrator`; callers cannot choose a role, path, pane, command, environment, or transport. The operation creates one
durable high-priority message through `TeamService` and requests the role's configured structured wake.

Idempotency records live in the authoritative team database. Repeating the same request returns the stored result
without creating another message; reusing a key for different content is rejected; an in-progress request returns a
conflict. Expected team-state rejection is stored and replayed. Unexpected failures roll back team-state changes and
leave the reserved request in `running`, requiring operator review rather than guessing whether execution occurred.
Completion audit events record only actor, opaque project ID, request ID, action, result, and time—not the goal payload.

Without `--allow-mutations`, the process remains the exact read-only/query-only reader described above. Supplying
`--project-id` or `--actor` without the mutation flag fails closed. This protocol still is not an approval mechanism:
deployments must place their own confirmation and authorization boundary in front of the opt-in process.

## Permissions

`--role-yolo` is a breakglass path. It passes Codex's native `--dangerously-bypass-approvals-and-sandbox` flag to managed role panes.

The normal hardening direction is:

- use role-specific Codex profiles for model, effort, sandbox, and approval settings;
- keep tmux panes as visibility/takeover surfaces, not security boundaries;
- enforce role intent through tmux-team policy, including sender, inbox, notify, pane capture, stable approval, and lifecycle actions;
- prefer MCP tools for role message operations when that becomes ergonomic, so roles do not need broad shell execution just to use tmux-team;
- keep task bodies in SQLite/message files, not wake prompts;
- use OS/process isolation such as containers, remote workers, or separate users for adversarial roles.

Implemented policy basics live in `src/tmux_team/policy.py`. Future credential storage or stronger isolation should keep the same local-control-plane shape instead of adding a second coordinator.
