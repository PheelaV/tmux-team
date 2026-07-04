# tmux-team Invariants

These are product constraints, not implementation suggestions.

## Control Plane

The Codex session that starts `tmux-team bootstrap` is the operator control session.

- Bootstrap names its tmux window `tt-control`.
- It is not a managed role agent.
- It is not a delivery target for `tmux-team send`.
- It remains available for the human operator to inspect, intervene, resize the team, and run control commands.

Do not route agent-to-agent work into the `tt-control` conversation. This prevents managed agents from interrupting the human prompt composer.

## App Server

Codex app-server is infrastructure and stays isolated.

- It runs in its own tmux window named `tt-app-server`.
- It is not grouped with role agents.
- It is the wake transport for Codex roles through app-server `turn/start`.
- If it exits, the pane stays open so the operator can inspect the failure.

## Role Agents

Role agents remain visible in tmux.

- Agents are not hidden background workers.
- Each role has a live Codex TUI pane.
- Each spawned Codex role must have the `start-tmux-team` skill available in its active `CODEX_HOME`.
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

`--role-yolo` launches managed role Codex TUIs with Codex `--dangerously-bypass-approvals-and-sandbox`. It does not change the `tt-control` session or the app-server process.

## Layout

The default layout is:

```text
tt-control window
tt-app-server window
tt-agents window
  pane 0: orchestrator
  pane 1: implementer
  pane 2: collector
  pane 3: trainer
```

The grouped `tt-agents` window is tiled so the operator can oversee the role fleet at once.

Other layouts may be supported by configuration, but they must preserve the `tt-control` and app-server isolation rules. The current alternate layout is `separate-windows`, which creates one tmux window per role.

## Role Worktrees

`--project-root` is the control/config root and default role worktree.

When roles need isolated checkout state, bootstrap must launch them with `--role-worktree ROLE=PATH`, pass that path to Codex with `--cd`, and record the same path in `.tmux-team/team.toml`.

Shared explicitly mapped role worktrees must be explicit with `--allow-shared-worktree ROLE,ROLE`. Dirty tracked files in explicitly mapped worktrees are rejected unless the role is allowed with `--allow-dirty-role ROLE`.

## Role Memory

Each role has a scratchpad memory file declared in config.

- Bootstrap creates missing scratchpads before role Codex panes start.
- A newly spawned role receives a startup prompt to load the `start-tmux-team` skill, read invariants, read memory, then claim inbox work or park.
- Scratchpad memory preserves long-term goals across context compression, sleep/resume, and pane restarts.
- Scratchpad memory is also an observability surface for the role, other agents, and the human overseer.
- Keep the latest and most important state at the top.
- Update memory only when durable state changed materially. Do not write routine startup, parking, no-pending, or "still waiting" notes.
- Use a threshold: append when the update would score at least 3 points, where active task/blocker/boundary/long-running job/final-result changes are 3, stable input/artifact/handoff decisions are 2, minor status/test/dirty-state facts are 1, and routine startup/parking/no-pending chatter is 0.
- Use memory for role, worktree, commit, dirty state, active task, running jobs, owned artifacts, blockers, boundaries, stable inputs, and next action.
- Do not use memory as the queue, chat log, command transcript, reasoning dump, or duplicate report body.
- If inbox instructions conflict with scratchpad boundaries, stop and ask the orchestrator.
- Reset safety is mechanical: use Codex `SessionStart` hooks for `startup|resume|clear|compact` to inject `tmux-team codex session-context` output. The hook restores the same role contract as the startup prompt; it is not a competing task or replacement for inbox work.
- The role contract has a version marker. Ordinary app-server wakes should not cause full skill rereads when the current contract version and role loop are already loaded; reread the full skill on startup, resume after sleep, SessionStart recovery, explicit operator request, or contract/version mismatch.

## Milestone Log

The milestone log is the operator-facing timeline.

- Store broad achievements and state changes in `milestones.jsonl` through `tmux-team milestone add`.
- Use milestones to answer questions like "what happened today?", "what changed in the last 4h?", and "what did this team accomplish while I was away?"
- Record team start, task routing, evidence accepted, blockers found/resolved, tests passing, stable commit approval, sleep/resume, and team resize.
- Keep milestones concise. They are not command transcripts, chat logs, scratchpad replacements, or message transport.
- Only the operator/control plane and orchestrator record milestones by default. Other roles report evidence or blockers through inbox completion; the orchestrator decides whether the result is milestone-worthy.
- Query from the control plane with `tmux-team milestone list --today` or `tmux-team milestone list --since -4h`.

## Role Todos

Todos are role-owned checklist state for active inbox work.

