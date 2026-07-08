# CLI Reference

This is the operator-facing command map. Run `tmux-team <command> --help` for exact flags.

## Team Setup

Use `bootstrap` for the normal path. It starts the app-server, role panes, config, runtime store, scratchpads, and the initial orchestrator message.

```bash
tmux-team bootstrap --project-root . --goal "Fix the failing test and report the final verifier."
tmux-team bootstrap --project-root . --roles orchestrator,implementer,collector,trainer
tmux-team bootstrap --project-root . --agent-layout grouped
tmux-team bootstrap --project-root . --agent-layout separate-windows
```

Use `init` only when you want a config/runtime scaffold without launching Codex role panes.

```bash
tmux-team init --name example-team --runtime-dir .tmux-team/runtime
```

Use per-role launch options when roles need different worktrees, models, reasoning effort, or Codex profiles.

```bash
tmux-team bootstrap \
  --project-root /repo/main \
  --role-worktree orchestrator=/repo/main \
  --role-worktree implementer=/repo/main \
  --role-worktree collector=/repo/main-collector \
  --allow-shared-worktree orchestrator,implementer \
  --role-model orchestrator=gpt-5.5 \
  --role-reasoning-effort orchestrator=xhigh \
  --role-codex-profile implementer=tmux-team-role
```

Use `--create-missing-worktrees` when tmux-team should create missing git worktrees before launching roles.

```bash
tmux-team bootstrap \
  --project-root /repo/main \
  --role-worktree collector=/repo/main-collector \
  --create-missing-worktrees \
  --worktree-base-ref HEAD
```

## Status And Inspection

Use `status` for durable state first. It does not scrape panes.

```bash
tmux-team status
tmux-team status --verbose
```

`status --verbose` shows bounded active message summaries, open todo counts, obligations, watchdog runners, stale claimed work, and claimed-but-not-acknowledged warnings.

Use `dashboard` for an operator snapshot. `dashboard --once` works in the base install; the live refreshing dashboard requires the optional `tmux-team[dashboard]` extra.

```bash
tmux-team dashboard --once
tmux-team dashboard --once --provenance
tmux-team dashboard --refresh 2
tmux-team dashboard --no-pane-preview
```

Dashboard output is read-only. It labels source classes such as `runtime-db`, `todo`, `milestone-jsonl`, `memory-excerpt`, and best-effort `pane-capture`. Use `--provenance` for row-level source/confidence labels. The live dashboard supports `r` refresh, `h` help, `escape` team overview, `f` filter to the focused role row, `1`-`9` and `0` role shortcuts, and direct jumps such as `a` alerts, `t` roles, `o` obligations, `d` watchdogs, `m` milestones, and `p` panes. Use inbox, obligations, milestones, and memory commands to mutate durable state.

## Messages And Routing

Use `send` for one durable message to one role. The body is stored in the runtime store; wake delivery does not type into the pane.

```bash
tmux-team send --to implementer --summary "Fix failing parser test" --body-file task.md
tmux-team send --to collector --summary "Collect failing evidence" --body "Run the focused test and report output."
```

Use correlation metadata when work belongs to a known thread. Reuse one stable `--correlation-key` for retries, follow-ups, and verification of the same logical work.

```bash
tmux-team send \
  --to collector \
  --summary "Verify parser fix" \
  --body-file verify.md \
  --correlation-key parser-regression
```

Use `broadcast` when the same instruction should become one independent message per recipient.

```bash
tmux-team broadcast --from orchestrator --summary "checkpoint" --body "Report status and blockers." --exclude orchestrator
tmux-team broadcast --from orchestrator --summary "collector check" --body "Report test status." --only collector
```

`--only` and `--exclude` are mutually exclusive. Broadcast is not a shared task; every recipient gets its own message id, claim, ack, completion, and optional reply.

Use `broadcast --notice` for durable announcements that should not create claimable inbox work.

