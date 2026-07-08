# Changelog

All notable user-visible changes should be recorded here. Keep migration notes concrete enough that an operator or agent can resume an older tmux-team session safely.

## Unreleased

- Reworked README and docs navigation so quickstart/demo guidance stays concise and the full command map lives in `docs/cli-reference.md`.
- Updated repo-local marketplace metadata to install the upcoming `v0.4.0` plugin tag.
- Replaced the long-running supervision `watch` command surface with `obligation`, including optional `--goal` metadata and obligation labels in status/dashboard/docs.
- Added watchdog pressure delivery: delivery-enabled `watchdog run --once` and watchdog runners create durable inbox escalation messages, wake the target role, and suppress duplicate active escalations by correlation key.
- Added watchdog runner `--description`, `--goal`, `--notify-role`, one-shot `--once`, and `watchdog update` for interval/scope/delivery/target changes.
- Added non-terminal pause/resume lifecycle commands for obligations and watchdog runners, with review-due findings in `tmux-team watchdog`.
- Surfaced paused obligations/runners in `status --verbose`, `obligation list`, `watchdog list/status`, and `dashboard`.
- Added operator recovery metadata through `[operator]`, `tmux-team operator show/bind`, and sleep snapshots.
- Made `tmux-team resume` replay configured role Codex launch settings from sleep snapshots, including model, reasoning effort, profile, raw Codex config overrides, and YOLO mode.
- Made `tmux-team sleep` snapshot and tear down running watchdog runner panes, and made `tmux-team resume` reinstantiate running watchdog runners from the sleep snapshot.
- Made `tmux-team resume` fall back to a recovery snapshot assembled from `team.toml` and SQLite runtime state when no graceful sleep snapshot exists.
- Expanded the live demo with an operator-triggered sleep/resume phase, resumed watchdog interval nudge, and a post-resume test operation.
- Added dashboard provenance/source labels, `dashboard --provenance`, safe Textual escaping for memory and pane preview text, tmux pane metadata in previews, role shortcut filtering, section jump keys, and a help overlay.
- Added milestone subject classification with `recorded_by`, `scope`, `subject_roles`, `milestone add --subject-role/--team`, and matching list filters.

Migration notes:

- Replace `tmux-team watch ...` usage with `tmux-team obligation ...`. Old command names are not kept as compatibility aliases.
- Existing `team.sqlite` stores migrate additively to schema version 9 when opened; old `watches` rows are copied into the new `obligations` table and new obligation ids use the `obligation_` prefix. Obligations and watchdog runners include pause/review metadata, and watchdog runners gain description, goal, and notify-role metadata.
- Bare `tmux-team watchdog` remains report-only. Use `watchdog run --once --delivery app-server-turn --notify-role <role>` or `watchdog start ... --delivery app-server-turn --notify-role <role>` when you want durable inbox pressure.
- Existing sessions may add `[operator]` manually or with `tmux-team operator bind --pane <pane> --codex-thread-id <thread-id>`. Role `codex_model`, `codex_reasoning_effort`, `codex_profile`, `codex_config`, and `codex_yolo` values in `team.toml` are now included in sleep snapshots and replayed by `resume`; TUI-only state such as `/fast` remains unknown unless mapped through explicit Codex config.
- Running watchdog runners are now part of lifecycle recovery. If a host or tmux session ends abruptly before `tmux-team sleep`, `tmux-team resume` can rebuild a recovery snapshot from current `team.toml` role bindings and SQLite watchdog runner rows, then restart the role panes and running watchdog panes.
- Existing `milestones.jsonl` entries remain readable. New entries include `recorded_by`, `scope`, and `subject_roles`; legacy `role` remains as a single-subject compatibility field.

## 0.3.1 - 2026-07-04

- Fixed the live Textual dashboard crash when pane previews are enabled by avoiding a `pane_lines` name collision in the refresh path.

Migration notes:

- Users of the optional live dashboard should upgrade the CLI and plugin to 0.3.1. `tmux-team dashboard --once` and `tmux-team dashboard --no-pane-preview` were not affected.

## 0.3.0 - 2026-07-04

- Removed `tmux-team dashboard --split`; open the dashboard manually in a tmux window/pane or through the control pane instead.
- Simplified dashboard rendering internals while keeping `dashboard --once` and the optional Textual live dashboard behavior.
- Added native watchdog runner lifecycle commands: `watchdog run`, `watchdog start`, `watchdog stop`, `watchdog list`, and `watchdog status`.
- Added durable watchdog runner state in SQLite and surfaced it in `status --verbose`, `dashboard`, and `pane list --all`.
- Hardened watchdog runners so duplicate running names are rejected and stopped runner state is observed by long-running loops.
- Allowed orchestrator strict-policy supervision to inspect cross-role inbox lists/reclaimable work and approve stable commits without breakglass mode.
- Added unblock-first orchestrator guidance so safe downstream prep work is routed promptly with explicit gates instead of waiting for local review/bookkeeping to finish.
- Added role-owned active-message todos with `tmux-team todo add/list/done/reopen/supersede/clear/recover`.
- Added `todo supersede` so obsolete checklist steps can be terminalized while creating a replacement step for the same message.
- Made `tmux-team inbox complete` refuse messages with open todos unless `--allow-open-todos` is passed.
- Made `inbox next`, `status --verbose`, and `codex session-context` surface active claimed/acknowledged work and open todos for reset recovery.
- Added `tmux-team dashboard --once` for deterministic read-only operator snapshots and an optional live Textual dashboard via `tmux-team[dashboard]`.
- Updated role skill guidance so spawned agents treat todos as active execution state, not inbox transport, scratchpad memory, or milestones.

