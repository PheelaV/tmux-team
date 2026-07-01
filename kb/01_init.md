# tmux-team KB - Initial Notes

## Purpose

`tmux-team` is a design and implementation space for a lightweight, operator-supervised multi-agent control plane for tmux-backed agent teams.
Codex is the first target backend; the control plane should stay narrow enough to support other agent CLIs later.

The goal is to preserve the useful parts of the current tmux setup:

- long-lived role agents;
- human visibility and takeover;
- separate worktrees for dangerous or expensive work;
- explicit stable-commit promotion;
- durable evidence and run state.

The goal is also to remove the fragile parts:

- agents typing directly into another agent CLI's composer;
- overwritten human prompts;
- lossy tmux delivery checks;
- markdown-only state with no leases, generations, or status transitions;
- social enforcement of stable commits.

## Current Baseline

The existing server workflow has:

- one orchestrator;
- role agents for implementer, collector-diagnostics, collector-data, and trainer;
- persistent agent sessions in tmux panes;
- separate git worktrees for most role boundaries;
- markdown inboxes, outboxes, memory files, and `stable_commits.md`;
- Slurm as the expensive execution substrate;
- `tmux-notify` as a best-effort marker mechanism.

This is already useful because the role boundaries and evidence boundaries are explicit. The main failure mode is that tmux is serving as both the UI and the message transport.

## Design Principle

The message service should become the source of truth.

Tmux should become only:

- a human-visible dashboard;
- a manual takeover interface;
- a best-effort marker channel.

Agents should never rely on raw tmux paste as the durable delivery mechanism. Codex panes that require guaranteed wake should use app-server remote TUI delivery.

## Proposed Control Plane

Use a small service-owned runtime directory or SQLite database:

```text
/tmp/tmux-team-example-runtime/
  team.sqlite
  events.jsonl
  inbox/
  outbox/
  run_registry.json
  stable_commits.json
  panes.json
```

The service owns:

- message IDs;
- message ordering;
- delivery attempts;
- acknowledgement state;
- role leases;
- heartbeats;
- run registry;
- stable-commit policy.

Agents interact with the service through a CLI or MCP tool, not by editing each other's composer directly.

## Message Lifecycle

Minimum lifecycle:

```text
queued -> notified -> claimed -> acknowledged -> completed
queued -> notified -> expired -> retrying
queued -> failed
```

`delivered` should mean "the target agent session accepted a turn or explicitly acknowledged the inbox item", not "tmux pane text looked right".

## Human Takeover

Human takeover remains first-class.

The human can:

- inspect message history;
- pause a role;
- mark a message cancelled;
- send an operator message;
- force a retry;
- resume an agent;
- override stable commit promotion.

Manual messages should also enter the service ledger, even when typed through tmux, so the history remains complete.
