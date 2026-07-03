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
