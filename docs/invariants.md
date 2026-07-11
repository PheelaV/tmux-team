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

Experimental ACP TUI roles do not create `tt-app-server`. Each visible Toad pane owns its configured ACP child and
accepts wake requests over a private runtime Unix socket.

## Role Agents

Role agents remain visible in tmux.

- Agents are not hidden background workers.
- Each role has a live Codex TUI pane.
- Each spawned Codex role must have the `start-tmux-team` skill available in its active `CODEX_HOME`.
- Each role receives wake turns through Codex app-server, not through tmux keystrokes.
- The durable task body lives in the `tmux-team` inbox, not in pane text.

For the experimental ACP runtime, the visible pane is the external Toad TUI. It must remain visible and
interruptible, and the ACP child must remain owned by that TUI rather than becoming hidden team infrastructure.

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
- A newly spawned role receives a startup prompt to load the `start-tmux-team` skill, read memory, then claim inbox work or park. It reads invariants only when changing behavior, debugging delivery/layout/lifecycle/recovery, migrating a team, or resolving a state conflict.
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

## ACP Runtime Handoffs

ACP provider sessions are replaceable execution segments. The tmux-team role,
SQLite work state, scratchpad, todos, worktree, and handoff capsule provide
continuity.

- Same-session configuration is limited to live `acp_tui` roles with a control
  socket. Read options from Toad; never maintain provider catalogs or hard-code
  config IDs.
- Configure only an idle, non-quiesced session and keep its session ID stable
  across the initial options, status, and every sequential `setConfig`
  response.
- Treat each returned complete option list as authoritative. Persist all
  confirmed current values in `acp_config`, derive model/effort/mode summaries
  by category, and sync TOML with SQLite.
- Record one `config_changed` lineage entry and
  `role.runtime_config_changed` event per confirmed change. A later failure
  preserves earlier confirmed state and events; same-session configuration
  never creates a capsule, replacement session, or rollback claim.
- Runtime switching is initially limited to visible `acp_tui` roles.
- Refuse switching while the current TUI is busy or asking unless explicit
  cooperative cancellation reaches an idle state.
- Mark the role draining before checking final TUI quiescence so ordinary dispatch cannot start a new turn during replacement.
- Use the TUI control-socket `quiesce` barrier before pane replacement; status polling alone cannot close the prompt race.
- Accept only the latest role-scoped prepared capsule with an unchanged digest and matching source session.
- Create a bounded handoff capsule before switching; never include inbox task
  bodies, credentials, hidden reasoning, or the full transcript.
- Reuse the existing pane and control-socket path for the replacement TUI.
- Update only the selected role's runtime capability fields after the new TUI
  reports ready.
- Send a compact recovery prompt pointing to skill, memory, handoff, Git, todos,
  and inbox state.
- Append old/new provider session provenance to runtime lineage state.
- Reactivate the role only after config, SQLite capabilities, lineage, and the
  recovery prompt are updated.
- Leave the role draining after failure; do not silently launch additional
  replacement sessions.

## Milestone Log

The milestone log is the operator-facing timeline.

- Store broad achievements and state changes in `milestones.jsonl` through `tmux-team milestone add`.
- Use milestones to answer questions like "what happened today?", "what changed in the last 4h?", and "what did this team accomplish while I was away?"
- Record team start, task routing, evidence accepted, blockers found/resolved, tests passing, stable commit approval, sleep/resume, and team resize.
- Keep milestones concise. They are not command transcripts, chat logs, scratchpad replacements, or message transport.
- New milestones should separate writer from subject: `recorded_by` is who wrote the entry, while `--subject-role` or `--team` describes what the milestone is about. Legacy `--role` remains a single-subject compatibility alias.
- Role-filtered milestone views must filter by subject role, not only by writer.
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

## Orchestrator Routing

The orchestrator is on the critical path for team throughput and should not serialize safe preparatory work behind local bookkeeping.

