---
name: start-tmux-team
description: Bootstrap and operate a pane-resident Codex agent team with tmux-team. Use when the user asks to start, spawn, initialize, resize, or coordinate a tmux/Codex agent team; wants a default orchestrator/implementer/collector/trainer setup; or wants reliable app-server wake delivery without tmux prompt injection.
---

# Start Tmux Team

Use `tmux-team` as the control plane for visible Codex roles in tmux. The Codex session that invokes this skill is the operator control session. Do not manually paste role work into panes with `tmux send-keys`.

## Reference Routing

Use this file for ordinary bootstrap, role startup, and day-to-day operation.

Read `references/invariants.md` when changing tmux-team behavior, debugging delivery/layout/lifecycle/recovery, migrating an existing team, or resolving a conflict between runtime state and expectations. Read `references/team-shapes.md` only for non-default role shapes.

For full command details, use the repo docs: `docs/cli-reference.md`, `docs/receiving-and-hooks.md`, `docs/live-demo.md`, and `docs/invariants.md`.

## Preflight

Verify the CLI exists before running it:

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

## Bootstrap

Start from the target project root:

```bash
tmux-team bootstrap --project-root . --goal "USER_GOAL"
```

Default roles:

```text
orchestrator, implementer, collector, trainer
```

Ask only when the project root, goal, or team shape is genuinely ambiguous. `--goal` and `--goal-file` are operator-to-orchestrator inputs only: objective, boundaries, and success criteria. Do not write them as role runbooks; the orchestrator decomposes work into role inbox messages.

For explicit shape or layout:

```bash
tmux-team bootstrap \
  --project-root /path/to/project \
  --session tt-my-team \
  --roles orchestrator,implementer,collector,trainer \
  --agent-layout grouped \
  --goal-file /path/to/goal.md
```

Use `--agent-layout separate-windows` only when the operator wants one tmux window per role.

For existing multi-worktree teams, preserve role worktrees:

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

If user instructions or exports mention separate worktrees but no mapping is provided, stop and ask for the generic role-to-path mapping. Do not silently launch every role from `--project-root`.

For autonomous role-to-role messaging, use an explicit role execution policy:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

Prefer `--role-profile`. Use `--role-yolo` only when the operator accepts Codex allow-all/no-sandbox mode for managed role panes. It does not change the operator control session.

Bootstrap creates or updates:

- `tt-control` for the operator launcher, not a managed role;
- `tt-app-server` for isolated `codex app-server` transport;
- visible role panes in grouped `tt-agents` by default;
- per-role Codex remote TUI sessions launched with `codex --cd <role-worktree> --remote ...`;
- `.tmux-team/team.toml`, `team.sqlite`, pane metadata, env discovery files, and scratchpad memory;
- the initial orchestrator inbox message and app-server wake when a goal is provided.

After startup, report the tmux session, config path, app-server endpoint, role thread IDs, and attach command:

```bash
tmux attach -t <session>
```

## Operator Commands

Use durable state first:

```bash
tmux-team status --verbose
tmux-team send --to implementer --summary "..." --body-file task.md --notify-method app-server-turn
tmux-team broadcast --from orchestrator --summary "checkpoint" --body "Report status and blockers." --exclude orchestrator
tmux-team inbox list --role orchestrator --state pending --verbose
tmux-team pane capture collector --lines 120 --offset 40
tmux-team milestone list --today
tmux-team obligation start --role collector --summary "Monitor verification" --next-update-in 15m
tmux-team watchdog start --name default --interval 15m --notify-role orchestrator --delivery app-server-turn
tmux-team role pause trainer
tmux-team sleep
tmux-team resume
```

Inside managed role panes, prefer short commands such as `tmux-team memory show` and `tmux-team inbox next` when role discovery works. Use explicit `--role <role>` or `--config <path>` when running from scripts, shared worktrees, or uncertain shells.

## Role Runtime Contract

Each spawned role must have this skill available in its active `CODEX_HOME`. The role startup prompt loads the skill, reads scratchpad memory, then claims inbox work or parks.

Use Codex `SessionStart` hooks for `startup|resume|clear|compact` recovery. The hook should print `tmux-team codex session-context`; that output restores the role, config, scratchpad path, pending count, contract version, and loop. It is not a task body.

Do not reread the full skill on ordinary app-server wakes when the role contract and loop are already loaded. Reread on first startup, resume after sleep, SessionStart recovery, explicit operator request, or contract/version mismatch.

App-server wakes are blunt interrupts. They say durable inbox work exists; the task body must be claimed from SQLite inbox state.

