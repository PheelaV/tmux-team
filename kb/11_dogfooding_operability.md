# Dogfooding Operability

This note tracks reusable product improvements found while dogfooding tmux-team with pane-resident agents. It intentionally avoids local project names, absolute paths, branches, job ids, or private run details.

## Implemented

### TT-FEAT-001: Stale Claimed-Message Visibility

Expired `claimed` messages are now treated as recoverable active work in operator-facing surfaces.

- `status` counts expired claims as pending and reports them as `stale_claimed`.
- `inbox reclaimable --role ROLE` lists expired claims with message id, sender, recipient, priority, summary, previous claimant, and claim expiry.
- App-server wake context includes expired claims so a role can be woken to reclaim work through the normal `inbox next` path.
- `stale_claimed` is a derived display state. The SQLite row remains `state='claimed'`, so existing stores do not need a schema migration.

Invariant: reclaiming still happens through `tmux-team inbox next`; `inbox reclaimable` is read-only visibility.

### TT-FEAT-002: Verbose Active Status

`tmux-team status --verbose` now shows bounded active message summaries under each role.

- The normal `status` output remains count-focused.
- Verbose output is derived from durable SQLite message state, not pane capture.
- Active rows include queued/notified/retrying, claimed, stale claimed, and acknowledged messages.
- Each row includes message id, display state, priority, sender, age, claim expiry when present, and summary.

Invariant: verbose status is for supervision and triage. It does not imply delivery, acknowledgement, or completion beyond the durable message state it prints.

### TT-FEAT-003: First-Class Long-Running Supervision Tasks

Long-running supervision now has a durable `watch` primitive instead of requiring roles to keep ordinary inbox messages acknowledged indefinitely.

- `watch start` creates an active role-owned watch with summary, optional terminal condition, optional next expected update, and optional reference id.
- `watch update` records heartbeat or blocker state as `active` or `blocked`.
- `watch complete` terminalizes the watch as `done`, `failed`, or `cancelled`.
- `watch list` defaults to active/blocked watches so operational views stay focused.
- `status --verbose` shows active watches under each role alongside active inbox messages.
- The SQLite schema moved to version 2 with an additive `watches` table.

Invariant: watches represent ongoing supervision state. They do not replace inbox assignment, handoff, evidence, or completion messaging.

### TT-FEAT-004: Message Correlation and Duplicate-Work Detection

Messages now carry optional relation metadata and warning-only duplicate detection.

- `send` and `broadcast` accept `--correlation-key`, `--related-to`, and `--supersedes`.
- Active duplicate detection warns when a new message targets the same role with a matching correlation key or normalized summary.
- `--allow-duplicate` suppresses duplicate warnings when overlap is deliberate.
- `inbox list --verbose` shows relation metadata for operator review.
- Completion replies set `related_to` to the completed message id.
- The SQLite schema moved to version 3 with additive message metadata columns.

Invariant: correlation is advisory routing context. It must not block message delivery unless a future explicit policy adds that behavior.

### TT-FEAT-005: Completion-Reply Handling

Completion replies are now distinguishable from ordinary tasks.

- `--reply-to-sender` creates messages with `message_kind='completion_notice'`.
- Completion notices retain normal durable delivery, claim, ack, and wake behavior.
- `inbox complete-replies --role ROLE` bulk-completes claimed or acknowledged completion notices after the recipient has read them.
- Unread queued/notified completion notices are intentionally not auto-closed.
- `inbox list --verbose` shows `kind=completion_notice` and relation metadata.
- The SQLite schema moved to version 4 with an additive `message_kind` column.

Invariant: completion notices are informational closure traffic. They should be easy to close after review, but they should not disappear before a role has claimed or acknowledged them.