- When new operator or role information can unblock another role's setup, route a bounded handoff promptly unless doing so would create irreversible external effects or violate an explicit safety gate.
- Prefer a gated prep message over waiting to finish local review or repeated validation.
- State hold conditions clearly, such as "prepare now; do not launch until stable approval arrives."
- Continue local validation after routing the prep message, then send an approve, cancel, or update follow-up.
- Do not block downstream prep on redundant verification already supplied by a worker unless forwarding the handoff would cross a safety boundary.
- Use a stable `--correlation-key` to keep prep messages and later approval/cancellation connected.

## Supervision

The operator and orchestrator may inspect managed role panes.

- Use `tmux-team status --verbose` first when aggregate counts are unclear. It must show bounded active message summaries from durable state without scraping panes.
- Use `tmux-team dashboard --once` for a deterministic read-only snapshot of roles, active messages, todos, obligations, milestones, memory excerpts, alerts, and optional pane tails.
- Use `tmux-team dashboard` for the live Textual operator dashboard only when the optional `tmux-team[dashboard]` extra is installed.
- Dashboard sections must label provenance. Runtime database rows are authoritative, memory excerpts are prose, and pane previews are best-effort tmux captures with screen-text heuristic status only.
- Textual dashboard rendering must escape arbitrary memory and pane text as plain text. Captured terminal output must not be treated as trusted Rich markup.
- The live dashboard should remain keyboard-first: refresh/help, role filter shortcuts, team overview, page switching, concise/verbose item mode, and direct section jumps must work without mouse input. Help must be an overlay, and dashboard sections should be independently focusable/scrollable.
- The live dashboard should separate work/supervision from context/history instead of squeezing all sections into one crowded view. Pane preview should be toggleable and off by default in live mode.
- Role-filtered dashboard views must scope roles, active work, obligations, milestones, memory, pane previews, role notification alerts, and watchdog runners whose scope or notify target matches the selected role.
- Alert display should keep a compact recent panel plus a scrollable bounded alert-history section instead of dropping older alerts from the live view.
- Dashboard-local operator preferences belong under `.tmux-team/runtime/`, not in project source, role memory, or docs.
- Dashboard views are observation surfaces. They must not mutate inbox, todo, obligation, milestone, memory, or role state.
- Use `tmux-team pane list --all` to show unmanaged panes in managed role windows. Unmanaged panes must be marked `managed=false`; lifecycle commands must not silently treat them as role panes.
- Use `tmux-team pane capture <role> --lines N --offset N` to read tmux stdout/history for a role.
- Use `tmux-team pane capture <role> --summary` when raw scrollback would flood context; summaries must be generated from bounded capture, use a compact JSON shape, and remain observational only.
- `--lines` or `--limit` controls how much history is printed. `--offset` skips the newest lines so the caller can page back.
- Pane capture is for live progress inspection, stuck-turn diagnosis, and operator overview.
- Pane capture is not a delivery, acknowledgement, or completion mechanism.
- Do not scrape pane output into scratchpad memory or milestones unless it represents a durable result that still belongs there.
- By default, roles can inspect themselves, the orchestrator can inspect all roles, and other cross-role inspection requires explicit policy.

## Delivery

Never use tmux stdin as the production wake path for managed agent roles.

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

Experimental ACP TUI wake delivery is:

```text
SQLite inbox message
  -> private role Unix socket
  -> visible Toad TUI prompt queue
  -> ACP session/prompt
  -> role claims durable inbox item
```

The generic versioned control socket carries only the compact wake prompt. It must not carry the durable task body.
Each role has a unique socket, and bootstrap must complete a `ping`/`status` readiness handshake before sending its
startup prompt.

`send-keys` is a debug/unsafe path only and must fail closed when tmux reports copy mode.

Initial bootstrap goals are orchestrator inputs only. `--goal` and `--goal-file` create the initial operator-to-orchestrator message; they are not role startup prompts. Keep them to objective, boundaries, and success criteria, then let the orchestrator send scoped role messages.

`tmux-team broadcast` is a convenience wrapper around durable send. It must create separate messages per recipient so every role has independent claim, ack, completion, and reply state. It must not create a shared message that multiple roles compete to claim. Recipient shaping must use either `--only` or `--exclude`, not both.

`tmux-team broadcast --notice` is the exception for announcements. It must record one durable `message_kind='notice'` row per recipient, keep those rows out of pending inbox work, and wake roles with notice-only wording when notification is requested. ACP notice coalescing keys are message-specific so distinct announcements cannot replace each other.

