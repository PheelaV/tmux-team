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
tmux-team broadcast --from orchestrator --summary "checkpoint" --body "Report status and blockers." --exclude orchestrator
tmux-team broadcast --from orchestrator --summary "collector check" --body "Report test status." --only collector
tmux-team obligation start --role collector --summary "Monitor verification" --next-update-in 15m
tmux-team pane capture collector --lines 120 --offset 40
tmux-team watchdog
tmux-team watchdog run --once --delivery app-server-turn --notify-role orchestrator
tmux-team watchdog start --name default --interval 15m --notify-role orchestrator --delivery app-server-turn
tmux-team watchdog list
tmux-team role pause trainer
tmux-team role resume trainer
tmux-team sleep
tmux-team resume
```

Inside spawned role panes, follow the role-specific commands shown in the startup prompt. They include explicit `--role <role>` because Codex tool shells do not always inherit pane-local env, and shared worktrees are ambiguous. Short commands such as `tmux-team memory show` and `tmux-team inbox next` are fine only when role discovery works. Use explicit `--config` as an override from the control plane or scripts.

Context recovery should use Codex `SessionStart` hooks, not longer wake prompts. Configure the hook for `startup|resume|clear|compact` and have it print `tmux-team codex session-context`. That output is the same operating contract as the initial role startup prompt, not a new task; it restores role, config, scratchpad, pending count, contract version, and the role loop after context reset or compaction.

Do not reread the full skill on ordinary app-server wakes when the current tmux-team role contract version and role loop are already loaded in context. Reread the skill on startup, resume after sleep, SessionStart recovery, explicit operator request, or contract/version mismatch.

When recovery fidelity matters, record operator metadata with `tmux-team operator bind --pane <pane> --codex-thread-id <thread-id>` if the operator thread id is known. `tmux-team sleep` snapshots operator metadata and configured role Codex launch settings; `tmux-team resume` replays known model, reasoning effort, profile, raw Codex config, and YOLO settings. Treat TUI-only state such as `/fast` as unknown unless Codex exposes it through explicit config.

Scratchpad memory is mandatory role state. It preserves long-term goals across context compression, sleep/resume, and pane restarts. It is also an observability surface for the role itself, other agents, and the human overseer. Keep the most recent and important state at the top.

Role loop on every startup or wake:

1. Confirm the tmux-team role contract is loaded; use `tmux-team codex session-context --role <role>` after context reset instead of rereading the full skill on every ordinary wake.
2. Run `tmux-team memory show --role <role>` unless the startup prompt or environment makes short commands reliable.
3. Run `tmux-team inbox next --role <role>` unless the startup prompt or environment makes short commands reliable.
4. If no message exists, park. Do not append routine "still idle" memory.
5. If a message exists, ack it.
6. Compare message instructions against scratchpad boundaries. If they conflict, stop and ask the orchestrator.
7. Use `tmux-team todo` for active-message substeps that should survive context reset while the message is active.
8. Do the work.
9. Before long work or completion, update memory only if durable state changed materially: active task, blocker, changed boundary, running job, stable input, owned artifact, final result, or next action. Use `tmux-team memory append --role <role> --body "<concise durable update>"` when role discovery is not guaranteed.
10. Complete or supersede open todos, then complete the message with a concise result. Use `--summary` for the one-line result and `--body` or `--body-file` for detailed evidence when needed.

Todos are role-owned checklist state for the active inbox message. They are not assignments, scratchpad memory, milestones, or messages to other roles. Use them when a message has several execution steps, when the role is about to do work that could be interrupted, or when a context reset would otherwise lose the active subplan:

```bash
tmux-team todo add --role <role> --message <message-id> "Run focused regression"
tmux-team todo done --role <role> <todo-id>
tmux-team todo supersede --role <role> <todo-id> "Run broader verification instead"
tmux-team todo recover --role <role>
```

If a todo is obsolete because the plan changed, supersede it instead of marking it done. `tmux-team inbox complete` refuses to finish a message while open todos remain unless the caller explicitly passes `--allow-open-todos`.

Use this memory score before appending:

- 3 points: active task changed, blocker appeared/resolved, boundary changed, long-running job started/stopped, final result changes next action.
- 2 points: stable input changed, important artifact/report was produced, handoff decision was made.
- 1 point: commit/dirty status changed, test result observed, minor status detail.
- 0 points: repeated startup, no pending inbox, routine command output, transient search result, "still waiting" with no new fact.

Append only when the score is 3 or higher, or when the orchestrator explicitly asks for a memory update. Combine low-score facts into the next high-value update instead of writing separate notes.

Do not use scratchpad memory as a full chat log, command transcript, reasoning dump, temporary grep store, duplicate report body, routine startup/parking log, or place to append full replacement sections. Reports go in files; memory points to them and records the conclusion.

Milestones are the broad operator timeline. The orchestrator should record concise milestones for team start, task routing, accepted evidence, blocker found/resolved, tests passing, stable commit approval, sleep/resume, and role resize:

```bash
tmux-team milestone add --kind result --summary "Targeted tests passed after implementer fix" --subject-role implementer --tag test
tmux-team milestone add --kind routing --summary "Team started" --team
tmux-team milestone list --today
tmux-team milestone list --since -4h
```

Milestones should separate writer from subject. Use `--subject-role ROLE` when the event is about one or more roles, and `--team` when it is team-wide. The writer is recorded separately as `recorded_by`.

Do not use milestones as chat, scratchpad memory, task transport, or command transcripts. Use them for "what happened today?" and "what changed while I was away?" operator summaries.

Non-orchestrator roles should not call `tmux-team milestone add` by default. They report evidence, blockers, and results by completing their inbox message back to the orchestrator. The orchestrator decides whether that result deserves a milestone.

When completing delegated work, use `tmux-team inbox complete ... --reply-to-sender` so the original sender is woken through the normal message path. Do not use `--reply-to-sender` for pure acknowledgement/bookkeeping messages that would create reply loops. When dispatching fan-out work, still keep the message id printed by `tmux-team send`; it is the durable handle for status and audit.

Orchestrator unblock-first rule: when new operator or role information can unblock another role's safe setup work, route a bounded handoff promptly unless doing so would create irreversible external effects or violate an explicit safety gate. Prefer a gated prep message over waiting for local review or bookkeeping to finish:

```bash
tmux-team send \
  --to collector \
  --priority high \
  --summary "Prepare next run; launch gated on stable approval" \
  --correlation-key next-run-prep \
  --body "Start preflight/setup now. Do not launch until stable approval or explicit release arrives."
