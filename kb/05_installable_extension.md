# Installable Extension Model

## Direction

`tmux-team` should be an installable extension, not just a pile of scripts.

The extension should provide:

- commands for humans and agents;
- a local control service;
- a durable state database;
- optional MCP tools;
- optional hooks;
- a declarative team configuration file;
- runtime commands to resize or reshape the team.

For Codex, this may map to a plugin-style package once the prototype stabilizes.

## Layers

### 1. Package Layer

Owns installation and distribution.

Contents:

```text
tmux-team/
  bin/tmux-team
  service/
  mcp/
  hooks/
  skills/
  templates/
  kb/
```

The package should install a CLI first. MCP and hooks can be enabled later.

### 2. Desired-State Config

Project-local config describes the intended team.
The MVP uses TOML for operator-facing team and lifecycle configuration.

Python stdlib `tomllib` is used for reading TOML. Writing TOML uses `tomli-w`; do not grow a custom TOML serializer inside `tmux-team`.

Example:

```toml
[team]
name = "example-team"
runtime_dir = "/tmp/tmux-team-example-runtime"

[roles.orchestrator]
mode = "human_visible"
worktree = "/workspace/example-project"
pane = "example-team:0"
can_edit = false
can_launch_slurm = false

[roles.implementer]
mode = "human_visible"
worktree = "/workspace/example-project"
pane = "example-team:1"
can_edit = true
can_launch_slurm = false

[roles.collector-data]
mode = "human_visible"
worktree = "/workspace/example-project-collector"
pane = "example-team:2"
can_edit = false
can_launch_slurm = true
requires_stable_commit = true

[roles.trainer]
mode = "human_visible"
state = "paused"
worktree = "/workspace/example-project-trainer"
can_edit = false
can_launch_slurm = "approval_only"
```

This file is desired state, not the full runtime truth.

### 3. Runtime State

Runtime state lives in SQLite or JSONL under the runtime directory.

It records:

- messages;
- role heartbeats;
- pane state;
- Codex thread IDs;
- active Slurm jobs;
- stable commit approvals;
- leases and claims;
- notification attempts;
- paused/draining/retired role state.

Sleep/restart snapshots live under the runtime directory as TOML:

```text
.tmux-team/runtime/sleeps/<sleep-id>.toml
.tmux-team/runtime/sleeps/latest.toml
```

### 4. Reconciler

A small service reconciles desired config against runtime state.

Examples:

- desired role exists but no pane: mark `needs_spawn` or spawn if allowed;
- desired role is `paused`: stop sending it new messages;
- desired role removed: mark `draining`, then retire when inbox is empty;
- role count reduced: reassign or cancel pending work;
- stable commit advanced: notify eligible collector/trainer roles.

The reconciler is what makes ad-hoc resizing safe.

## Mutable Team Shape

Team shape must be changeable at runtime.

Commands:

```bash
tmux-team config show
tmux-team config edit
tmux-team role pause trainer
tmux-team role resume trainer
tmux-team role drain collector-diagnostics
tmux-team role retire collector-diagnostics
tmux-team role add verifier --from-template verifier
tmux-team role scale collector-data --count 2
tmux-team reconcile
```

States:

```text
active      accepts new work
paused      keeps state but receives no new non-urgent work
draining    finishes claimed work, receives no new work
retired     hidden from routing, retained in history
failed      needs operator action
```

## Human-Visible Versus Wakeable Roles

A role should have an execution mode:

```text
human_visible
  Runs in a tmux pane. The service sends visible markers, not task bodies.

app_server_remote_tui
  Runs in a tmux pane attached to Codex app-server with `codex --remote`.
  The service submits wake turns through app-server `turn/start`.

paused
  No new work.
```

For Codex teams, bootstrap should start wake-required roles as `app_server_remote_tui` immediately. Use `human_visible` for non-Codex roles or operator-marker-only panes.

## Configuration Precedence

Use layered config:

```text
package defaults
  < user defaults
  < project .tmux-team/team.toml
  < runtime operator overrides
```

Operator overrides should be explicit and inspectable:

```bash
tmux-team override set role.trainer.mode paused --reason "No training until B19 resolved"
tmux-team override list
tmux-team override clear role.trainer.mode
```

This prevents "temporary" resizing decisions from being lost in chat history.

## Routing Policy

Routing should consult live role state.

If a target role is paused:

- urgent operator messages may still notify;
- normal work should bounce or route to orchestrator;
- batch work should remain queued with `blocked_by_role_paused`.

If a role is draining:

- existing claimed messages continue;
- new messages are rejected unless explicitly forced.

If a role is retired:

- new messages are rejected;
- history remains queryable.

## Minimal Installable MVP

MVP scope:

1. CLI package with `tmux-team`.
2. `team.toml` desired-state config.
3. SQLite message store.
4. Role states: `active`, `paused`, `draining`, `retired`.
5. `send`, `inbox`, `ack`, `complete`, `status`.
6. App-server wake turns for wake-required Codex roles; tmux markers only for non-wakeable human-visible panes.
7. Stable commit table plus `sync-to-stable` helper.

Keep the app-server surface minimal: thread creation, role binding, and wake turns. Do not build a separate agent framework until this small path is reliable.