Role loop on startup or wake:

1. Confirm the tmux-team role contract is loaded; after context reset, run `tmux-team codex session-context`.
2. Run `tmux-team memory show` before claiming work.
3. Run `tmux-team inbox next`.
4. If no message exists, park without writing routine idle memory.
5. Ack the claimed message.
6. Compare message instructions with scratchpad boundaries; ask orchestrator on conflict.
7. Use `tmux-team todo` for active-message substeps that should survive reset.
8. Do the work from the role worktree.
9. Before long work, completion, or parking, update memory only for material durable state changes.
10. Complete or supersede open todos, then complete the inbox message with a concise result.
11. Repeat `tmux-team inbox next` until no pending work remains.

Only `inbox next` is the role's drain check. Do not declare an inbox clear from
a concrete-state list. For read-only supervision, `inbox list --state pending`
matches `status pending=N` and includes queued, notified, retrying, and expired
claimed work.

## Memory, Todos, And Milestones

Scratchpad memory is mandatory durable role state. Keep the newest and most important state near the top. Use it for long-term goals, role boundaries, current task, blockers, running jobs, stable inputs, owned artifacts, and next action.

Append memory only when the update is likely to matter after context reset, sleep/resume, or operator handoff. As a rule of thumb, append for active-task, blocker, boundary, long-running-job, or final-result changes; do not append routine startup, no-pending, "still waiting", command transcripts, temporary search output, or full report bodies.

Use todos only for the active inbox message:

```bash
tmux-team todo add --message <message-id> "Run focused regression"
tmux-team todo done <todo-id>
tmux-team todo supersede <todo-id> "Run broader verification instead"
tmux-team todo recover
```

`tmux-team inbox complete` refuses open todos unless `--allow-open-todos` is explicit. Supersede obsolete todos instead of marking undone work as done.

Milestones are the broad operator timeline. The operator/control plane and orchestrator record them by default; other roles report evidence through inbox completion. Record team start, task routing, accepted evidence, blockers found/resolved, tests passing, stable commit approval, sleep/resume, and role resize.

```bash
tmux-team milestone add --kind result --summary "Targeted tests passed after implementer fix" --subject-role implementer --tag test
tmux-team milestone add --kind routing --summary "Team started" --team
```

## Messaging And Supervision

Complete delegated work with `tmux-team inbox complete ... --reply-to-sender` so the sender receives a normal durable completion notice. Do not use it for pure bookkeeping that would create reply loops.

Material delegated results must not terminate at an intermediate non-orchestrator role. If a completion notice affects the team goal, stable commit, blocker state, external run state, or operator-visible result, send a concise upward report to `orchestrator` or complete the still-active orchestrator-owned task with `--reply-to-sender`.

Use one stable `--correlation-key` per logical work thread across retries, follow-ups, and verification. Check `status --verbose` or `inbox list --verbose` before sending duplicate-looking work.

Use `tmux-team broadcast` for the same checkpoint or instruction to multiple roles. It creates one durable message per recipient. Use either `--only` or `--exclude`, not both.

Use `tmux-team pane capture <role> --lines N --offset N` to inspect live pane output when memory and messages are insufficient. Pane capture is observation only; it is not proof of delivery, acknowledgement, or completion.

Use `tmux-team watchdog` for report-only durable-state checks. Use delivery-enabled `watchdog run --once` or `watchdog start` when findings should create durable inbox pressure and wake a role. Runners are visible panes in `tt-watchdogs`, not role agents; change them with `watchdog update/pause/resume/stop`.

Use `tmux-team sleep` to snapshot role/app-server bindings, operator metadata, running watchdogs, and configured Codex launch settings before tearing down managed role, app-server, and watchdog windows. Use `tmux-team resume` to restore from the latest snapshot or durable runtime state after abrupt shutdown. Treat TUI-only Codex state such as `/fast` as unknown unless explicit config records it.

Bootstrap and resume set tmux truecolor options on the managed session by default. Use `--no-truecolor` only if the operator's terminal stack mis-renders color.

## Safety Rules

- Keep agents in visible tmux panes.
- Keep `tt-control`, `tt-app-server`, and role panes isolated by purpose.
- Use app-server `turn/start` for production wake delivery.
- Never use `tmux send-keys` as the production wake path.
- Keep task bodies in the inbox, not wake prompts or scratchpads.
- Preserve user takeover: role panes and infrastructure panes stay inspectable.
- If bootstrap partially fails, report the failed command and current tmux session name.