```

State the hold condition clearly, continue validation, then send an approve/cancel/update follow-up. Do not block downstream prep on redundant verification already supplied by a worker unless forwarding the handoff would cross a safety boundary.

When dispatching multi-step work, choose one stable `--correlation-key` per logical work thread and reuse it for retries, follow-ups, and verification. Before sending a follow-up, check `tmux-team status --verbose` or `tmux-team inbox list --verbose` for existing active or completed work. Do not invent near-synonym keys for the same task; use `--allow-duplicate` only when redundant independent work is deliberate.

Use `tmux-team broadcast` when the orchestrator needs to send the same checkpoint or instruction to several roles. Broadcast queues one normal message per recipient, so each role still has its own message id, ack, completion, and optional reply. Use either `--only` for a positive role filter or `--exclude` for a negative role filter; those switches are mutually exclusive. Do not treat broadcast as a shared task.

Use `tmux-team pane capture <role> --lines N --offset N` for live supervision when memory and messages are not enough. `--lines` or `--limit` controls how much history to print; `--offset` pages back from the newest output. Pane capture lets the orchestrator or operator inspect recent visible pane output for progress, stuck commands, approval prompts, or intermediate test output. Pane capture is observation only; do not use it as proof of delivery or completion.

Use native watchdog runners for repeated durable-state checks. Bare `tmux-team watchdog` is a single-shot report-only checker. Delivery-enabled `watchdog run --once` or `watchdog start` creates durable inbox pressure and wakes the configured `--notify-role` while suppressing duplicate active escalations by correlation key. `tmux-team watchdog start --name <name> --interval <duration>` opens or reuses the visible `tt-watchdogs` tmux window, gives each runner a titled pane such as `tt-watchdog-default`, records runner state in SQLite, and surfaces it through `watchdog list`, `status --verbose`, `dashboard`, and `pane list --all`. Use `watchdog update` to change interval, goal, scope, delivery, or notify target. Use `watchdog pause/resume` for non-terminal deferral with a reason and optional review time; use `watchdog stop` for terminal shutdown. Do not treat watchdog runners as role agents or obligations.

For freeze, checkpoint, restart, or do-not-continue instructions, send an urgent message. App-server wakes include the highest-priority pending sender, priority, and summary; urgent wakes tell the role to stop at the current safe point and claim the urgent message before continuing other work. Keep the full instruction in the durable message body.

Use `tmux-team sleep` to snapshot role/app-server bindings, running watchdog runners, operator metadata, and configured Codex launch settings before tearing down managed role, app-server, and watchdog windows. It leaves `tt-control` alive by default and pauses active/draining roles unless `--no-pause-roles` is used.

Use `tmux-team resume` to restore a slept team from `.tmux-team/runtime/sleeps/latest.toml` or `--snapshot PATH`. Resume recreates managed role panes and launches each with `codex resume <saved-session>` so the Codex conversations captured in the sleep snapshot continue instead of starting fresh sessions, then reinstantiates running watchdog runner panes from durable runner state. If no graceful sleep snapshot exists, resume can build a recovery snapshot from `team.toml` and SQLite runtime state. Use `--dry-run` first when inspecting a migration or remote host.

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
