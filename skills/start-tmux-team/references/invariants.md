# Invariants

Follow these constraints when changing or debugging tmux-team behavior. Ordinary bootstrap, role startup, and inbox work should use `SKILL.md` first and read this file only when a deeper invariant is needed.

## Control Plane

The Codex session that invokes the skill is the operator control session.

- Name the launcher/operator tmux window `tt-control`.
- Do not treat `tt-control` as a managed role.
- Do not route `tmux-team send` work to `tt-control` unless the operator explicitly adds it as a role.
- Record operator recovery metadata in `[operator]` when available. `operator.pane` and `operator.codex_thread_id` help recovery, but they do not make the control pane a managed role.

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

## Role Worktrees

When roles already have separate git worktrees, bootstrap must preserve them with `--role-worktree ROLE=PATH`.

- `--project-root` is the control/config root and default role worktree.
- The role worktree must be passed to Codex with `--cd`; tmux pane cwd alone is not enough for remote TUI sessions.
- Do not silently collapse collector/trainer/reviewer roles into the project root when the user provided separate worktrees.
- Shared worktrees must be explicit with `--allow-shared-worktree ROLE,ROLE`.
- Keep examples generic; do not write private project names, branches, jobs, or absolute operator paths into reusable docs.

## Delivery

Never use tmux stdin as the production wake path for Codex roles.

- Do not paste task bodies into panes.
- Do not use `tmux send-keys` for normal Codex wake.
- Use app-server `turn/start`.
- Wake prompts should be blunt interrupts; do not restate the skill, scratchpad rules, or ack/complete syntax in every wake turn.
- App-server wake prompts may include compact metadata for the highest-priority pending message: sender, priority, summary, total pending count, and urgent count. They must not include the task body.
- If an urgent message is pending, the app-server wake must tell the role to stop at the current safe point and claim the urgent message before continuing other work.
- Durable task content must be claimed from the tmux-team inbox.
- A role handles work as: `inbox next -> ack -> do work -> complete --reply-to-sender -> inbox next` until there is no pending work. Startup prompts should include explicit `--role <role>` commands; short commands are allowed only when role discovery works.
- Use `--reply-to-sender` when completing work delegated by another managed role, so the sender is woken without a second hand-written send command.
- Completion notices are not team-level reconciliation by themselves. If a non-orchestrator role receives a completion notice with material impact on the team goal, stable commit, blocker state, external run state, or operator-visible result, it must send or complete a concise upward report to `orchestrator`.
- Completion can carry detail with `--body` or `--body-file`; keep `--summary` concise.
- Role panes are bound to team config and role; do not put full config paths in normal wake instructions.
- `--goal` and `--goal-file` seed only the initial operator message to orchestrator. They are not role startup prompts; the orchestrator decomposes the objective into role-specific inbox messages.
- For one logical work thread, reuse one stable `--correlation-key` across retries, follow-ups, and verification. Different keys are treated as different work. Check `status --verbose` or `inbox list --verbose` before sending duplicate-looking follow-up work.
- `tmux-team broadcast` creates one durable message per recipient. Each recipient must have independent claim, ack, completion, and reply state.
- Broadcast recipient shaping must use either `--only` or `--exclude`, not both.

## Supervision

Use `tmux-team status --verbose` and `tmux-team dashboard` before scraping pane output. Dashboard views are read-only. Role-filtered dashboard views must scope roles, active work, obligations, milestones, memory, pane previews, role notification alerts, and watchdog runners whose scope or notify target matches the selected role. The live dashboard should keep modal help, independently focusable/scrollable sections, and a compact recent alert panel plus scrollable alert history.

The operator and orchestrator can inspect managed role panes for live progress:

```bash
tmux-team pane capture collector --lines 120 --offset 40
```

- Pane capture is observation only.
- `--lines` or `--limit` controls how much history is printed. `--offset` skips the newest lines so the caller can page back.
- Use it for intermediate progress, stuck turns, visible approval prompts, and recent test output.
- Do not use pane capture as proof that a message was delivered, acknowledged, or completed.
- Do not turn pane scrollback into scratchpad or milestone spam.
- By default, roles can inspect themselves, the orchestrator can inspect all roles, and other cross-role inspection needs explicit policy.

