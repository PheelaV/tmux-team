# MCP/App-Server Surface

`tmux-team` has a dependency-free MCP-shaped facade over `TeamService` in `src/tmux_team/mcp_server.py`.

It exposes only the role-facing message loop:

- `team_status`
- `team_inbox_next`
- `team_ack`
- `team_complete`
- `team_send`
- `team_notify`
- `team_wake`

Run it for local experiments:

```bash
python -m tmux_team.mcp_server --config .tmux-team/team.toml
```

This is not a full MCP transport claim. It is a narrow adapter that can be wrapped with a real MCP SDK later if the dependency becomes worth it.