```bash
tmux-team broadcast --notice --summary "Policy updated" --body "Read current operating notes." --exclude orchestrator
```

## Inbox Work Loop

Roles process inbox work one message at a time.

```bash
tmux-team inbox next --role collector
tmux-team inbox ack <message-id> --role collector
tmux-team inbox complete <message-id> --role collector --summary "Focused test reproduced" --body-file result.md --reply-to-sender
```

Use `--auto-ack` when a role wants claim and acknowledgement to be one step before starting work.

```bash
tmux-team inbox next --role collector --auto-ack
```

Use the verbose and reclaimable surfaces to inspect active or expired work.

```bash
tmux-team inbox list --role collector --verbose
tmux-team inbox reclaimable --role collector
```

Use `--reply-to-sender` for delegated role work so the original sender receives a completion notice and wake. After reading acknowledged completion notices, close the bookkeeping with:

```bash
tmux-team inbox complete-replies --role orchestrator
```

## Active Todos

Use `todo` for role-owned substeps of the active inbox message. Todos are durable execution state for the role that owns the message; they are not assignments and they do not wake other roles.

```bash
tmux-team todo add --role collector --message <message-id> "Run focused test"
tmux-team todo list --role collector --message <message-id>
tmux-team todo done --role collector <todo-id>
tmux-team todo reopen --role collector <todo-id>
tmux-team todo supersede --role collector <todo-id> "Run broader regression"
tmux-team todo recover --role collector
```

Open todos block `inbox complete` by default. Complete, reopen, supersede, or clear the checklist before finishing the message; pass `--allow-open-todos` only as an explicit override.

## Memory And Milestones

Use scratchpad memory for durable role state: long-term goals, current task, blockers, boundaries, stable inputs, owned artifacts, and next action.

```bash
tmux-team memory show --role collector
tmux-team memory append --role collector --body "Active task: reproduce failing parser test; next action: run focused pytest."
```

Append only high-value durable updates near the top. Avoid routine startup, parking, no-pending, command transcript, or "still waiting" notes.

Use milestones for the operator timeline.

```bash
tmux-team milestone add --kind result --summary "Targeted test fixed and passed" --subject-role implementer --tag test
tmux-team milestone add --kind routing --summary "Team started" --team
tmux-team milestone list --today
tmux-team milestone list --subject-role implementer
tmux-team milestone list --team
tmux-team milestone list --since -4h
```

By default, the operator/control plane and orchestrator record milestones. Other roles report evidence through inbox completion and let the orchestrator decide what deserves a milestone. New milestones separate the writer (`recorded_by`) from the subject (`--subject-role`, repeated or comma-separated, or `--team`). The older `--role` flag remains as a legacy single-subject alias.

## Obligations And Watchdogs

Use `obligation` for long-running role commitments that should not stay as acknowledged inbox tasks for hours.

```bash
tmux-team obligation start --role collector --summary "Monitor external run" --goal "Report terminal result" --next-update-in 15m
tmux-team obligation update <obligation-id> --role collector --summary "Heartbeat ok" --next-update-in 15m
tmux-team obligation pause <obligation-id> --role collector --reason "Blocked by prerequisite" --review-in 30m
tmux-team obligation resume <obligation-id> --role collector --summary "Prerequisite resolved" --next-update-in 15m
tmux-team obligation complete <obligation-id> --role collector --summary "Run terminalized"
```

Paused obligations keep their previous summary, store a pause reason and optional review time, and do not count as overdue. When the review time passes, `watchdog` reports `obligation_review_due`. Use `obligation complete --status cancelled` when the obligation is truly terminal.

Use `watchdog` for built-in durable-state checks and optional pressure delivery.

