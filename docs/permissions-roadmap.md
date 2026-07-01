# Permissions Roadmap

`--role-yolo` is an explicit opt-out/breakglass path, not the target permission model.

Codex implements YOLO with its native `--dangerously-bypass-approvals-and-sandbox` flag. tmux-team only passes that flag to managed role panes when `--role-yolo` is requested. This is reliable for role-to-role autonomy, but it gives each role broad local execution as the current user.

## Lessons From Hermes

Hermes does not make YOLO the whole permission model. It layers:

- approval modes and allowlists;
- catastrophic deny rules;
- dangerous-command heuristics;
- per-terminal backends such as local, Docker, SSH, Singularity, and cloud sandboxes;
- environment and secret filtering;
- separate gates for shell, file edits, and arbitrary code execution.

The important lesson is boundary clarity. Hermes treats approval gates, scanners, and allowlists as mitigations. The real security boundary is OS/process isolation: containers, remote environments, separate users, or whole-process wrapping.

For tmux-team, tmux panes are visibility and takeover surfaces. They are not an isolation boundary.

## Codex Surfaces

Codex gives us several narrower tools than YOLO:

- `--profile <name>` for role-specific config layering;
- approval policies such as `untrusted`, `on-request`, and `never`;
- sandbox modes such as `read-only`, `workspace-write`, and `danger-full-access`;
- named permission profiles under `[permissions]`;
- exec-policy prefix rules for exact command allow/deny decisions;
- MCP server/tool allowlists and approval modes;
- app-server remote TUI transport through `ws://`, and Codex support for `unix://` endpoints.

There is no current single switch for “allow exactly tmux-team plus exactly this app-server localhost port.” The closest immediate option is a Codex role profile plus exact exec-policy prefix rules.

## Near-Term Target

Keep `--role-yolo` available when the operator wants to ignore tmux-team/Codex permission narrowing for a trusted local run. Prefer role profiles for normal autonomous teams:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
```

Example role profile shape:

```toml
approval_policy = "never"
default_permissions = "tmux-team-role"

[permissions.tmux-team-role]
extends = ":workspace"

[permissions.tmux-team-role.network]
enabled = false
```

Pair that with exact exec-policy rules for tmux-team control commands:

```starlark
prefix_rule(pattern=["tmux-team", "send"], decision="allow", justification="tmux-team role messaging")
prefix_rule(pattern=["tmux-team", "notify"], decision="allow", justification="tmux-team role notification")
prefix_rule(pattern=["tmux-team", "codex", "wake"], decision="allow", justification="tmux-team app-server wake")
```

Do not use broad prefixes such as `python`, `uv run`, or `tmux-team` without subcommands.

## tmux-team Policy Layer

Codex profiles are not enough by themselves. tmux-team also needs its own local authorization so authenticated role commands cannot claim another inbox, impersonate another sender, or use unsafe wake methods.

Target policy shape:

```toml
[roles.orchestrator.policy]
execution = "brokered" # default | brokered | profile | yolo_breakglass
can_send_to = ["implementer", "collector", "trainer"]
can_claim = ["orchestrator"]
can_notify = ["implementer", "collector", "trainer"]
can_change_role_state = false
can_bind_app_server = false
can_approve_stable = false
can_use_send_keys = false
```

First-pass implementation:

- `tmux_team.policy.authorize(actor, action, resource, context)`;
- CLI `--actor` for authenticated role context;
- strict role defaults: send as self, claim/ack/complete own inbox, notify self only;
- explicit `can_notify` and `can_use_send_keys` gates;
- `--policy-mode permissive` breakglass for local experiments.

Remaining implementation direction:

- issue per-role runtime credentials at bootstrap;
- store only credential hashes in SQLite;
- derive sender/claim role from authenticated role context for role commands;
- keep `codex bind`, `stable approve`, `sleep`, `--force`, role-state changes, and `send-keys` operator-only.
- make permissive mode visible in `status` and config.

Policy should be enforced by default for authenticated role contexts. Operator commands remain intentionally broad unless an operator policy is added later.

## MCP/App-Server Control Surface

MCP is a near-term control surface, not a long-term nice-to-have.

The shell CLI is useful for humans and tests, but role agents should not need broad shell execution just to move messages through tmux-team. A tmux-team MCP server should expose a small tool surface:

- `team_status`;
- `team_inbox_next`;
- `team_ack`;
- `team_complete`;
- `team_send`;
- `team_notify` / `team_wake`;
- `team_stable_current`.

Codex role panes still receive wake turns through app-server remote TUI. The role then uses MCP tools to claim and complete work instead of shelling out to `tmux-team`. This narrows Codex permissions from “can run local commands” to “can call these tmux-team tools,” while tmux-team enforces role policy centrally.

The first MCP-shaped implementation is a thin wrapper over the same `Store` operations. It does not fork a second state model.

## Transport Hardening

The app-server wake path is the right transport because it avoids tmux prompt injection. It should still be hardened:

- validate app-server endpoints as loopback-only unless explicitly allowed;
- prefer `unix://` app-server sockets when tmux-team supports them;
- keep task bodies in SQLite/message files, not wake prompts;
- treat app-server submission as notification, not completion.

## Correctness Work Before Policy

The policy model depends on reliable state transitions. Implemented first-pass fixes:

- make `inbox next` claim atomically with `UPDATE ... RETURNING` or a write transaction;
- reclaim expired claims;
- enforce valid ack/complete state transitions;

Remaining priority fixes:

- add a lifecycle lock for sleep/bootstrap/bind/notify/claim races;
- use restrictive runtime file permissions when storing future credentials.

## Later Isolation Work

For adversarial or high-risk roles, policy and MCP are still not a hard security boundary. The harder boundary is per-role OS/process isolation:

- containers or remote workers;
- separate Unix users;
- constrained worktree mounts;
- explicit network policy;
- no ambient host secrets.

This can layer under the same tmux-visible/app-server-visible control plane.
