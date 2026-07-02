# Hermes Comparison

## Question

Are we trying to build Hermes Agent?

Short answer: no.

Hermes is a broad personal agent platform:

- terminal UI;
- messaging gateway across Telegram, Discord, Slack, WhatsApp, Signal, Email, CLI;
- memory and skill systems;
- cron and webhook automations;
- delegation/subagents;
- browser/search/media tools;
- multiple execution backends;
- ACP and MCP-style integration surfaces.

`tmux-team` should be much smaller.

## What We Actually Need

The immediate problem is reliable coordination for a small fleet of persistent coding agents, starting with Codex:

- durable message delivery;
- no tmux composer corruption;
- role inbox/outbox history;
- live message status;
- role pause/drain/retire;
- stable commit enforcement;
- run registry for Slurm jobs;
- human takeover via tmux remains possible.

This is closer to a narrow control plane than a general agent platform.

## Hermes Ideas Worth Reusing

### Kanban/Dispatcher Pattern

Hermes has a board/dispatcher model with:

- SQLite-backed task state;
- task events;
- notification subscriptions;
- worker ownership checks;
- heartbeats;
- single-dispatcher locking;
- dispatcher enable/disable config.

This overlaps strongly with `tmux-team`.

Useful concepts:

- one dispatcher owns scheduling;
- workers mutate only their assigned task;
- heartbeats are automatic and explicit;
- task events are durable;
- notifications are derived from state, not the state itself.

### Async Completion Queue

Hermes async delegation avoids splicing results into an active turn. It pushes completion events onto a queue, then re-enters the conversation later as a fresh event.

This maps well to `tmux-team`:

- collector finishes a Slurm run;
- service records completion;
- orchestrator sees a queued completion event;
- no one pastes directly into an active composer.

### Gateway Separation

Hermes separates platform delivery from internal state. The delivery target can fail without losing the task event.

For us:

- tmux visible markers or app-server wake turns can fail;
- the message remains queued;
- status becomes `notify_deferred`, `notify_failed`, or app-server submission failure;
- the operator can inspect/retry.

### Config That Can Change Live

Hermes has dispatcher settings designed to be read fresh so users can stop runaway fan-out without restart.

For us:

- team shape changes must apply live;
- `role pause trainer` should affect the next routing decision;
- scale-down should move roles to `draining`, not require restarting all agents.

## What Not To Copy

Avoid copying Hermes scope:

- multi-platform human messaging gateway;
- self-improving memory system;
- browser/media/search tool gateway;
- broad model/provider abstraction;
- full TUI;
- all terminal backends.

Those are useful in Hermes, but they are not required to solve the tmux-backed agent-team reliability issue.

## Reuse Options

### Option 1: Use Hermes Directly

Run Hermes as the team dispatcher and wrap Codex behind it.

Pros:

- much already exists;
- mature gateway/kanban/delegation ideas;
- broad operations surface.

Cons:

- large dependency and operational surface;
- not Codex-native;
- likely overfits to Hermes agent/session assumptions;
- harder to preserve the existing tmux takeover flow cleanly.

Verdict: only evaluate if we want Hermes to become the operator console.

### Option 2: Borrow Hermes Patterns

Implement a small `tmux-team` service inspired by Hermes kanban/dispatcher semantics.

Pros:

- minimal scope;
- Codex-first but backend-aware;
- easy to reason about;
- preserves current workflow;
- avoids replacing working pieces.

Cons:

- we implement the state machine ourselves;
- must be disciplined about not growing into a full platform.

Verdict: recommended.

### Option 3: Integrate With Hermes Later

Keep `tmux-team` independent but expose MCP/CLI surfaces that Hermes could call.

Pros:

- no lock-in;
- lets Hermes become an optional dashboard or gateway later;
- keeps control-plane semantics narrow and explicit.

Cons:

- duplicate some infrastructure initially.

Verdict: good long-term compatibility target.

## Minimal Functional Target

Build only:

```text
tmux-team send
tmux-team inbox next/ack/complete
tmux-team status
tmux-team role pause/resume/drain/retire
tmux-team stable approve/current/sync
tmux-team notify
```

Active run tracking is intentionally later work, not part of the current minimal command set.

Storage:

- SQLite for state;
- JSONL for append-only audit;
- markdown only for human-readable message bodies/evidence.

Transport:

- tmux visible markers for non-wakeable panes;
- Codex app-server remote TUI wake turns for wake-required panes.

## Decision

Do not build Hermes again.

Use Hermes as prior art for:

- dispatcher ownership;
- event-driven notifications;
- heartbeats;
- worker task ownership;
- live config safety toggles.

Keep `tmux-team` deliberately smaller: a reliable tmux-backed agent-team message/run/stable-commit control plane.
