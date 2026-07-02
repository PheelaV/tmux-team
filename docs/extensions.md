# Extensions

`tmux-team` supports project-local executable hooks for small policy, routing, validation, and observability changes.

Use extensions when the behavior is project-specific. Change core code only when the invariant or CLI contract itself needs to change.

## Location

```text
.tmux-team/extensions/<extension-id>/extension.toml
```

Example:

```toml
[extension]
id = "example.freeze"
name = "Freeze collector"
version = "0.1.0"
api_version = "1"

[[hooks]]
event = "message.before_create"
command = "python3 hook.py"
mode = "decision"
timeout_ms = 3000
order = 100
```

Check extensions with:

```bash
tmux-team ext list
tmux-team ext doctor
```

## Current Events

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

Hooks receive JSON on stdin and may return JSON on stdout. Empty stdout means success with no changes.

## Limits

Hooks must not become transport:

- do not write directly to `team.sqlite`;
- do not paste into tmux panes;
- do not mark work complete implicitly;
- do not send full message bodies to external services by default.

Detailed agent authoring notes live in [../kb/08_extensibility_and_hooks.md](../kb/08_extensibility_and_hooks.md).