## Skill Availability

Every Codex role spawned by bootstrap must have the `start-tmux-team` skill available in the active `CODEX_HOME`. The skill may not be loaded into the current turn context until triggered, so wake prompts still include a compact role wake signal.

Do not reload the full skill on every ordinary app-server wake. Use the loaded tmux-team role contract version and role loop when present. Use `tmux-team codex session-context` after startup/resume/clear/compact recovery, explicit operator request, or contract/version mismatch.

## Role Memory

Every managed role has a scratchpad memory file declared by config.

- Bootstrap must create the scratchpad if it is missing.
- A newly spawned role must be instructed to read this skill, read memory with an explicit `--role <role>` command, then claim inbox work or park.
- Codex `SessionStart` hooks for `startup|resume|clear|compact` should inject `tmux-team codex session-context` output after resets. This restores the same role contract version as startup; it is not a new task and does not replace the inbox.
- A role must run `tmux-team memory show --role <role>` before claiming pending inbox work unless role discovery is known to work.
- Use memory for durable context: long-lived goals, constraints, decisions, blockers, handoff notes, current worktree/commit/dirty state, running jobs, owned artifacts, stable inputs, and next action.
- Keep the latest and most important state at the top so it remains useful after context compression and for human oversight.
- Update memory only when durable state changed materially. Use a threshold: append when the update would score at least 3 points, where active task/blocker/boundary/long-running job/final-result changes are 3, stable input/artifact/handoff decisions are 2, minor status/test/dirty-state facts are 1, and routine startup/parking/no-pending chatter is 0.
- Do not append just because startup ran, inbox was empty, git status was checked, or the role is still waiting.
- Do not use memory as transport. The SQLite inbox is the source of truth for tasks and delivery status.
- Do not append routine command transcripts or full replacement sections. Append concise notes likely to matter after context reset, sleep/resume, or operator handoff.
- If an inbox message conflicts with scratchpad boundaries, stop and ask the orchestrator instead of guessing.

## Role Todos

Todos are role-owned checklist state for active inbox work.

- Scope every todo to one role and one inbox message.
- Use todos for active execution substeps that should survive context compression, pane restart, or sleep/resume while the message is active.
- Do not use todos as assignments, message transport, milestones, scratchpad memory, chat, or operator timeline.
- A role should add todos after claiming or acknowledging the active message when the work has several steps or a reset would lose the subplan.
- Mark real completed steps with `todo done`.
- If a todo is obsolete because the plan changed, use `todo supersede` with the replacement step instead of marking obsolete work done.
- `inbox complete` should fail while open todos remain unless the caller explicitly chooses `--allow-open-todos`.
- Use `todo recover`, `status --verbose`, and `codex session-context` after reset to recover active todo state.
- The owning role mutates its todos. The orchestrator and operator may inspect todos for supervision.

## Orchestrator Routing

The orchestrator should avoid becoming a serial bottleneck for safe prep work.

- When new operator or role information can unblock another role's setup, route a bounded handoff promptly unless doing so would create irreversible external effects or violate an explicit safety gate.
- Prefer a gated prep message over waiting to finish local review or bookkeeping.
- State the hold condition in the message body, such as "prepare now; do not launch until stable approval arrives."
- Continue local validation after routing the prep message, then send an approve, cancel, or update follow-up.
- Do not block downstream prep on redundant verification already supplied by a worker unless forwarding the handoff would cross a safety boundary.
- Use a stable `--correlation-key` so the prep message and later approval/cancellation remain linked.

## Watchdog Runners

Watchdog checks are local supervision, not autonomous orchestration.

