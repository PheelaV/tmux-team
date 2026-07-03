# Durable Messaging State

This note explains the message-state decisions that keep role-to-role coordination reliable. It is agent-facing design memory; current command behavior is in `README.md`, `docs/`, and tests.

## Reclaimable Claims

Expired `claimed` messages are recoverable work, not silent ownership.

- `stale_claimed` is a derived display state. The SQLite row remains `state='claimed'`.
- `status` counts expired claims as pending so the team does not look idle while work is reclaimable.
- `inbox reclaimable --role ROLE` is read-only visibility into expired claims.
- Reclaiming still happens through `tmux-team inbox next`; there is no second claim path.

## Claim And Ack Discipline

The normal role loop is:

```text
inbox next -> inbox ack -> work -> inbox complete
```

`inbox next --auto-ack` exists for roles that want claim and acknowledgement to be atomic before starting work.

Claimed-but-not-acknowledged work is an operator-visible discrepancy:

- `status --verbose` adds `warning=claimed_unacked` after the configured threshold.
- The warning does not mutate state or prove the role is stuck.
- It exists so the operator can distinguish accepted work from a role-loop miss.

## Relation Metadata

Point-to-point `send` can attach relation metadata:

- `--correlation-key`
- `--related-to`
- `--supersedes`

Duplicate detection is warning-only. Matching active work by correlation key or normalized summary should alert the sender, but delivery must continue unless a future explicit policy says otherwise.

The sender is responsible for stable correlation-key discipline:

- use one stable key for one logical work thread;
- reuse that key for retries, follow-ups, and verification of the same work;
- inspect `status --verbose` or `inbox list --verbose` before sending follow-up work;
- do not invent near-synonym keys such as `fix-verify` and `fix-verification` for the same task;
- use `--allow-duplicate` only when redundant independent work is deliberate.

Different correlation keys mean tmux-team should treat the messages as different work. This keeps transport simple, but it means the orchestrator must avoid accidental fan-out churn.

`broadcast` intentionally does not expose relation flags. Broadcast is a simple fan-out convenience that creates one independent message per recipient.

## Completion Notices

`--reply-to-sender` creates a `message_kind='completion_notice'` message related to the completed message id.

Completion notices are durable, claimable, and visible until the recipient has seen them. They should not disappear before being claimed or acknowledged, because the sender may need the closure evidence after a context reset.

`inbox complete-replies --role ROLE` exists to close claimed or acknowledged completion notices in bulk after review.

## Notice Broadcasts

`broadcast --notice` records one completed `message_kind='notice'` row per recipient.

Notices are announcements, not tasks:

- no pending inbox work is created;
- no claim, ack, or completion is required;
- optional wake delivery uses notice-only wording;
- recipient filtering still uses `--only` or `--exclude`.

Use normal `send` or `broadcast` when recipients need to act and report completion.
