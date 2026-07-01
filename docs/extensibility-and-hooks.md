# Extensibility and Hooks

## Goal

`tmux-team` should be a small control plane that teams adapt without forking or editing core source.

The target shape is similar to Pi's harness model:

- the core stays conservative and focused;
- project-specific behavior lives in local extensions;
- common behavior can be packaged and shared;
- hooks and registries expose important lifecycle points;
- agents get enough examples and contracts to build extensions for users instead of modifying `src/tmux_team`.

The current repo already has the right foundation: `Store` owns durable state, `team.sqlite` and `events.jsonl` are the source of truth, app-server wake prompts are notification only, and the MCP-shaped facade can expose a narrow role-facing control surface. The extensibility layer should build around those boundaries.

## Lessons From Pi

The neighboring Pi repo uses a few patterns worth copying conceptually, not literally:

1. Extensions are first-class resources, not patches. Pi loads TypeScript extension modules from project, user, and package locations.
2. Extensions register capabilities through an API. They do not import random internals to mutate state.
3. Event hooks are named and typed. An extension subscribes with `on("event_name", handler)`.
4. Capabilities are broader than hooks. Pi extensions can add tools, commands, flags, UI, providers, prompt behavior, compaction, and policy-like gates.
5. Discovery is conventional. Project-local resources live under `.pi/`, user resources under `~/.pi/agent/`, and packages can declare resources in a manifest.
6. Examples are part of the product. Pi has many small examples that show extension authors what to copy.
7. Extension code is arbitrary code. Pi documents that package installation is a trust decision.

For `tmux-team`, the same philosophy should become:

- project-local extensions under `.tmux-team/extensions/`;
- optional user-level extensions under `~/.tmux-team/extensions/`;
- optional installed packages later;
- a stable hook/event contract over JSON;
- a Python service layer that brokers all state changes;
- narrow MCP tools for role agents;
- examples and templates designed for agents to generate safely.

## Non-Negotiable Invariants

Extensions must not weaken the control-plane invariants from [docs/invariants.md](docs/invariants.md):

- Task bodies stay in the durable inbox, not wake prompts or tmux pane text.
- Production Codex wake delivery stays app-server `turn/start`, not `tmux send-keys`.
- The control-plane pane is not a role target.
- The app-server window remains infrastructure.
- `team.sqlite`, message body files, and `events.jsonl` remain the source of truth.
- Hooks are not the transport. They are policy, transformation, lifecycle, and observability points around the durable transport.
- Role agents should use a narrow CLI or MCP surface. They should not need broad local shell power just to move messages.

An extension can add Slack notifications, route messages, add policy checks, adjust role instructions, register an agent backend, or publish metrics. It should not bypass claim/ack/complete, write directly to `team.sqlite`, paste into panes, or mark work complete implicitly.

## Recommended Architecture

Implement extensibility in three layers.

### 1. Brokered Service Layer

Add a `TeamService` layer above `Store`.

Today, CLI and MCP paths call `Store` methods directly. That will make hook behavior easy to forget in one surface. The better shape is:

```text
CLI command
  -> TeamService
  -> HookRunner before events
  -> Store durable operation
  -> HookRunner after events

MCP tool
  -> TeamService
  -> same hooks and Store operations
```

`Store` should remain the low-level durable database primitive. `TeamService` should own user-visible operations:

- `send_message`
- `claim_next`
- `ack_message`
- `complete_message`
- `notify_role`
- `set_role_state`
- `bind_role_app_server`
- `approve_stable_commit`
- `sleep_team`
- bootstrap planning and completion hooks

This avoids two classes of bugs:

- CLI and MCP diverge on policy or hooks.
- Extensions reach into `Store` internals because there is no public brokered API.

### 2. Hook Runner

The first implementation should be language-neutral executable hooks, not in-process Python plugins.

Reasons:

- `tmux-team` is a short-lived CLI. A process hook fits that model.
- Agents can generate hooks in Python, shell, Node, or any project-local language.
- Hooks can be run with timeouts and clean JSON input/output.
- The core avoids dynamic Python import and dependency-loading problems at first.

The hook runner should:

