# Operator Supervision Surfaces

This note records the supervision model for pane-resident teams. It is not user documentation; verify current flags in `README.md`, `docs/`, and tests.

## Status First

Use durable state before scraping panes.

`tmux-team status --verbose` is the first inspection surface when counts are unclear. It shows bounded active message summaries per role from SQLite state:

- queued, notified, and retrying work;
- claimed and stale-claimed work;
- acknowledged work;
- priority, sender, age, claim expiry, and summary.

Verbose status is supervision and triage. It does not imply delivery, acknowledgement, or completion beyond the durable message state it prints.

## Dashboard

`tmux-team dashboard` is the aggregated operator view.

Design choice:

- keep `dashboard --once` in the base install as deterministic text output for tests, scripts, and SSH-safe snapshots;
- put the live refreshing TUI behind the optional `tmux-team[dashboard]` extra with Textual;
- keep the dashboard read-only in v1.

The dashboard should combine existing supervision surfaces rather than inventing new state:

- roles and message counts from SQLite;
- active messages and open todos from SQLite;
- obligations from SQLite;
- milestones from `milestones.jsonl`;
- latest scratchpad excerpts from role memory files;
- bounded pane tails from tmux when preview is enabled;
- recent notification failures/deferred notices as alerts.

Dashboard sections must label provenance. SQLite-backed rows are authoritative runtime state. Scratchpad excerpts are prose snapshots, not current-state truth. Pane previews are best-effort tmux captures and any screen-derived status is only a UI hint. Textual rendering must escape arbitrary pane and memory text as plain text.

The live dashboard should stay keyboard-first: refresh/help, role-filter shortcuts, team overview, and section jump keys must be available without mouse input.

It is deliberately not a control surface yet. Follow-up actions such as notify, focus pane, complete an obligation, or inspect full message body can be added later behind explicit commands.

## Obligations

Long-running monitoring must not be hidden as an indefinitely acknowledged inbox task.

`tmux-team obligation` is the durable role-owned state for ongoing commitments:

- `obligation start` creates an active obligation with a summary, optional goal, and optional next expected update.
- `obligation update` records update or blocker state as `active` or `blocked`.
- `obligation pause` records intentional deferral with a reason and optional review time without counting as overdue.
- `obligation resume` restores a paused obligation with a fresh summary and next expected update.
- `obligation complete` terminalizes the obligation as `done`, `failed`, or `cancelled`.
- `obligation list` defaults to active, blocked, and paused obligations.
- `status --verbose` shows visible obligations alongside active inbox messages.

Obligations are not message transport. Assignment, handoff, evidence, and completion replies still use inbox messages.

## Unblock-First Routing

The orchestrator is allowed to do careful review, but it should not block safe preparatory work behind redundant local validation.

When the operator or another role provides information that lets a worker safely begin setup, the orchestrator should send a bounded prep message first, then continue review. The prep message should include the safety gate:

```bash
tmux-team send \
  --to collector \
  --priority high \
  --summary "Prepare next run; launch gated on stable approval" \
  --correlation-key next-run-prep \
  --body "Start preflight/setup now. Do not launch until stable approval or explicit release arrives."
```

This is a routing discipline, not a new queue type. Existing message priority, correlation keys, todos, and stable approval are enough for the first version.

## Pane Hygiene

Operators may create helper shells in managed role windows. Those panes are useful, but lifecycle and supervision commands must not confuse them with roles.

`pane list --all` inspects tmux panes in managed role windows and marks panes not bound to a role as `managed=false`. It is restricted to the operator or orchestrator because it can expose shell commands and current paths.

## Pane Capture

Pane capture is observational only.

Use `pane capture ROLE --lines N --offset N` when durable state is insufficient and the operator or orchestrator needs recent visible pane output for progress, stuck-turn, approval-prompt, or intermediate-test diagnosis.

Use `pane capture ROLE --summary` when raw scrollback would flood context. Summary mode:

- captures bounded tmux scrollback using the same `--lines` and `--offset` controls;
- calls `codex exec`;
- returns a compact JSON-shaped summary;
- must not be treated as delivery, acknowledgement, completion, or durable state.

Do not scrape pane output into scratchpad memory or milestones unless it represents a durable result that belongs there.

## Watchdog

`tmux-team watchdog` is a durable-state report and native runner surface.

Bare `tmux-team watchdog` remains a single-shot report. It reports:

- urgent pending work;
- stale claimed messages;
- claimed-but-not-acknowledged messages;
- old acknowledged tasks;
- overdue obligations;
- review-due paused obligations and watchdog runners.

Bare checks are report-only. Delivery-enabled runners may create one durable inbox escalation and wake the target role. They must not mutate existing message or obligation state, and they do not write milestones by default.

Use native runners for repeated checks:

```bash
tmux-team watchdog run --once --delivery app-server-turn --notify-role orchestrator
tmux-team watchdog start --name default --interval 15m --description "Keep team state fresh" --goal "Escalate stale work" --notify-role orchestrator --delivery app-server-turn
tmux-team watchdog update default --interval 10m --goal "Escalate stale collector obligations"
tmux-team watchdog pause default --reason "operator review" --review-in 30m
tmux-team watchdog resume default
tmux-team watchdog list
tmux-team watchdog stop default
```

Runner invariants:

- runners are visible tmux infrastructure, not hidden background processes;
- runner panes print their purpose, interval, scope, delivery label, notify target, last run, next run, last finding, and safe-close guidance;
- delivery-enabled runners create durable inbox pressure and suppress duplicate active escalation messages by correlation key;
- paused runners do not emit repeated findings and keep the previous finding summary plus pause reason and review time;
- runner state is durable in SQLite and appears in `status --verbose` and `dashboard`;
- `pane list --all` marks runner panes with `infrastructure=watchdog`;
- obligations are role-owned commitments, while watchdog runners are periodic checkers.
