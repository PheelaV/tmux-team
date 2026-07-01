# tmux-team Invariants

These are product constraints, not implementation suggestions.

## Control Plane

The Codex session that starts `tmux-team bootstrap` is the control-plane.

- It is named `control-plane` in tmux.
- It is not a managed role agent.
- It is not a delivery target for `tmux-team send`.
- It remains available for the human operator to inspect, intervene, resize the team, and run control commands.

Do not route agent-to-agent work into the control-plane conversation. This prevents managed agents from interrupting the human prompt composer.

## App Server

Codex app-server is infrastructure and stays isolated.

- It runs in its own tmux window named `app-server`.
- It is not grouped with role agents.
- It is the wake transport for Codex roles through app-server `turn/start`.
- If it exits, the pane stays open so the operator can inspect the failure.

## Role Agents

Role agents remain visible in tmux.

- Agents are not hidden background workers.
- Each role has a live Codex TUI pane.
- Each role receives wake turns through Codex app-server, not through tmux keystrokes.
- The durable task body lives in the `tmux-team` inbox, not in pane text.

The default role set is:

```text
orchestrator, implementer, collector, trainer
```

## Role Permissions

Role agents must be able to run the `tmux-team` control CLI if they are expected to message other roles autonomously.

- A normal Codex approval profile can park a role on `tmux-team send`, `notify`, or local app-server access.
- Parked approval prompts are treated as operational blockage, not successful delivery.
- Use a narrow Codex role profile when available.
- Use `tmux-team bootstrap --role-yolo` only when the role panes are already inside an external trust boundary you accept for this project.

`--role-yolo` launches managed role Codex TUIs with Codex `--dangerously-bypass-approvals-and-sandbox`. It does not change the control-plane session or the app-server process.

## Layout

The default layout is:

```text
control-plane window
app-server window
agents window
  pane 0: orchestrator
  pane 1: implementer
  pane 2: collector
  pane 3: trainer
```

The grouped `agents` window is tiled so the operator can oversee the role fleet at once.

Other layouts may be supported by configuration, but they must preserve the control-plane and app-server isolation rules. The current alternate layout is `separate-windows`, which creates one tmux window per role.

## Delivery

Never use tmux stdin as the production wake path for Codex roles.

- Do not paste task bodies into panes.
- Do not use `tmux send-keys` to wake a pane that a human might be typing in.
- Do not rely on pane capture to prove delivery.
- Copy mode, active composers, approval prompts, and SSH disconnects must not corrupt messages.

Production Codex wake delivery is:

```text
SQLite inbox message
  -> app-server turn/start
  -> role Codex TUI receives wake turn
  -> role claims durable inbox item
```

`send-keys` is a debug/unsafe path only and must fail closed when tmux reports copy mode.

## State

The config and runtime store are the source of truth.

- `.tmux-team/team.toml` records role names, pane targets, app-server endpoint, and Codex thread IDs.
- Operator-facing team, role, and lifecycle configuration is TOML.
- `team.sqlite` records messages, notifications, role state, events, and stable commits.
- Tmux is the view/control surface, not the durable state store.

If a role pane target changes, config must change with it.

## Sleep

`tmux-team sleep` is the lifecycle boundary for tearing down a visible team.

- It snapshots role state, pane targets, tmux session/window/pane IDs, and app-server thread bindings before teardown.
- It writes the snapshot as TOML under `.tmux-team/runtime/sleeps/`.
- It tears down managed role/app-server windows by default and leaves `control-plane` alive.
- It marks active/draining roles paused by default so stale bindings do not keep accepting work.

## Resizing

Team shape is configurable.

- Use `tmux-team role pause`, `resume`, `drain`, `retire`, or `fail` for runtime state changes.
- Do not silently repurpose a role name for a different responsibility.
- Scaling down should preserve message history and role state.
- A role that is paused or draining must not receive normal new work unless explicitly forced.
