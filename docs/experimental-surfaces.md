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