- A todo is scoped to one role and one inbox message.
- Todos are not assignments, message transport, milestones, or scratchpad memory.
- Roles use todos for immediate execution substeps that should survive context compression, pane restart, or sleep/resume while the inbox message is active.
- Use `tmux-team todo add --role ROLE --message MSG_ID "step"` after the role has claimed or acknowledged the message.
- Use `todo done` for completed steps, `todo reopen` for incorrectly closed steps, and `todo supersede` when an obsolete step is replaced by a new step.
- Superseded todos are terminal audit state. Do not mark obsolete work as done just to close the checklist.
- `tmux-team inbox complete` must refuse completion while open todos remain unless the caller passes explicit `--allow-open-todos`.
- `tmux-team inbox next` should point at claimed or acknowledged active work and open todos when there is nothing new to claim.
- `tmux-team status --verbose`, `tmux-team todo recover`, and `tmux-team codex session-context` are the recovery surfaces for active todos.
- The owning role mutates its todos. The orchestrator and operator may inspect todos for supervision.

## Supervision

The operator and orchestrator may inspect managed role panes.

- Use `tmux-team status --verbose` first when aggregate counts are unclear. It must show bounded active message summaries from durable state without scraping panes.
- Use `tmux-team dashboard --once` for a deterministic read-only snapshot of roles, active messages, todos, watches, milestones, memory excerpts, alerts, and optional pane tails.
- Use `tmux-team dashboard` for the live Textual operator dashboard only when the optional `tmux-team[dashboard]` extra is installed.
- Dashboard views are observation surfaces. They must not mutate inbox, todo, watch, milestone, memory, or role state.
- Use `tmux-team pane list --all` to show unmanaged panes in managed role windows. Unmanaged panes must be marked `managed=false`; lifecycle commands must not silently treat them as role panes.
- Use `tmux-team pane capture <role> --lines N --offset N` to read tmux stdout/history for a role.
- Use `tmux-team pane capture <role> --summary` when raw scrollback would flood context; summaries must be generated from bounded capture, use a compact JSON shape, and remain observational only.
- `--lines` or `--limit` controls how much history is printed. `--offset` skips the newest lines so the caller can page back.
- Pane capture is for live progress inspection, stuck-turn diagnosis, and operator overview.
- Pane capture is not a delivery, acknowledgement, or completion mechanism.
- Do not scrape pane output into scratchpad memory or milestones unless it represents a durable result that still belongs there.
- By default, roles can inspect themselves, the orchestrator can inspect all roles, and other cross-role inspection requires explicit policy.

## Delivery

Never use tmux stdin as the production wake path for Codex roles.

- Do not paste task bodies into panes.
- Do not use `tmux send-keys` to wake a pane that a human might be typing in.
- Do not rely on pane capture to prove delivery.
- Copy mode, active composers, approval prompts, and SSH disconnects must not corrupt messages.
- Wake prompts are blunt interrupts, not operating manuals. They should say there is pending work and point at `tmux-team inbox next`; the role skill, scratchpad, and team config carry the protocol.
- App-server wake prompts may include compact metadata for the highest-priority pending message: sender, priority, summary, total pending count, and urgent count. They must not include the task body.
- If an urgent message is pending, the app-server wake must clearly tell the role to stop at the current safe point and claim the urgent message before continuing other work.

Production Codex wake delivery is:

```text
SQLite inbox message
  -> app-server turn/start
  -> role Codex TUI receives wake turn
  -> role claims durable inbox item
```

`send-keys` is a debug/unsafe path only and must fail closed when tmux reports copy mode.

Initial bootstrap goals are orchestrator inputs only. `--goal` and `--goal-file` create the initial operator-to-orchestrator message; they are not role startup prompts. Keep them to objective, boundaries, and success criteria, then let the orchestrator send scoped role messages.

`tmux-team broadcast` is a convenience wrapper around durable send. It must create separate messages per recipient so every role has independent claim, ack, completion, and reply state. It must not create a shared message that multiple roles compete to claim. Recipient shaping must use either `--only` or `--exclude`, not both.

`tmux-team broadcast --notice` is the exception for announcements. It must record one durable `message_kind='notice'` row per recipient, keep those rows out of pending inbox work, and wake roles with notice-only wording when notification is requested.

## Completion Tracking

Message completion is durable state. Conversational completion replies are explicit but one-command.

- `tmux-team inbox complete` records the result on the original message.
- Expired claimed messages are recoverable work, not silent ownership. They must appear as `stale_claimed` in operator status surfaces and remain reclaimable through `tmux-team inbox next`.
- `tmux-team inbox reclaimable --role ROLE` is an observation aid for expired claims; it must not create a second claim/ack path.
- The orchestrator may inspect cross-role inbox state with `inbox list` and `inbox reclaimable`, but must not claim, ack, or complete another role's inbox work.
- `tmux-team inbox next --auto-ack` may claim and acknowledge a message atomically for roles that accept work before inspecting details.
- `tmux-team status --verbose` must warn about claimed-but-not-acknowledged work older than the configured threshold without changing message state.
- Correlation metadata (`correlation_key`, `related_to`, `supersedes`) is advisory routing context. Duplicate detection must warn without blocking delivery unless a future explicit policy says otherwise.
- `tmux-team inbox list --verbose` is the operator surface for inspecting relation metadata.
- Use `--summary` for the concise result and optional `--body` or `--body-file` for evidence, test output, or handoff detail.
- Roles should use `--reply-to-sender` when completing delegated work from another managed role.
- `--reply-to-sender` queues a concise completion message back to the original sender and wakes it through the normal notification path.
- Completion replies must be stored as `message_kind='completion_notice'`.
- `tmux-team inbox complete-replies --role ROLE` may bulk-complete claimed or acknowledged completion notices only; it must not close unread queued/notified notices.
- A dispatcher must keep the message id returned by `tmux-team send`.
- After fan-out, the dispatcher checks `tmux-team status`, `inbox list`, or events for that message's state before routing follow-up work.
- For one logical work thread, reuse one stable `--correlation-key` across retries, follow-ups, and verification. Different keys are treated as different work, so near-synonym keys create avoidable duplicate work. Use `--allow-duplicate` only when redundant independent work is deliberate.
- Plain `complete` remains available for scripts and operator-originated tasks that should not generate reply traffic.

