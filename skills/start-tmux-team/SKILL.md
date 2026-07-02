---
name: start-tmux-team
description: Bootstrap and operate a pane-resident Codex agent team with tmux-team. Use when the user asks to start, spawn, initialize, resize, or coordinate a tmux/Codex agent team; wants a default orchestrator/implementer/collector/trainer setup; or wants reliable app-server wake delivery without tmux prompt injection.
---

# Start Tmux Team

## Workflow

Before starting a team, read `references/invariants.md`.

Before running `tmux-team`, verify the CLI exists:

```bash
command -v tmux-team
```

If it is missing, stop and tell the user:

```text
tmux-team CLI is not installed. Install it with:
uv tool install git+https://github.com/PheelaV/tmux-team.git

or:
pipx install git+https://github.com/PheelaV/tmux-team.git
```

Use `tmux-team bootstrap` as the entry point. Do not manually type prompts into role panes with `tmux send-keys`.

The Codex session that invoked this skill is the operator control session. Bootstrap names its tmux window `tt-control`, keeps `tt-app-server` isolated, and uses a grouped `tt-agents` window by default. If the launcher is already inside tmux, bootstrap uses that tmux session by default.

Default team shape:

```text
orchestrator, implementer, collector, trainer
```

If the user gives a goal, pass it with `--goal` for short text or `--goal-file` for longer text. If the user gives no team shape, use the default. Ask only when the target project root or goal is genuinely ambiguous.

## Start A Team

From the target project root:

```bash
tmux-team bootstrap --project-root . --goal "USER_GOAL"
```

For an explicit team:

```bash
tmux-team bootstrap \
  --project-root /path/to/project \
  --session tt-my-team \
  --roles orchestrator,implementer,collector,trainer \
  --agent-layout grouped \
  --goal-file /path/to/goal.md
```

Use `--agent-layout separate-windows` only when the user explicitly asks for one tmux window per role.

When roles must message or notify each other without operator approvals, use one of:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

Prefer `--role-profile` when the user already has a suitable Codex profile. Use `--role-yolo` only when the operator accepts Codex allow-all mode for managed role panes. It passes `--dangerously-bypass-approvals-and-sandbox` to role TUIs only; the `tt-control` session remains separate.

What bootstrap does:

- uses the current tmux session when launched inside tmux, or starts a tmux session if needed;
- names the launcher/operator window `tt-control`;
- starts a visible `tt-app-server` tmux window running `codex app-server`;
- opens role panes in one tiled `tt-agents` window by default using `codex --remote ...`;
- waits for each role TUI to create a loaded app-server thread;
- writes `.tmux-team/team.toml` with app-server endpoint and discovered thread IDs;
- queues the initial goal to `orchestrator` when a goal is provided;
- wakes the orchestrator through app-server `turn/start`, not terminal input.

## After Startup

Report:

- tmux session name;
- app-server endpoint;
- config path;
- role thread IDs;
- how to attach: `tmux attach -t <session>`.

Use normal operations after startup:

```bash
tmux-team status
tmux-team send --to implementer --summary "..." --body-file task.md --notify-method app-server-turn
tmux-team role pause trainer
tmux-team role resume trainer
tmux-team sleep
```

Use `tmux-team sleep` to snapshot role/app-server bindings and tear down managed role/app-server windows. It leaves `tt-control` alive by default and pauses active/draining roles unless `--no-pause-roles` is used.

## Safety Rules

- Keep agents in tmux panes.
- Keep `tt-control` and `tt-app-server` isolated from role-agent panes.
- Use Codex app-server remote TUI for wake delivery.
- Never use `tmux send-keys` for production wake.
- For autonomous role-to-role messaging, launch role panes with a permissions profile or explicit `--role-yolo`.
- Preserve user takeover: `tt-app-server` and role panes are visible tmux windows.
- If bootstrap fails after creating partial tmux windows, report the exact failed command and current session name.

## Team Shapes

For non-default team shapes or role naming guidance, read `references/team-shapes.md`.