Migration notes:

- Existing `team.sqlite` stores migrate additively to schema version 6 when opened; the new `watchdog_runners` table is created automatically.
- Existing `team.sqlite` stores migrate additively to schema version 5 when opened; the new `todos` table is created automatically.
- Replace ad hoc watchdog shell loops with `tmux-team watchdog start --name <name> --interval <duration>` when you want native visible scheduling.
- Install the optional `tmux-team[dashboard]` extra to use the live dashboard; base installs can still run `tmux-team dashboard --once`.
- Update the installed plugin/skill after pulling this release so role startup and `SessionStart` recovery prompts mention active-message todos.

## 0.2.1 - 2026-07-03

- Fixed `pane list --all` for managed roles stored as tmux `%pane_id` targets.
- Fixed MCP `team_status` pending counts so stale claimed work remains visible as reclaimable pending work.
- Hardened `pane capture --summary` by sending the prompt to `codex exec` through stdin, capping captured text bytes, and enforcing a summary timeout.

## 0.2.0 - 2026-07-03

- Added a versioned tmux-team role contract marker to role startup/resume prompts and `codex session-context` to avoid full skill rereads on ordinary wakes.
- Added single-shot `tmux-team watchdog` for durable-state supervision findings.
- Added `tmux-team pane capture --summary` to summarize bounded pane output through `codex exec` as compact JSON.
- Added `tmux-team pane list --all` to show managed role panes and unmanaged panes in managed windows.
- Added `tmux-team broadcast --notice` for durable announcements that do not create pending inbox tasks.
- Added `tmux-team inbox next --auto-ack` and verbose status warnings for claimed-but-not-acknowledged messages.
- Added `completion_notice` message kind for completion replies and `tmux-team inbox complete-replies` for closing acknowledged completion notices.
- Added optional `send` message correlation metadata (`--correlation-key`, `--related-to`, `--supersedes`) with warning-only active duplicate detection.
- Added `tmux-team watch start/list/update/complete` for long-running supervision tasks with heartbeat-style status.
- Added `tmux-team status --verbose` with bounded active message summaries per role.
- Added `tmux-team inbox reclaimable` and `stale_claimed` status visibility so expired claimed messages are surfaced as recoverable work.
- Added a repeatable public-snapshot live demo scenario with `make live-demo-setup`, `make live-demo-bootstrap`, `make live-demo-verify`, and `make live-demo-clean`.
- Tightened live demo verification to cover stable correlation-key discipline, completion replies, notice broadcasts, watches, milestones, stable approval, and clean final inbox state.

Migration notes:

- Existing `team.sqlite` stores migrate additively to schema version 4 when opened; the new `watches` table, message correlation columns, and message kind column are created automatically.

## 0.1.3 - 2026-07-03

- Added `tmux-team pane capture <role>` for read-only live supervision of managed role pane output, with `--lines`/`--limit` and `--offset` for paging history.
- Added `tmux-team broadcast` to queue one durable message per recipient while preserving individual ack/completion state; recipient shaping now uses mutually exclusive `--only` or `--exclude` filters.
- Added priority/sender/summary context to app-server wake prompts, with explicit preemption wording for urgent messages.
- Added `tmux-team resume` to restore managed role panes from sleep snapshots using `codex resume <saved-session>`.
- Fixed role startup prompts to use explicit `--role <role>` commands so Codex tool shells that do not inherit `TMUX_TEAM_ROLE` still follow the documented loop.
- Wrote `TMUX_TEAM_ROLE` into `.tmux-team/team.env` only for unique role worktrees; shared worktrees remain config-only to avoid ambiguous role discovery.
- Preferred `bash -lc` for managed tmux wrapper commands when bash is available, reducing shell-profile noise on environments that expect bash features.

## 0.1.2 - 2026-07-02

- Added append-only milestones via `tmux-team milestone add/list` and `.tmux-team/runtime/milestones.jsonl`.
- Documented milestone usage as the operator timeline for "what happened today?" and "what changed in the last 4h?" summaries.
- Restricted default milestone writes to the operator/control plane and orchestrator; non-orchestrator roles report evidence through inbox completion.
- Added detailed completion results with `tmux-team inbox complete --body/--body-file`; `--summary` remains the concise result.
- Added `tmux-team codex session-context` so Codex `SessionStart` hooks can restore role/framework context after startup, resume, clear, or compact without competing with the initial role spawn prompt.
- Clarified that `--goal` and `--goal-file` seed only the initial operator message to orchestrator, not role startup prompts.
- Shortened app-server wake prompts so they are blunt interrupts instead of repeating the role workflow.
- Tightened scratchpad memory guidance with a score threshold to avoid startup/parking/status spam.
- Added root agent guidance in `AGENTS.md` and a `CLAUDE.md` pointer to keep contributor instructions consistent.

Migration notes:

- Existing teams can keep running. New milestone logging starts when agents or operators begin using `tmux-team milestone add`.
- Update installed skills after pulling these changes so spawned agents receive the milestone and scratchpad instructions.
- If you add the optional Codex `SessionStart` recovery hook, review/trust it through Codex's normal hook trust flow before relying on it for reset recovery.