## Supervision Watches

Long-running monitoring work must not be hidden as an indefinitely acknowledged inbox task.

- Use `tmux-team watch start/update/complete` for ongoing supervision with heartbeat-style updates.
- Watches are durable role-owned state with a current summary, last update, optional next expected update, and terminal status.
- Watches appear in `tmux-team status --verbose` so the operator can distinguish healthy ongoing supervision from stale one-shot inbox work.
- Watches are not message transport. Assignment, handoff, evidence, and completion replies still use inbox messages.
- A role may manage its own watches. The orchestrator and operator may manage or inspect watches across roles.

## Watchdog

Watchdog checks are local supervision, not autonomous orchestration.

- `tmux-team watchdog` reports durable-state findings such as urgent pending work, stale claims, claimed-but-unacked messages, old acknowledged tasks, and overdue watches.
- Watchdog checks must not mutate message/watch state, wake roles, or write milestones by default.
- Bare `tmux-team watchdog` remains a single-shot report command for debugging and scripts.
- Repeated checks should use the native visible runner lifecycle: `watchdog run`, `watchdog start`, `watchdog stop`, `watchdog list`, and `watchdog status`.
- `watchdog start` must create visible tmux infrastructure, not a hidden background process.
- Watchdog runner panes must be self-describing: name, interval, scope, delivery label, last run, next run, last finding, backing pane, and safe-close guidance are visible in pane output.
- Watchdog runner state is durable SQLite state and must appear in `status --verbose` and `dashboard`.
- `tmux-team pane list --all` must mark watchdog panes as watchdog infrastructure, not as ordinary unmanaged shells.
- Watches and watchdog runners are different: watches are role-owned deadlines/expectations; watchdog runners are schedulers/checkers that periodically inspect durable state.

## State

The config and runtime store are the source of truth.

- `.tmux-team/team.toml` records role names, pane targets, app-server endpoint, and Codex thread IDs.
- Operator-facing team, role, and lifecycle configuration is TOML.
- `team.sqlite` records messages, todos, notifications, role state, watches, events, and stable commits.
- Tmux is the view/control surface, not the durable state store.
- `TMUX_TEAM_CONFIG` and `TMUX_TEAM_ROLE` are pane-local process bindings for ergonomics only.
- Bootstrap startup prompts must include explicit `--role <role>` commands because Codex tool shells may not inherit pane-local env.
- Role worktrees also get a `.tmux-team/team.env` pointer back to the current team config, so commands from Codex tool shells can rediscover the team even when process env is not inherited.
- `.tmux-team/team.env` may include `TMUX_TEAM_ROLE` only when exactly one role owns that worktree. Shared worktrees must stay config-only because a single role value would be ambiguous.
- Tmux pane option `@tmux-team-role` is the pane-local role fallback when `TMUX_PANE` is available.
- Explicit CLI flags override env, pointer-file discovery, and pane/cwd inference.
- Spawned or respawned role panes must receive fresh process env, pointer files, pane labels, and pane options from the current config; do not use tmux session-global role env.
- Skill availability is not enough for reset safety. A role that has lost context must recover the role contract from the startup prompt, skill/invariants files, scratchpad memory, and bound config before claiming new work.

If a role pane target changes, config must change with it.

## Sleep

`tmux-team sleep` and `tmux-team resume` are the lifecycle boundary for tearing down and restoring a visible team.

- It snapshots role state, pane targets, tmux session/window/pane IDs, and app-server thread bindings before teardown.
- It writes the snapshot as TOML under `.tmux-team/runtime/sleeps/`.
- It tears down managed role/app-server windows by default and leaves `tt-control` alive.
- It marks active/draining roles paused by default so stale bindings do not keep accepting work.
- Resume reads the latest or specified sleep snapshot, recreates the app-server/role panes, and launches roles with `codex resume <saved-session>` using the saved Codex thread/session ids.
- Resume must update config/runtime pane and app-server bindings after recreating panes.
- Resume reactivates roles by default; use `--no-reactivate-roles` when the operator wants to inspect before accepting work.

## Resizing

Team shape is configurable.

- Use `tmux-team role pause`, `resume`, `drain`, `retire`, or `fail` for runtime state changes.
- Do not silently repurpose a role name for a different responsibility.
- Scaling down should preserve message history and role state.
- A role that is paused or draining must not receive normal new work unless explicitly forced.