## Completion Tracking

Message completion is durable state. Conversational completion replies are explicit but one-command.

- `pending` is a derived message selector, not a stored message state. It means queued, notified, retrying, or expired claimed work, and `inbox list --state pending` must match the `status pending=N` count.
- A concrete-state inbox list is diagnostic only. Roles must use `inbox next` to determine whether claimable work remains; they must not declare the inbox clear from `--state queued`, `--state notified`, or any other partial filter.
- Unknown inbox state filters must fail explicitly instead of producing a false empty result.
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
- Material completion notices must not terminate at an intermediate non-orchestrator role. If a delegated result affects the team goal, stable commit, blocker state, external run state, or operator-visible outcome, the recipient must reconcile upward to `orchestrator` with a concise durable message or by completing the still-active orchestrator-owned task.
- A dispatcher must keep the message id returned by `tmux-team send`.
- After fan-out, the dispatcher checks `tmux-team status`, `inbox list`, or events for that message's state before routing follow-up work.
- For one logical work thread, reuse one stable `--correlation-key` across retries, follow-ups, and verification. Different keys are treated as different work, so near-synonym keys create avoidable duplicate work. Use `--allow-duplicate` only when redundant independent work is deliberate.
- Plain `complete` remains available for scripts and operator-originated tasks that should not generate reply traffic.

## Obligations

Long-running monitoring work must not be hidden as an indefinitely acknowledged inbox task.

- Use `tmux-team obligation start/update/complete` for ongoing role-owned commitments with expected updates.
- Obligations are durable role-owned state with a current summary, optional goal, last update, optional next expected update, and terminal status.
- Use `tmux-team obligation pause/resume` for non-terminal deferral. A paused obligation preserves its previous summary, stores a pause reason, optional review time, paused timestamp, and actor, and must not count as overdue while paused.
- When a paused obligation review time passes, `tmux-team watchdog` reports `obligation_review_due`. Use `obligation complete --status cancelled` for terminal cancellation.
- Obligations appear in `tmux-team status --verbose` so the operator can distinguish healthy ongoing supervision from stale one-shot inbox work.
- Obligations are not message transport. Assignment, handoff, evidence, and completion replies still use inbox messages.
- A role may manage its own obligations. The orchestrator and operator may manage or inspect obligations across roles.

## Watchdog

Watchdog checks are local supervision, not autonomous orchestration.

- `tmux-team watchdog` reports durable-state findings such as urgent pending work, stale claims, claimed-but-unacked messages, old acknowledged tasks, overdue obligations, and review-due paused obligations/runners.
- Bare watchdog checks are report-only. Delivery-enabled runners may create one durable inbox escalation and wake the target role; they must not mutate existing message/obligation state or write milestones.
- Bare `tmux-team watchdog` remains a single-shot report command for debugging and scripts.
- Repeated checks should use the native visible runner lifecycle: `watchdog run`, `watchdog start`, `watchdog update`, `watchdog pause`, `watchdog resume`, `watchdog stop`, `watchdog list`, and `watchdog status`.
- `watchdog run --once` may be used as a one-shot pressure/checker surface for scripts.
- `watchdog start` must create visible tmux infrastructure, not a hidden background process. Multiple runners should share a `tt-watchdogs` window with one titled pane per runner.
- Watchdog runners must carry inspectable description, goal, scope, delivery method, and notify target when configured.
- Watchdog delivery must suppress duplicate escalation while a prior active watchdog message with the same correlation key is queued, notified, claimed, or acknowledged.
- Paused watchdog runners must not emit repeated findings or wake roles while paused. They preserve the last finding summary plus pause reason, review time, paused timestamp, and actor. Use `watchdog stop` for terminal shutdown.
- Watchdog runner panes must be self-describing: name, interval, scope, delivery label, last run, next run, last finding, backing pane, and safe-close guidance are visible in pane output.
- Watchdog runner state is durable SQLite state and must appear in `status --verbose` and `dashboard`.
- `tmux-team pane list --all` must mark watchdog panes as watchdog infrastructure, not as ordinary unmanaged shells.
- Obligations and watchdog runners are different: obligations are role-owned commitments; watchdog runners are schedulers/checkers that periodically inspect durable state.

