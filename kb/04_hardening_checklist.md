# Hardening Checklist

## Immediate

- Move message body delivery out of tmux paste.
- Add message IDs and statuses.
- Add per-role `inbox next` and `ack`.
- Make role execution policy explicit: default, named Codex profile, or role-only YOLO.
- Add role heartbeat records.
- Add `notification_pending` when a pane cannot be safely notified.
- Keep every human/operator message in the ledger.

## Short Term

- Replace `stable_commits.md` with `stable_commits.json` or a SQLite table.
- Enforce collector/trainer sync through a helper command.
- Add an active Slurm run registry.
- Add role leases so two processes cannot process the same inbox item.
- Add retry/backoff for notifications.
- Add a simple `tmux-team tui` or `tmux-team status --watch`.

## Medium Term

- Add MCP tools backed by the same service.
- Add app-server remote TUI wake delivery for selected roles.
- Record backend thread IDs per role.
- Stream backend events into the ledger.
- Track approval parked states.
- Add subagent join ledgers for bounded fan-out inside a role.

## Policy Decisions

Define these explicitly:

- Which roles may edit production code?
- Which roles may launch paid collection?
- Which roles may launch Slurm jobs?
- Which roles may promote stable commits?
- What happens when an agent hits an approval prompt?
- Which roles are allowed to run with `--role-yolo`?
- What messages are allowed to interrupt the orchestrator?
- Which message classes require human approval?

## Success Criteria

The design is working when:

- no agent can overwrite a human's half-written prompt;
- no task body depends on tmux paste delivery;
- every message has a durable status;
- failed notification does not imply lost work;
- a restarted agent can recover pending messages;
- collector/trainer cannot accidentally sync unapproved commits;
- the human can inspect the full message/run history without reading panes.
