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

`--goal` and `--goal-file` are operator-to-orchestrator inputs only. They should describe the objective, boundaries, and success criteria. Do not write them as role runbooks; role-specific instructions belong in orchestrator-created inbox messages.

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

If the user is migrating an existing multi-worktree setup, preserve role worktrees during bootstrap. Use generic role mapping like:

```bash
tmux-team bootstrap \
  --project-root /repo/main \
  --session tt-my-team \
  --roles orchestrator,implementer,collector,trainer \
  --role-worktree orchestrator=/repo/main \
  --role-worktree implementer=/repo/main \
  --role-worktree collector=/repo/main-collector \
  --role-worktree trainer=/repo/main-trainer \
  --allow-shared-worktree orchestrator,implementer \
  --goal-file /path/to/goal.md
```

If role exports or user instructions mention separate worktrees but no mapping is provided, stop and ask for the generic role-to-path mapping. Do not silently launch every role from `--project-root`.

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
- opens role panes in one tiled `tt-agents` window by default using `codex --cd <role-worktree> --remote ...`;
- binds each role pane to the team config and role using process env, worktree pointer files, and tmux pane metadata;
- creates one scratchpad memory file per role;
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

Inside spawned role panes, the team config and role are already discoverable. Prefer short role commands such as `tmux-team memory show`, `tmux-team inbox next`, `tmux-team inbox ack <message-id>`, and `tmux-team inbox complete <message-id> --reply-to-sender`. Use explicit `--config` or `--role` only as an override from the control plane or scripts.

Context recovery should use Codex `SessionStart` hooks, not longer wake prompts. Configure the hook for `startup|resume|clear|compact` and have it print `tmux-team codex session-context`. That output is the same operating contract as the initial role startup prompt, not a new task; it restores role, config, scratchpad, pending count, and the role loop after context reset or compaction.

Scratchpad memory is mandatory role state. It preserves long-term goals across context compression, sleep/resume, and pane restarts. It is also an observability surface for the role itself, other agents, and the human overseer. Keep the most recent and important state at the top.

Role loop on every startup or wake:

1. Run `tmux-team memory show`.
2. Run `tmux-team inbox next`.
3. If no message exists, park. Do not append routine "still idle" memory.
4. If a message exists, ack it.
5. Compare message instructions against scratchpad boundaries. If they conflict, stop and ask the orchestrator.
6. Do the work.
7. Before long work or completion, update memory only if durable state changed materially: active task, blocker, changed boundary, running job, stable input, owned artifact, final result, or next action. Use `tmux-team memory append --body "<concise durable update>"`.
8. Complete the message with a concise result. Use `--summary` for the one-line result and `--body` or `--body-file` for detailed evidence when needed.

Use this memory score before appending:

- 3 points: active task changed, blocker appeared/resolved, boundary changed, long-running job started/stopped, final result changes next action.
- 2 points: stable input changed, important artifact/report was produced, handoff decision was made.
- 1 point: commit/dirty status changed, test result observed, minor status detail.
- 0 points: repeated startup, no pending inbox, routine command output, transient search result, "still waiting" with no new fact.

Append only when the score is 3 or higher, or when the orchestrator explicitly asks for a memory update. Combine low-score facts into the next high-value update instead of writing separate notes.

Do not use scratchpad memory as a full chat log, command transcript, reasoning dump, temporary grep store, duplicate report body, routine startup/parking log, or place to append full replacement sections. Reports go in files; memory points to them and records the conclusion.

Milestones are the broad operator timeline. The orchestrator should record concise milestones for team start, task routing, accepted evidence, blocker found/resolved, tests passing, stable commit approval, sleep/resume, and role resize:

```bash
tmux-team milestone add --kind result --summary "Targeted tests passed after implementer fix" --tag test
tmux-team milestone list --today
tmux-team milestone list --since -4h
```

Do not use milestones as chat, scratchpad memory, task transport, or command transcripts. Use them for "what happened today?" and "what changed while I was away?" operator summaries.

Non-orchestrator roles should not call `tmux-team milestone add` by default. They report evidence, blockers, and results by completing their inbox message back to the orchestrator. The orchestrator decides whether that result deserves a milestone.

When completing delegated work, use `tmux-team inbox complete ... --reply-to-sender` so the original sender is woken through the normal message path. Do not use `--reply-to-sender` for pure acknowledgement/bookkeeping messages that would create reply loops. When dispatching fan-out work, still keep the message id printed by `tmux-team send`; it is the durable handle for status and audit.

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
