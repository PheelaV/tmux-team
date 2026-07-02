# Extensibility and Hooks

This is the current agent-facing contract. Keep human usage docs in [../docs/receiving-and-hooks.md](../docs/receiving-and-hooks.md).

## Current Implementation

`tmux-team` has project-local executable hooks.

Implemented pieces:

- `TeamService` brokers message and notification operations for CLI and MCP.
- `Store` remains the durable SQLite/message-file primitive.
- `HookRunner` runs executable hooks with JSON stdin/stdout.
- Project extensions load from `.tmux-team/extensions/<id>/extension.toml`.
- `tmux-team ext list` and `tmux-team ext doctor` inspect manifests.

Not implemented yet:

- custom notification providers;
- custom agent backends;
- extension-provided MCP tools;
- lifecycle hooks for bootstrap/sleep/session events;
- package marketplace loading.

Do not add those until a real use case forces them.

## Invariants

Extensions must not weaken the product invariants:

- Task bodies stay in the durable inbox, not wake prompts or pane text.
- Production Codex wake delivery stays app-server `turn/start`, not `tmux send-keys`.
- The control-plane pane is not a role target.
- The app-server window remains infrastructure.
- `team.sqlite`, message bodies, and `events.jsonl` remain the source of truth.
- Hooks are not transport. They observe, deny, or mutate brokered operations.

## Layout

Project extension:

```text
.tmux-team/
  extensions/
    example-route/
      extension.toml
      hook.py
```

Manifest:

```toml
[extension]
id = "example.route"
name = "Example route"
version = "0.1.0"
api_version = "1"

[[hooks]]
event = "message.before_create"
command = "python3 hook.py"
mode = "mutate"
timeout_ms = 3000
order = 100
```

Modes:

- `observe`: side effect only; failure is fail-open unless it is a `.before` hook.
- `mutate`: may return a JSON merge patch; failure is fail-closed.
- `decision`: may return `decision = "deny"`; failure is fail-closed.

## Events

Current hook events:

- `message.before_create`
- `message.created`
- `message.before_claim`
- `message.claimed`
- `message.acknowledged`
- `message.before_complete`
- `message.completed`
- `notification.before`
- `notification.after`
- `notification.failed`

Selection stays inside `Store.claim_next`. If routing needs to change, mutate message fields before creation.

## Protocol

Every hook receives one JSON object on stdin:

```json
{
  "api_version": "1",
  "event": "message.before_create",
  "invocation_id": "hook_ab12",
  "extension": {"id": "example.route", "version": "0.1.0"},
  "team": {
    "name": "tmux-team",
    "project_root": "/repo",
    "runtime_dir": "/repo/.tmux-team/runtime",
    "config_path": "/repo/.tmux-team/team.toml"
  },
  "actor": "orchestrator",
  "dry_run": false,
  "data": {}
}
```

Empty stdout means success with no changes.

Allow:

```json
{"ok": true}
```

Deny:

```json
{"ok": true, "decision": "deny", "reason": "collector is frozen"}
```

Patch:

```json
{"ok": true, "patch": {"message": {"priority": "high"}}}
```

Patch semantics are JSON merge-patch style for objects. `null` deletes a key.

## Authoring Rules

For a customization request, prefer a project extension before editing `src/tmux_team`.

Good extension uses:

- deny messages during local freezes;
- promote priority based on summary/body metadata;
- record metrics;
- validate completion evidence;
- notify a human outside tmux.

Bad extension uses:

- writing directly to `team.sqlite`;
- pasting into tmux panes;
- marking work complete implicitly;
- sending full message bodies to external services by default;
- granting permissions that `team.toml` denies.

Keep hooks tiny. A hook that needs retries, queues, or long-running state is probably not a hook.

## Test Rule

Every extension behavior added to core needs one cheap test:

- manifest parsing errors;
- hook timeout or nonzero exit;
- fail-open observe behavior;
- fail-closed before/mutate/decision behavior;
- CLI and MCP both passing through `TeamService`.