- discover hook manifests;
- execute hooks in deterministic order;
- send JSON event payloads on stdin;
- read JSON results from stdout;
- enforce timeouts;
- record every invocation in `events`;
- fail closed for decision/pre hooks;
- fail open for post/observe hooks unless configured otherwise;
- never run hooks while holding a long SQLite transaction.

### 3. Capability Registries

Hooks alone are not enough. Some extension points are better as named registries:

- notification providers;
- agent backends;
- role instruction builders;
- policy checks;
- MCP tools;
- status/diagnostic providers;
- message templates and prompt resources.

These registries can initially be command-backed, using the same manifest and JSON protocol as hooks. Later, Python entry points can be added for performance or richer integrations.

## Extension Layout

Use project-local extensions by default:

```text
.tmux-team/
  extensions/
    slack-notify/
      extension.toml
      notify.py
      README.md
    route-urgent/
      extension.toml
      hook.py
      README.md
```

User-level extensions should be opt-in:

```text
~/.tmux-team/extensions/
  github-issue-router/
    extension.toml
    hook.py
```

The project config should control whether user extensions load:

```toml
[team.extensions]
enabled = true
project = true
user = false
fail_closed = ["message.before_create", "policy.check", "role.before_state_change"]
fail_open = ["message.created", "message.completed", "notification.after"]
```

For repeatable teams, prefer project-local extensions committed with the repo. User-level extensions are useful for personal notifications and local operator workflows, but they should not silently affect shared project behavior.

## Extension Manifest

Use a TOML manifest per extension:

```toml
[extension]
id = "example.route-urgent"
name = "Route urgent messages"
version = "0.1.0"
api_version = "1"
description = "Promotes messages tagged [urgent] and adds routing metadata."

[[hooks]]
event = "message.before_create"
command = "python3 hook.py"
mode = "mutate"
timeout_ms = 3000
order = 100

[[hooks]]
event = "message.created"
command = "python3 hook.py"
mode = "observe"
timeout_ms = 3000
order = 100

[[notify_methods]]
name = "slack"
command = "python3 notify.py"
timeout_ms = 5000
```

Manifest fields:

| Field | Purpose |
| --- | --- |
| `extension.id` | Stable reverse-DNS-like identifier. |
| `extension.version` | Extension version for diagnostics. |
| `extension.api_version` | Hook protocol version the extension expects. |
| `hooks.event` | Named event to receive. |
| `hooks.command` | Command executed from the extension directory. |
| `hooks.mode` | `observe`, `mutate`, `decision`, or `provider`. |
| `hooks.timeout_ms` | Hard timeout for one invocation. |
| `hooks.order` | Lower numbers run first. |
| `notify_methods.name` | New `notify_method` value. |

Do not let extensions register arbitrary hooks by importing Python internals. The manifest is the contract, and `tmux-team ext doctor` should be able to validate it without executing extension code.

## Hook Protocol

Every hook receives one JSON object on stdin:

