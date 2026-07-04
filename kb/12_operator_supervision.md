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
- watches from SQLite;
- milestones from `milestones.jsonl`;
- latest scratchpad excerpts from role memory files;
- bounded pane tails from tmux when preview is enabled;
- recent notification failures/deferred notices as alerts.

It is deliberately not a control surface yet. Follow-up actions such as notify, focus pane, complete watch, or inspect full message body can be added later behind explicit commands.

## Watches

Long-running monitoring must not be hidden as an indefinitely acknowledged inbox task.

`tmux-team watch` is the durable role-owned state for ongoing supervision:

- `watch start` creates an active watch with a summary and optional next expected update.
- `watch update` records heartbeat or blocker state as `active` or `blocked`.
- `watch complete` terminalizes the watch as `done`, `failed`, or `cancelled`.
- `watch list` defaults to active and blocked watches.
- `status --verbose` shows active watches alongside active inbox messages.

Watches are not message transport. Assignment, handoff, evidence, and completion replies still use inbox messages.

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
- overdue watches.

It must not mutate message or watch state, wake roles, or write milestones by default.

Use native runners for repeated checks:

```bash
tmux-team watchdog start --name default --interval 15m
tmux-team watchdog list
tmux-team watchdog stop default
```

Runner invariants:

- runners are visible tmux infrastructure, not hidden background processes;
- runner panes print their purpose, interval, scope, delivery label, last run, next run, last finding, and safe-close guidance;
- runner state is durable in SQLite and appears in `status --verbose` and `dashboard`;
- `pane list --all` marks runner panes with `infrastructure=watchdog`;
- watches are role-owned deadlines, while watchdog runners are periodic checkers.
