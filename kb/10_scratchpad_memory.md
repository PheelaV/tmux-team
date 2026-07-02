# Scratchpad Memory Contract

Scratchpad memory is durable role state.

It exists for three reasons:

- preserve long-term goals across context compression, sleep/resume, and pane restarts;
- expose role state to the role itself, other agents, and the human overseer;
- keep operational boundaries visible before a role accepts new work.

It is not the queue. The SQLite inbox remains authoritative for task delivery, claim, ack, and completion status.

## Startup Loop

Every newly spawned role should receive a startup prompt that says:

1. load the `start-tmux-team` skill and invariants;
2. run `tmux-team memory show`;
3. create or update memory if missing/stale;
4. run `tmux-team inbox next`;
5. if no message exists, park;
6. if a message exists, ack, compare against boundaries, work, update memory, and complete.

## Context Reset Recovery

Do not depend on model memory to preserve the operating framework across context compression or reset.

Use Codex `SessionStart` hooks with matcher `startup|resume|clear|compact` to inject `tmux-team codex session-context`. That command emits the same role contract as the initial startup prompt plus durable local state: role, config, runtime, worktree, scratchpad path, pending count, and scratchpad excerpt.

This hook is not a task and does not replace the startup prompt. It only restores context. The SQLite inbox remains authoritative for work, and claimed message bodies still provide task-specific instructions.

## Shape

Keep the latest and most important state near the top:

```text
## Latest
Role:
Worktree:
Commit:
Git status:
Active task:
Current blocker:
Next action:
```

Then keep slower-moving sections:

```text
## Current State
Running jobs:
Owned reports/artifacts:

## Boundaries
Do not launch:
Do not edit:
Do not sync unless:

## Stable Inputs
Current stable commit:
Dataset snapshot:

## Next Action
If woken with no new task:
If current run finishes:
If blocker recurs:
```

## Update Rules

Update memory:

- at startup only when role/worktree/runtime facts are missing and materially affect future work;
- before long work so a context reset can recover the active task and stop rules;
- before completion when durable state changed materially.

Use this score before appending:

- 3 points: active task changed, blocker appeared/resolved, boundary changed, long-running job started/stopped, final result changes next action.
- 2 points: stable input changed, important artifact/report was produced, handoff decision was made.
- 1 point: commit/dirty status changed, test result observed, minor status detail.
- 0 points: repeated startup, no pending inbox, routine command output, transient search result, "still waiting" with no new fact.

Append only when the score is 3 or higher, or when the orchestrator explicitly asks for a memory update. Fold low-score details into the next high-value update instead of writing separate notes.

Do not write:

- full chat logs;
- every command transcript;
- speculative reasoning dumps;
- temporary search results;
- routine startup/parking/no-pending notes;
- "still waiting" notes with no new durable fact;
- full replacement sections through append commands;
- duplicate report bodies.

Reports belong in files. Memory should point to the file and record the conclusion.