```json
{
  "api_version": "1",
  "event": "message.before_create",
  "invocation_id": "hook_20260701_120000_ab12cd",
  "extension": {
    "id": "example.route-urgent",
    "version": "0.1.0"
  },
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

Every hook may return one JSON object on stdout. Empty stdout means success with no changes.

For observe hooks:

```json
{
  "ok": true,
  "message": "published metric"
}
```

For mutate hooks:

```json
{
  "ok": true,
  "patch": {
    "message": {
      "priority": "high",
      "metadata": {
        "routed_by": "route-urgent"
      }
    }
  }
}
```

For decision hooks:

```json
{
  "ok": true,
  "decision": "deny",
  "reason": "collector cannot wake trainer outside business hours"
}
```

For provider hooks:

```json
{
  "ok": true,
  "result": {
    "details": "sent to Slack channel #builds"
  }
}
```

Use JSON Merge Patch semantics for `patch`. It is easier for agents to generate and review than Python callback code or JSON Patch paths. If arrays need fine-grained editing later, add event-specific fields rather than making every extension author learn patch operations.

## Failure Semantics

Failure behavior should be predictable:

| Hook kind | Default failure behavior |
| --- | --- |
| `*.before_*` decision hooks | Fail closed. Abort the operation with a clear error. |
| `policy.check` | Fail closed. Do not grant permission on hook failure. |
| `message.before_create` mutate hooks | Fail closed unless the extension is explicitly marked advisory. |
| `notification.before` | Fail closed for the selected provider. The message stays queued or claimable. |
| `*.created`, `*.completed`, `*.after`, metrics hooks | Fail open. Record the hook failure, keep the core operation complete. |
| `sleep.before_teardown` | Fail closed unless `--force` is used. |

Always record:

- extension id;
- event;
- invocation id;
- duration;
- exit code;
- timeout status;
- decision;
- error text, truncated to a safe size.

Use existing `events` for this:

```text
extension.invoked
extension.completed
extension.failed
extension.denied
extension.mutated
```

## Event Catalog

Start with a small stable event set. Add events only where there is a real external customization need.

### Config and Startup

| Event | Kind | Purpose |
| --- | --- | --- |
| `config.loaded` | observe | Report resolved config and enabled extensions. |
| `extension.loaded` | observe | Diagnostics for loaded extension manifests. |
| `extension.failed` | observe | Diagnostics for invalid manifests or failed hooks. |

Do not allow hooks to mutate config after it has loaded. If config needs dynamic behavior, expose that as a provider or explicit command.

### Bootstrap

| Event | Kind | Purpose |
| --- | --- | --- |
| `bootstrap.before_plan` | mutate/decision | Adjust roles, layout, role profiles, or dry-run plan before tmux commands are generated. |
| `bootstrap.role_instructions` | mutate | Append or replace role developer instructions. |
| `bootstrap.before_role_start` | mutate/decision | Adjust one role launch command or veto role creation. |
| `bootstrap.role_bound` | observe | Observe pane and app-server thread binding. |
| `bootstrap.completed` | observe | Publish team startup information. |
| `bootstrap.failed` | observe | Report startup failure. |

Important constraint: `bootstrap.role_instructions` must not inject the durable task body. It may add role identity, domain rules, or inbox handling guidance.

### Messages

| Event | Kind | Purpose |
| --- | --- | --- |
| `message.before_create` | mutate/decision | Validate, route, tag, reprioritize, or block a new message before body file and DB insert. |
| `message.created` | observe | Publish metrics or external notification about a durable message. |
| `message.before_claim` | decision | Allow or deny a role claim attempt before the atomic SQL claim. |
| `message.claimed` | observe | Observe successful claim. |
| `message.acknowledged` | observe | Observe ack. |
| `message.before_complete` | mutate/decision | Normalize result status/summary or require completion evidence. |
| `message.completed` | observe | Publish completion, metrics, or follow-up automation. |

`message.before_claim` should not select which message to claim in v1. Selection stays in `Store.claim_next` so priority ordering and expired-claim reclaim remain atomic. If custom routing needs to affect selection, mutate message priority, recipient, or metadata before creation.

### Notifications and Wake

| Event | Kind | Purpose |
| --- | --- | --- |
| `notification.before` | mutate/decision | Adjust method-specific settings or veto unsafe delivery. |
| `wake_prompt.build` | mutate | Modify the short app-server wake prompt while preserving durable-work instructions. |
| `notification.after` | observe | Record successful delivery. |
| `notification.failed` | observe | Notify humans, metrics, retries. |

`wake_prompt.build` must enforce a schema-level guard: the prompt may include role, pending count, and claim instructions. It must not include message bodies.

### Roles and Policy

| Event | Kind | Purpose |
| --- | --- | --- |
| `role.before_state_change` | decision | Block pause/resume/drain/retire/fail transitions based on local rules. |
| `role.state_changed` | observe | Publish role state changes. |
| `policy.check` | decision | Add deny-only policy checks. |
| `policy.denied` | observe | Publish policy denial. |

In v1, extension policy hooks should only deny. They should not grant permissions. Grants belong in `team.toml` policy config or future signed/trusted policy providers.

### MCP and Agent Tools

| Event | Kind | Purpose |
| --- | --- | --- |
| `mcp.tools.list` | mutate | Add command-backed extension tools to the role-facing tool list. |
| `mcp.tool.before_call` | decision | Deny tool calls based on actor, role, or arguments. |
| `mcp.tool.after_call` | observe | Record tool usage. |
| `mcp.tool.failed` | observe | Record tool failure. |

Extension MCP tools should be disabled for role agents unless policy allows them. Do not expose raw shell or tmux operations by default.

### Sleep and Teardown

| Event | Kind | Purpose |
| --- | --- | --- |
| `sleep.before_snapshot` | observe/mutate | Add extension metadata to the sleep snapshot. |
| `sleep.snapshot_written` | observe | Publish snapshot path. |
| `sleep.before_teardown` | decision | Block teardown if external preconditions fail. |
| `sleep.completed` | observe | Publish sleep completion. |
| `sleep.failed` | observe | Publish sleep failure. |

The sleep snapshot is a good place for extensions to record external handles such as cloud worker ids, dashboards, or notification channels.

### Stable Commits

| Event | Kind | Purpose |
| --- | --- | --- |
| `stable.before_approve` | decision | Require checks before stable approval. |
| `stable.approved` | observe | Publish stable updates. |

This lets teams enforce "stable must pass CI" or "collector cannot approve its own commit" without core source changes.

## Registries

### Notification Providers

Notification providers let teams add methods such as:

- `slack`
- `github-comment`
- `desktop-notify`
- `email`
- `pagerduty`

Manifest:

```toml
[[notify_methods]]
name = "slack"
command = "python3 notify.py"
timeout_ms = 5000
```

Provider input:

```json
{
  "event": "notification.provider",
  "data": {
    "role": "implementer",
    "pending": 2,
    "method": "slack",
    "message_ids": ["msg_..."],
    "wake_prompt": "You have 2 pending tmux-team inbox message(s)..."
  }
}
```

Provider output:

```json
{
  "ok": true,
  "details": "sent to #agent-team"
}
```

Provider rules:

- `app-server-turn` remains the default production Codex wake provider.
- Custom providers may supplement app-server wake.
- A custom provider must not mark messages complete.
- A custom provider must not receive message bodies by default.

### Agent Backends

Codex app-server remote TUI is the first backend. Other agent CLIs should be added as backends, not hard-coded bootstrap forks.

Backend responsibilities:

- render role launch command;
- create or discover a session/thread id if available;
- render role-specific instructions;
- submit a wake signal if the backend has a safe protocol;
- report capability flags.

Manifest sketch:

```toml
[[agent_backends]]
name = "claude-code"
command = "python3 backend.py"
supports_safe_wake = false
```

Backends that cannot safely wake an interactive TUI should return `supports_safe_wake = false`. The core should then require polling or human-visible notification rather than falling back to stdin injection.

### Role Instruction Builders

Role instructions should be extensible without editing `bootstrap.role_developer_instructions`.

Example use cases:

- add domain-specific role duties;
- add project runbook links;
- add coding standards;
- add incident workflow instructions;
- add "how to use this extension" guidance.

Instruction builders should receive:

- role name;
- team name;
- project root;
- enabled capabilities;
- default base instructions.

They may return:

- appended text;
- replacement text, only if explicitly configured;
- diagnostics.

### Policy Checks

Policy extension hooks are useful for local organization rules:

- no role-to-role messages outside an allowlist;
- no urgent messages without a reason;
- no stable approval without CI evidence;
- no sleep while urgent work is pending;
- no notifications to retired roles.

V1 policy hooks should be deny-only. If a hook wants to grant permission, the correct fix is to add a policy config field or a future trusted policy provider model.

### MCP Tools

Extension MCP tools should be command-backed and explicit:

```toml
[[mcp_tools]]
name = "project_ci_status"
description = "Return the current project CI status."
command = "python3 ci_status.py"
timeout_ms = 5000