```bash
tmux-team watchdog
tmux-team watchdog --json
tmux-team watchdog run --once --delivery app-server-turn --notify-role orchestrator
tmux-team watchdog start --name default --interval 15m --description "Keep team state fresh" --goal "Escalate stale work" --notify-role orchestrator --delivery app-server-turn
tmux-team watchdog update default --interval 10m --goal "Escalate stale collector obligations"
tmux-team watchdog pause default --reason "Operator review" --review-in 30m
tmux-team watchdog resume default
tmux-team watchdog list
tmux-team watchdog status default
tmux-team watchdog stop default
```

`watchdog start` opens a visible tmux window named `tt-watchdog-<name>` that runs `watchdog run`. Runner state is stored in SQLite, appears in `status --verbose` and `dashboard`, and `pane list --all` marks watchdog panes as `infrastructure=watchdog`.

Bare `watchdog` is report-only. `watchdog run --once` and `watchdog start` create durable inbox pressure only when `--delivery` is not `report-only`. Use `--notify-role` to pick the escalation target; otherwise tmux-team uses the scoped `--role`, then `orchestrator`, then the first configured role. Duplicate pressure is suppressed while an active watchdog escalation with the same correlation key is still queued, notified, claimed, or acknowledged.

Paused watchdog runners remain non-terminal, preserve the last finding summary, suppress repeated findings from that runner, and surface review-due reminders through single-shot `tmux-team watchdog`. Use `watchdog stop` when the runner should end.

## Pane Supervision

Use pane commands only for live observation. Pane output is not proof of delivery, acknowledgement, or completion.

```bash
tmux-team pane list --all
tmux-team pane capture collector --lines 120 --offset 40
tmux-team pane capture collector --summary --lines 120 --summary-timeout 60 --summary-max-bytes 20000
```

`pane capture --summary` asks `codex exec` for compact JSON from bounded pane output; it sends the prompt through stdin, caps captured text with `--summary-max-bytes`, and bounds the call with `--summary-timeout`.

## Codex Context And Wake Delivery

For Codex context resets, configure a `SessionStart` hook with matcher `startup|resume|clear|compact` that runs:

```bash
tmux-team codex session-context
```

Wake-capable Codex delivery uses Codex app-server remote TUI mode. Bootstrap configures this automatically, but the manual form is:

```bash
codex app-server --listen ws://127.0.0.1:4500
codex --remote ws://127.0.0.1:4500
tmux-team codex bind implementer --endpoint ws://127.0.0.1:4500 --thread-id <thread-id>
tmux-team send --to implementer --summary "..." --body-file task.md --notify-method app-server-turn
```

`app-server-turn` submits a real Codex turn to the role's thread. The pane stays the live Codex UI, but `tmux-team` never types into the pane.

## Runtime State

Config lives at `.tmux-team/team.toml` by default. Runtime state lives in the configured runtime directory and includes:

- `team.sqlite` for messages, todos, obligations, role state, notifications, events, and stable commits;
- `events.jsonl` for append-only audit;
- `milestones.jsonl` for append-only operator milestones;
- `messages/*.md` for message bodies;
- `sleeps/*.toml` for operator-facing sleep/restart snapshots.

Persistent storage defaults to `.tmux-team/runtime`. Override it with `--runtime-dir`, `TMUX_TEAM_HOME`, or `[team].runtime_dir` in `.tmux-team/team.toml`; that is also the precedence order.

## Sleep And Resume

Use `sleep` to snapshot and stop managed role/app-server windows without killing `tt-control`.

```bash
tmux-team sleep
tmux-team sleep --dry-run
```

Use `resume` to restore from `.tmux-team/runtime/sleeps/latest.toml` or a chosen snapshot.

```bash
tmux-team resume
tmux-team resume --dry-run
tmux-team resume --snapshot .tmux-team/runtime/sleeps/<snapshot>.toml
tmux-team resume --no-reactivate-roles
```

## Extensions

Project-local extensions live under `.tmux-team/extensions/<name>/extension.toml`.

```bash
tmux-team ext list
tmux-team ext doctor
```

See [Extensions](extensions.md).