## State

The config and runtime store are the source of truth.

- `.tmux-team/team.toml` records role names, pane targets, runtime-specific app-server or control-socket bindings,
  and provider metadata.
- Operator-facing team, role, and lifecycle configuration is TOML.
- `team.sqlite` records messages, todos, notifications, role state, obligations, events, and stable commits.
- Tmux is the view/control surface, not the durable state store.
- `TMUX_TEAM_CONFIG` and `TMUX_TEAM_ROLE` are pane-local process bindings for ergonomics only.
- Bootstrap startup prompts must include explicit `--role <role>` commands because Codex tool shells may not inherit pane-local env.
- Role worktrees also get a `.tmux-team/team.env` pointer back to the current team config, so commands from Codex tool shells can rediscover the team even when process env is not inherited.
- `.tmux-team/team.env` may include `TMUX_TEAM_ROLE` only when exactly one role owns that worktree. Shared worktrees must stay config-only because a single role value would be ambiguous.
- Tmux pane option `@tmux-team-role` is the pane-local role fallback when `TMUX_PANE` is available.
- The optional `[operator]` table records control-pane recovery metadata such as `pane` and `codex_thread_id`. It is not a managed role and must not receive inbox work.
- Explicit CLI flags override env, pointer-file discovery, and pane/cwd inference.
- Spawned or respawned role panes must receive fresh process env, pointer files, pane labels, and pane options from the current config; do not use tmux session-global role env.
- Skill availability is not enough for reset safety. A role that has lost context must recover the role contract from the startup prompt, skill/invariants files, scratchpad memory, and bound config before claiming new work.

If a role pane target changes, config must change with it.

## Sleep

`tmux-team sleep` and `tmux-team resume` are the lifecycle boundary for tearing down and restoring a visible team.

- It snapshots role state, pane targets, tmux session/window/pane IDs, runtime-specific session bindings, running watchdog runners, operator mapping metadata, and configured launch settings before teardown.
- It writes the snapshot as TOML under `.tmux-team/runtime/sleeps/`.
- It tears down managed role, app-server, and watchdog windows by default and leaves `tt-control` alive.
- It marks active/draining roles paused by default so stale bindings do not keep accepting work.
- Resume reads the latest or specified sleep snapshot, recreates the app-server, role, and running watchdog panes, and launches roles with `codex resume <saved-session>` using the saved Codex thread/session ids and known launch settings.
- ACP `exact` resume requires negotiated `session/load` support, starts Toad with the saved provider session ID, and
  verifies the returned ID before reactivating or waking pending work.
- ACP `handoff` resume is an explicit operator choice that starts a fresh provider session and injects the saved
  memory/capsule recovery prompt. Never silently fall back from `exact` to `handoff`.
- ACP sleep drains and atomically quiesces every role before snapshot/teardown; any pre-teardown failure unquiesces
  roles and restores their prior state.
- Resume restores configured model, reasoning effort, profile, raw Codex config overrides, and YOLO mode when present. TUI-only state that Codex does not expose, such as `/fast`, is reported as unknown and must be verified manually if important.
- Bootstrap and resume set tmux truecolor options on the managed session by default. They should use `tmux-256color`, prefer RGB terminal features when supported, set `COLORTERM=truecolor`, and keep `--no-truecolor` as the opt-out for unusual terminal stacks.
- Resume must update config/runtime pane and app-server bindings after recreating panes.
- Resume reactivates roles by default; use `--no-reactivate-roles` when the operator wants to inspect before accepting work.
- If no graceful sleep snapshot exists after an abrupt host or tmux shutdown, resume must build a recovery snapshot from durable `team.toml` and SQLite runtime state when role thread ids, app-server endpoints, worktrees, and running watchdog rows are available.

## Resizing

Team shape is configurable.

- Use `tmux-team role pause`, `resume`, `drain`, `retire`, or `fail` for runtime state changes.
- Do not silently repurpose a role name for a different responsibility.
- Scaling down should preserve message history and role state.
- A role that is paused or draining must not receive normal new work unless explicitly forced.