- Bare `tmux-team watchdog` is a single-shot report-only durable-state checker.
- Use `tmux-team watchdog run --once --delivery app-server-turn --notify-role <role>` for one-shot durable pressure.
- Use `tmux-team watchdog start --name <name> --interval <duration>` for repeated checks; runners should be grouped as titled panes in `tt-watchdogs`, not one tmux window per runner.
- Use `tmux-team watchdog update <name>` to change interval, goal, scope, delivery, or notify target.
- Use `tmux-team watchdog pause/resume` for non-terminal deferral with a reason and optional review time; use `watchdog stop` for terminal shutdown.
- Watchdog runners must stay visible in tmux and self-describing in pane output.
- Runner state must be inspected through `watchdog list`, `watchdog status`, `status --verbose`, `dashboard`, or `pane list --all`.
- Do not confuse obligations with watchdog runners: obligations are role-owned commitments; watchdog runners are periodic checkers.
- Delivery-enabled watchdog runners may create durable inbox pressure and wake the target role. Bare checks do not wake roles. Watchdog checks must not mutate existing inbox/obligation state or write milestones by default.

## Milestone Log

`milestones.jsonl` is the append-only operator timeline.

- Record broad achievements and state changes with `tmux-team milestone add`.
- Only the operator/control plane and orchestrator record milestones by default. Other roles report evidence or blockers through inbox completion; the orchestrator decides whether the result is milestone-worthy.
- Good milestones: team start, task routed, evidence accepted, blocker found/resolved, tests passing, stable commit approved, sleep/resume, and team resize.
- Use `--subject-role ROLE` for role-scoped milestones and `--team` for team-wide milestones. `recorded_by` is the writer; subject roles are what the milestone is about.
- Do not store chat logs, command transcripts, reasoning dumps, or task bodies in milestones.
- Query from the control plane with `tmux-team milestone list --today` or `tmux-team milestone list --since -4h`.

## Runtime Env

Role pane env is pane-local state derived from the current config:

- `TMUX_TEAM_CONFIG`: path to the active team config;
- `TMUX_TEAM_ROLE`: role name for this pane.

Because Codex tool shells may not inherit every role-process variable, tmux-team also writes `.tmux-team/team.env` inside each role worktree and records the role in tmux pane option `@tmux-team-role`. The CLI discovery order is explicit flags, env, worktree pointer, tmux pane role, then cwd role inference. `.tmux-team/team.env` may contain `TMUX_TEAM_ROLE` only when the worktree belongs to exactly one role; shared worktrees stay config-only.

When roles are added, removed, slept, resumed, or respawned, the new role process must receive fresh env bindings, pointer files, pane labels, and pane options from the current config. Do not set role env at tmux session scope because grouped role panes need different `TMUX_TEAM_ROLE` values.

## Role Permissions

Autonomous role-to-role messaging requires role panes that can run the `tmux-team` control CLI.

- Prefer a Codex role profile when available.
- Use `--role-yolo` only when the operator accepts allow-all Codex execution for managed role panes.
- Do not treat an approval prompt as successful notification.

## State

`.tmux-team/team.toml` and `team.sqlite` are the source of truth. Operator-facing team, role, and lifecycle configuration is TOML. Tmux is the view/control surface.

## Sleep

Use `tmux-team sleep` to snapshot role state, pane targets, app-server bindings, running watchdog runners, operator mapping metadata, and configured Codex launch settings before tearing down managed role, app-server, and watchdog windows. Sleep must leave `tt-control` alive by default and pauses active/draining roles unless explicitly told not to.

Use `tmux-team resume` to restore a slept team from the latest or specified sleep snapshot. Resume must recreate managed role panes with `codex resume <saved-session>`, replay known model/reasoning/profile/config/YOLO launch settings, update pane/app-server bindings, reinstantiate running watchdog runner panes, and reactivate roles by default unless the operator passes `--no-reactivate-roles`. Treat TUI-only settings such as `/fast` as unknown unless Codex exposes it through explicit config. If no graceful sleep snapshot exists after an abrupt host or tmux shutdown, resume must use durable `team.toml` and SQLite runtime state to build a recovery snapshot when role thread ids, app-server endpoints, worktrees, and watchdog runner rows are available.
