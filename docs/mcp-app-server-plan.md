# MCP/App-Server Control Surface Plan

## Current Shape

`tmux-team` already has the hard part of Codex delivery: pane-resident roles run as remote TUIs connected to `codex app-server`, while `tmux-team` submits short app-server `turn/start` wake prompts. Durable task content stays in SQLite and message files.

Today that wake prompt tells the role to run shell commands such as `tmux-team inbox next`, `ack`, and `complete`. The smallest useful MCP path is to keep app-server wake delivery exactly as it is, but replace role shell execution with a narrow tool surface backed by the existing `Store`.

## Smallest Useful Integration

The current first pass adds a dependency-free MCP-shaped facade over `Store`:

- `src/tmux_team/mcp_server.py` defines tool metadata and pure `call_tool(store, conn, name, arguments)` dispatch.
- The same module includes a line-delimited JSON-RPC stdio skeleton for prototype wiring and tests.
- It intentionally does not add a new state model, lifecycle hook, bootstrap policy, or external MCP SDK dependency.

The prototype JSON-RPC methods are:

- `initialize`
- `tools/list`
- `tools/call`
- `ping`
- `shutdown`

This is not a claim of full MCP transport compliance. It is a stable local adapter shape that can later be wrapped with a real MCP server package when that dependency is worth taking.

## Tool Set

| Tool | Purpose | Store operation |
| --- | --- | --- |
| `team_status` | Return team name, runtime path, role states, queue counts, and app-server binding status. | `list_roles`, `active_counts`, `resolve_role_app_server` |
| `team_inbox_next` | Claim one durable message for a role and optionally include the body text. | `claim_next` |
| `team_ack` | Mark a role-addressed message acknowledged. | `ack_message` |
| `team_complete` | Mark a role-addressed message completed with status and summary. | `complete_message` |
| `team_send` | Queue a durable message to another role and optionally wake it. | `create_message`, app-server wake |
| `team_notify` / `team_wake` | Submit an app-server wake turn for pending role work. | `notify_role(..., "app-server-turn")` |

The MCP surface should not expose `bootstrap`, `sleep`, `codex bind`, stable approval, role state mutation, raw tmux commands, or `send-keys`.

## App-Server Flow

The near-term role loop becomes:

```text
operator or role calls team_send
  -> Store writes a durable message
  -> team_wake submits Codex app-server turn/start
  -> remote TUI receives a short wake prompt
  -> role calls team_inbox_next through MCP
  -> role calls team_ack
  -> role does the work
  -> role calls team_complete
  -> role repeats team_inbox_next until no message is returned
```

The wake prompt remains small and non-authoritative. It tells Codex that work exists; the MCP tool returns the actual message body from the durable store.

## Why This Is Narrower Than Shell CLI

The shell CLI is an operator and test surface. Giving a role permission to run it means granting local process execution plus every currently registered subcommand.

An MCP tool server can be narrower:

- only the message and wake operations are callable;
- app-server wake is available, but tmux stdin injection is not;
- lifecycle operations stay operator-only;
- future policy can derive actor identity at the server boundary instead of trusting `--role` and `--from` flags;
- Codex role profiles can allow this one MCP server without broad shell command prefixes.

This is still not a hard security boundary by itself. Until per-role credentials and policy exist, the local stdio server inherits the privileges of the user that starts it.

## Prototype Usage

Run the skeleton directly for local experiments:

```bash
python -m tmux_team.mcp_server --config .tmux-team/team.toml
```

Example request:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"team_status","arguments":{}}}
```

Example role send without waking:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"team_send","arguments":{"to":"implementer","from":"orchestrator","summary":"Fix failing test","body":"See failing test output.","wake":false}}}
```

## Next Steps

1. Wire the same tool definitions into a real MCP stdio server once an SDK dependency is justified.
2. Add a Codex role profile that exposes only the tmux-team MCP server and does not allow broad `tmux-team` shell prefixes.
3. Add per-role credentials and policy checks before treating MCP as an authorization boundary.
4. Add policy checks and per-role actor identity at the MCP boundary.
5. Keep app-server wake submission as notification only; completion must always come from explicit `team_complete`.
