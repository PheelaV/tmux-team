# Omnigraph Memory Extension Plan

Status: plan, not implemented.

Source checked: https://github.com/ModernRelay/omnigraph on 2026-07-02.

## Decision

Do not add Omnigraph to `tmux-team` core.

Use it, if at all, as an optional memory extension for structured, searchable, cross-run agent memory.

Core stays authoritative for:

- SQLite messages;
- role state;
- notification status;
- app-server wake delivery;
- sleep/resume snapshots.

Omnigraph may derive memory from that state. It must not become the queue, wake path, or source of truth.

## Why This Fits Memory But Not Coordination

`tmux-team` solves congestion and delivery:

```text
SQLite inbox -> app-server wake turn -> role claims durable item
```

Omnigraph solves a bigger problem: context graph, agentic memory, retrieval, branching, policy, and fleet-scale coordination. That is useful for long-term memory, but too much surface area for the hot path.

The current Omnigraph integration surface is CLI/server/HTTP/TypeScript/MCP. Python SDK is still future work. Inline Python usage would mean shelling out, calling HTTP, or running another service, which is not a strict win for core.

## Product Boundary

Keep scratchpad files human-readable and recoverable.

Use Omnigraph as a derived index:

```text
scratchpad files
message lifecycle events
completion summaries
stable commits
failure events
  -> optional Omnigraph graph
  -> memory search / recall
```

If Omnigraph is unavailable, agents still work. They just lose structured recall.

## First Extension Shape

Extension name:

```text
tmux-team-omnigraph-memory
```

Config sketch:

```toml
[extension]
id = "tmux-team.omnigraph-memory"
name = "Omnigraph memory"
version = "0.1.0"
api_version = "1"

[omnigraph]
mode = "cli"
store = ".tmux-team/memory/graph.omni"
index_full_bodies = false
fail_closed = false
```

Default behavior:

- fail-open;
- metadata-only;
- local store by default;
- full message bodies and scratchpad bodies opt-in;
- no outbound network unless the user configures an endpoint.

## Initial Hook Mapping

Use existing hooks first:

- `message.created`: create/update `Message`, `Task`, `Role` metadata.
- `message.claimed`: link `Role WORKING_ON Message`.
- `message.acknowledged`: record acknowledgement time.
- `message.completed`: index result status, result summary, role, message id, touched commit if known.
- `notification.failed`: record delivery failure for debugging.

Do not invent `scratchpad.updated` yet. Scratchpad files are not currently a brokered core API. First version can expose a manual index command or a project hook script that indexes known scratchpad paths.

## Minimal Graph Model

Nodes:

- `Role`
- `Message`
- `Task`
- `MemoryNote`
- `Decision`
- `File`
- `Commit`
- `Failure`

Edges:

- `Role SENT Message`
- `Role CLAIMED Message`
- `Role COMPLETED Message`
- `Message PRODUCED MemoryNote`
- `MemoryNote MENTIONS File`
- `Message RESULTED_IN Commit`
- `Commit APPROVED_AS_STABLE`
- `Failure BLOCKED Role`

Keep identifiers stable:

- role id = role name;
- message id = tmux-team message id;
- commit id = git sha;
- memory note id = hash of source path plus heading or byte range.

## Phases

### Phase 0: No Core Change

Create an example project extension under documentation or examples only.

It checks for `omnigraph` on `PATH`, accepts hook JSON on stdin, and writes JSONL import records or calls the Omnigraph CLI.

Tests use a fake `omnigraph` executable and assert:

- hook failure is fail-open by default;
- full bodies are not exported unless enabled;
- message ids and role names are preserved.

### Phase 1: Search Command

Add a role-facing command only if the extension proves useful:

```bash
tmux-team memory search --role implementer "verifier failure"
```

This command should be optional extension plumbing, not required for inbox operations.

### Phase 2: Scratchpad Indexing

Add one minimal core convention for scratchpad paths only if needed:

```toml
[roles.implementer.memory]
scratchpad = ".tmux-team/memory/implementer.md"
```

Then an extension command can index those paths. Avoid file watchers until manual indexing is clearly painful.

### Phase 3: Branchable Memory

Use Omnigraph branches only after basic indexing works:

- per-role branch for draft memory;
- per-task branch for isolated notes;
- orchestrator/operator merges durable facts.

This is the first Omnigraph-specific feature that may justify the dependency.

## Non-Goals

- No Omnigraph dependency in `pyproject.toml`.
- No blocking send/claim/ack/complete/wake on Omnigraph.
- No replacement of `team.sqlite`.
- No automatic export of full task bodies by default.
- No hidden background daemon started by bootstrap.
- No graph schema migration framework in core.

## Promotion Bar

Keep this as an optional extension unless it proves all of:

- scratchpad search is a real bottleneck;
- metadata-only indexing is useful in live workflows;
- failure-open behavior is reliable;
- users can inspect and delete the memory store easily;
- the extension removes more operator work than it adds.
