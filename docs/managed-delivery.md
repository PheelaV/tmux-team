# App-Server Remote TUI Delivery

## Problem

An interactive Codex TUI pane has one stdin stream.

If `tmux-team` writes to that stream while a human or the agent has text in the composer, it can corrupt the prompt. Sending a shorter wake-up prompt reduces payload size but does not remove the race.

Therefore `tmux send-keys` is not a reliable wake-up mechanism for human-visible Codex TUI panes.

## Boundary

Use two delivery classes:

```text
human_visible
  Durable inbox, visible tmux notifications, manual polling, human takeover.

app_server_remote_tui
  The agent is still visible in a tmux pane, but that pane is connected with
  `codex --remote` to a Codex app-server. The service submits turns through
  app-server `turn/start`.
```

## Minimal Wakeable Mode

Use `tmux-team bootstrap` to create this mode by default for Codex roles. Bootstrap starts a visible app-server window, opens each role pane with `codex --remote ...`, waits for the TUI-created app-server thread ID, and records each binding in `.tmux-team/team.toml`.

The minimal wakeable mode starts with a role-bound app-server thread:

```text
tmux-team send
  -> SQLite message queued
  -> tmux-team submits app-server turn/start to the role thread
  -> pane-resident Codex receives the wake turn
  -> Codex claims/acks/completes one durable inbox item
  -> Codex repeats inbox next until no pending messages remain
```

This is the shape tested by the fake app-server unit test. The real Codex task integration still uses `codex exec` as a separate automation smoke test.

## Role Autonomy

App-server wake delivery and Codex command permissions are separate concerns.

If a role only consumes operator-sent work, the default Codex launch profile may be enough. If a role must route work to another role, it must be able to run `tmux-team send` and `tmux-team notify` without parking on approval.

Bootstrap supports two role-only launch options:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

`--role-profile` passes a named Codex profile to managed role TUIs. `--role-yolo` passes Codex `--dangerously-bypass-approvals-and-sandbox` to managed role TUIs. Neither option changes the control-plane session.

## Human Takeover

Tmux remains the view/control surface:

- the role pane is the live Codex UI;
- operator can read history, use copy mode, and type normally;
- `tmux-team` never sends keystrokes into that pane;
- pause/drain controls stop new wake turns before takeover.

The service must stop submitting new turns before takeover when the operator wants exclusive control.

## Later Mode

After the CLI semantics settle, `tmux-team` can use richer Codex clients:

- Python/TypeScript SDK app-server clients;
- Unix socket transport;
- event stream recorded directly into `team.sqlite`.

The core requirement stays the same: wake through Codex protocol, not tmux stdin.
