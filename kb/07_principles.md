# Principles

## Product Shape

`tmux-team` should be intentionally minimal:

- low surface area;
- high reliability;
- easy to understand;
- easy to operate over SSH;
- ergonomic for both humans and agents;
- boring enough to trust during expensive runs.

This is not a general agent platform. It is a small coordination layer for persistent role agents, starting with Codex.

## Reliability Over Capability

Prefer a smaller feature that is observable and recoverable over a richer feature that can silently fail.

Examples:

- A queued message plus failed visible marker is acceptable for non-wakeable roles.
- A failed app-server wake for a wake-required role is a real delivery failure.
- A long pasted prompt that may or may not have submitted is not acceptable.
- A role marked `paused` in durable state is acceptable.
- A role "probably stopped taking work because everyone remembers" is not acceptable.

## Human Takeover Stays Simple

The operator should always be able to answer:

- What roles exist?
- Which roles are active, paused, draining, or failed?
- What messages are pending?
- Which message is each role working on?
- What commit is approved for collector/trainer?
- What Slurm jobs are active?
- What failed and what needs human action?

No answer should require reading four tmux panes and several markdown files.

## State Is Explicit

Avoid implicit social/procedural state.

Use durable records for:

- messages;
- role state;
- role scratchpad memory;
- stable commits;
- active runs;
- delivery attempts;
- heartbeats;
- leases;
- operator overrides.

Markdown is allowed for human-readable bodies and evidence, but the control state should be structured.

Scratchpads are operational memory, not transcripts. They preserve long-term goals across context compression, sleep/resume, and pane restarts, and they let other agents and the human overseer inspect the role's current state. Keep the latest task, blocker, boundary, and next action near the top. Do not append routine startup, parking, no-pending, or "still waiting" notes. Put bulky reports in separate files and link or summarize them from memory.

## Tmux Is A View, Not The Transport

Tmux remains valuable because it gives a human a familiar cockpit.

But tmux should not be the source of truth for:

- message content;
- message delivery;
- completion status;
- run state;
- role availability.

Use tmux only for visibility, manual takeover, and non-guaranteed markers. Use Codex app-server for guaranteed wake of Codex panes.

## Ergonomics

The common commands should feel obvious:

```bash
tmux-team status
tmux-team send --to implementer --body task.md
tmux-team inbox next --role orchestrator
tmux-team role pause trainer
tmux-team stable approve d6d77ab
```

The system should be usable without remembering internal table names, queue mechanics, or tmux details.

## Non-Goals

Do not build:

- a full Hermes replacement;
- a general messaging gateway;
- a full terminal UI before the CLI is reliable;
- a custom LLM framework;
- a broad plugin marketplace;
- a complex distributed system when a single SQLite DB is enough;
- background magic that cannot be inspected or replayed.

## Bias

When choosing between two designs, prefer:

- append-only logs over mutable markdown;
- explicit states over inference from text;
- one dispatcher over many writers;
- manual recovery over hidden retries;
- shell-friendly commands over rich UI;
- config plus runtime overrides over chat-memory decisions;
- small, composable tools over an all-in-one daemon.