[mcp_tools.input_schema]
type = "object"
additionalProperties = false
```

Tool output should map to MCP structured content.

Security rule: role agents should only see extension tools allowed by role policy. Operator-only tools can still exist, but they should not appear in role-facing `tools/list`.

## Data Model Additions

The existing tables are enough for a first hook runner, but two additions would make extensions much more useful:

### Message Metadata

Add `metadata_json` to `messages`.

Use cases:

- routing tags;
- external issue ids;
- source event ids;
- required evidence;
- extension-specific state.

Hooks may mutate metadata before message creation. Later events include it read-only unless the event explicitly allows mutation.

### Extension Invocation Audit

The existing `events` table can record hook invocation events. A separate table is optional. If added, it should be derived/audit-oriented:

```sql
CREATE TABLE IF NOT EXISTS extension_invocations (
  id TEXT PRIMARY KEY,
  extension_id TEXT NOT NULL,
  event TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  duration_ms INTEGER,
  exit_code INTEGER,
  error TEXT,
  created_at TEXT NOT NULL
);
```

Do not store secrets or full message bodies in invocation audit rows.

## CLI Surface

Add extension inspection and scaffolding commands:

```bash
tmux-team ext list
tmux-team ext doctor
tmux-team ext init route-urgent
tmux-team ext run-hook route-urgent message.before_create --payload payload.json
```

Command purposes:

| Command | Purpose |
| --- | --- |
| `ext list` | Show discovered extensions, hooks, providers, enabled state, and diagnostics. |
| `ext doctor` | Validate manifests, commands, timeouts, event names, and policy exposure without running hooks. |
| `ext init` | Create a minimal project-local extension from templates. |
| `ext run-hook` | Reproduce one hook invocation for debugging. |

Do not add arbitrary extension subcommands in v1. If an extension needs a command, prefer MCP tools or a project-local script documented in its README.

## Agent Authoring Contract

Agents should be told to solve customization requests by adding an extension before touching core.

Recommended project docs:

```text
.tmux-team/extensions/README.md
docs/tmux-team-extension-authoring.md
```

The authoring guide should tell agents:

1. Search existing extensions first.
2. If the task is notification, routing, validation, policy, instructions, status, or metadata, create an extension.
3. Do not edit `src/tmux_team` unless the requested behavior needs a new core event, registry, schema migration, or invariant change.
4. Keep extension code small and project-local.
5. Include a README with purpose, events used, config, secrets, tests, and rollback.
6. Use `tmux-team ext doctor`.
7. Add tests for hook input/output using saved JSON fixtures.

Agent-facing checklist:

```text
Extension checklist:
- manifest id is stable
- event names are valid
- hook command runs from extension directory
- timeout is set
- hook handles empty or unknown fields
- no task body is sent to external systems unless documented
- no direct writes to team.sqlite
- failure behavior is documented
- README includes how to disable the extension
```

## Examples To Ship

The extension system should ship examples the same way Pi does. These are high-value examples for `tmux-team`:

### `examples/extensions/route-urgent`

Use `message.before_create` to promote `[urgent]` summaries:

```json
{
  "ok": true,
  "patch": {
    "message": {
      "priority": "urgent",
      "metadata": {
        "urgent_reason": "summary tag"
      }
    }
  }
}
```

### `examples/extensions/slack-notify`

Register a `slack` notification provider that posts role, pending count, and message ids. It should not send message bodies unless explicitly configured.

### `examples/extensions/role-runbook-instructions`

Use `bootstrap.role_instructions` to append role-specific runbook links from `.tmux-team/runbooks/<role>.md`.

### `examples/extensions/stable-ci-gate`

Use `stable.before_approve` to require a local CI status file or command to pass before a stable commit is approved.

### `examples/extensions/no-sleep-with-urgent`

Use `sleep.before_teardown` to block `tmux-team sleep` while urgent messages are pending.

### `examples/extensions/completion-evidence`

Use `message.before_complete` to require completion summaries to include a test command or evidence URL for selected roles.

Each example should include:

- `extension.toml`;
- executable hook/provider script;
- fixture input JSON;
- expected output JSON;
- README;
- unit test if the extension lives in this repo.

## Implementation Plan

### Phase 1: Hook Runner and Service Layer

1. Add `src/tmux_team/extensions/manifest.py` for TOML parsing and validation.
2. Add `src/tmux_team/extensions/runner.py` for discovery and JSON subprocess execution.
3. Add `src/tmux_team/service.py` to broker operations currently duplicated between CLI and MCP.
4. Move CLI `send`, `inbox`, `role`, `notify`, `codex bind`, `stable`, and MCP calls onto `TeamService`.
5. Implement initial events:
   - `message.before_create`
   - `message.created`
   - `message.before_complete`
   - `message.completed`
   - `notification.before`
   - `notification.after`
   - `notification.failed`
   - `role.before_state_change`
   - `role.state_changed`
6. Add `tmux-team ext list` and `tmux-team ext doctor`.
7. Add tests for discovery, ordering, mutation, denial, timeout, and CLI/MCP parity.

This phase makes extensions useful without touching bootstrap internals.

### Phase 2: Bootstrap and Sleep Hooks

1. Add bootstrap plan objects that can be patched before tmux commands are generated.
2. Add role instruction builder hooks.
3. Add sleep snapshot metadata hooks.
4. Add teardown decision hooks.
5. Add examples for runbook instructions and sleep guards.

### Phase 3: Registries

1. Add notification provider registry.
2. Add command-backed MCP tool registry with role policy exposure controls.
3. Add agent backend registry.
4. Add status/diagnostic provider registry.

### Phase 4: Packaging

1. Add package manifest support:

   ```toml
   [tmux_team]
   extensions = ["extensions/slack-notify"]
   skills = ["skills/start-tmux-team"]
   docs = ["docs"]
   ```

2. Add install/list/update commands only after local extensions have settled.
3. Support git and local path installs before any central registry.

## Testing Strategy

Unit tests:

- manifest parsing and validation;
- discovery order;
- disabled extensions;
- hook timeout;
- non-zero exit;
- invalid JSON output;
- mutate result merge;
- deny result abort;
- fail-open post hook;
- fail-closed pre hook;
- audit events written.

Service parity tests:

- CLI `send` and MCP `team_send` both emit the same events.
- CLI `complete` and MCP `team_complete` both apply `message.before_complete`.
- Notification hooks run for CLI and MCP wake paths.

Integration tests:

- project-local extension routes a message;
- extension blocks role state change;
- extension notification provider records delivery;
- sleep hook writes metadata to snapshot.

Regression tests:

- hook failure never marks a message completed;
- app-server wake prompt never includes body text;
- `send-keys` is not exposed through MCP extension tools by default;
- a hook cannot select a lower-priority message ahead of an urgent one through `claim_next`.

## Security Model

Extensions are arbitrary local code. Treat installing or enabling one like adding a script to the repo.

Minimum guardrails:

- project config must control user-level extension loading;
- `ext doctor` should show every executable command before hooks run;
- event payloads should omit message bodies unless the event needs them;
- external notification examples should avoid sending bodies by default;
- hook subprocesses should inherit the current process permissions, but no database handles;
- hooks should not run inside long database transactions;
- policy hooks should be deny-only in v1;
- role-facing MCP tools from extensions must be policy-gated;
- timeout defaults should be short.

Longer-term hardening:

- signed/trusted extension packages;
- per-extension allowlists for environment variables;
- secret references instead of raw secrets in manifests;
- OS-level isolation for high-risk extensions;
- separate policy for operator-facing and role-facing extension tools.

## What Should Still Require Core Changes

Extensibility should reduce source edits, not hide necessary product decisions. Core changes are still appropriate for:

- new durable state tables or migrations;
- changes to message claim/ack/complete semantics;
- new safe delivery transports;
- authorization grant semantics;
- app-server protocol support;
- new first-party event types;
- invariant changes documented in `docs/invariants.md`;
- common extension APIs that multiple real extensions need.

Everything else should start as an extension.

## Practical First Cut

The smallest useful implementation is:

```text
TeamService
HookRunner
project-local .tmux-team/extensions discovery
message.before_create
message.created
message.before_complete
message.completed
notification.after
role.before_state_change
role.state_changed
tmux-team ext list
tmux-team ext doctor
examples/extensions/route-urgent
examples/extensions/completion-evidence
```

That first cut would let users and agents customize routing, validation, completion evidence, notifications, and role state policy without editing source. It would also establish the contract for future bootstrap, backend, package, and MCP tool extension points.
