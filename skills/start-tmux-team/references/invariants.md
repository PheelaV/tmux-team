# Invariants

Follow these constraints when starting or operating tmux-team.

## Control Plane

The Codex session that invokes the skill is the operator control session.

- Name the launcher/operator tmux window `tt-control`.
- Do not treat `tt-control` as a managed role.
- Do not route `tmux-team send` work to `tt-control` unless the operator explicitly adds it as a role.

## App Server

The app-server is isolated infrastructure.

- Keep it in its own tmux window named `tt-app-server`.
- Do not group it with role agents.
- Use it for app-server `turn/start` wake delivery.

## Role Layout

The default role layout is grouped:

```text
tt-agents window
  pane 0: orchestrator
  pane 1: implementer
  pane 2: collector
  pane 3: trainer
```

Use `--agent-layout grouped` unless the user asks for another layout. `--agent-layout separate-windows` is the current alternate.

## Delivery

Never use tmux stdin as the production wake path for Codex roles.

- Do not paste task bodies into panes.
- Do not use `tmux send-keys` for normal Codex wake.
- Use app-server `turn/start`.
- Durable task content must be claimed from the tmux-team inbox.
- A role handles work as: `inbox next -> ack -> do work -> complete -> inbox next` until there is no pending work.

## Role Permissions

Autonomous role-to-role messaging requires role panes that can run the `tmux-team` control CLI.

- Prefer a Codex role profile when available.
- Use `--role-yolo` only when the operator accepts allow-all Codex execution for managed role panes.
- Do not treat an approval prompt as successful notification.

## State

`.tmux-team/team.toml` and `team.sqlite` are the source of truth. Operator-facing team, role, and lifecycle configuration is TOML. Tmux is the view/control surface.

## Sleep

Use `tmux-team sleep` to snapshot role state, pane targets, and app-server bindings before tearing down managed windows. Sleep must leave `tt-control` alive by default and pauses active/draining roles unless explicitly told not to.
