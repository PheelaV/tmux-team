# Permissions Roadmap

`--role-yolo` is a bootstrap escape hatch, not the target permission model.

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

Keep `--role-yolo` as breakglass. Prefer role profiles:

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

Codex profiles are not enough by themselves. tmux-team also needs its own local authorization because any role that can run the CLI can currently claim any inbox or impersonate any sender.

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

Implementation direction:

- add `tmux_team.policy.authorize(actor, action, resource, context)`;
- issue per-role runtime credentials at bootstrap;
- store only credential hashes in SQLite;
- derive sender/claim role from authenticated role context for role commands;
- keep `codex bind`, `stable approve`, `sleep`, `--force`, role-state changes, and `send-keys` operator-only.

## Transport Hardening

The app-server wake path is the right transport because it avoids tmux prompt injection. It should still be hardened:

- validate app-server endpoints as loopback-only unless explicitly allowed;
- prefer `unix://` app-server sockets when tmux-team supports them;
- keep task bodies in SQLite/message files, not wake prompts;
- treat app-server submission as notification, not completion.

## Correctness Work Before Policy

The policy model depends on reliable state transitions. Priority fixes:

- make `inbox next` claim atomically with `UPDATE ... RETURNING` or a write transaction;
- reclaim expired claims;
- enforce valid ack/complete state transitions;
- add a lifecycle lock for sleep/bootstrap/bind/notify/claim races;
- use restrictive runtime file permissions when storing future credentials.

## Long-Term Target

Move role control from shell commands to a tmux-team MCP server:

- `team_send`;
- `team_notify`;
- `team_inbox_next`;
- `team_ack`;
- `team_complete`;
- `team_status`;
- `team_stable_current`.

That gives Codex a narrower tool surface than arbitrary shell execution, while tmux-team enforces role policy centrally.
