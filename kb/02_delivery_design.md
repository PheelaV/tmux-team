# Delivery Design

## Problem

Directly typing messages into an agent tmux pane is unsafe when more than one sender exists.

Observed failures:

- another agent can overwrite or interrupt a human prompt;
- text can remain in the composer without Enter being accepted;
- Enter may need to be delayed;
- pane capture can misclassify delivery;
- approval prompts can block the target pane;
- SSH or tmux state can leave delivery half-complete.

The durable act should be writing a message to a service-owned queue. Tmux notification should only mark that pending work exists. Wake-capable Codex delivery should use app-server remote TUI mode.

## Recommended Path

### Phase 1: Service Inbox Plus Safe Marker

Keep the current interactive agent panes.

Change the protocol so senders do this:

```bash
tmux-team send --to orchestrator --summary "B19 health failed" --body-file body.md
```

The service records the message and then shows only a non-typing tmux marker:

```text
[tmux-team] 3 pending messages. Run: tmux-team inbox next --role orchestrator
```

This avoids touching the prompt composer. It does not wake an unattended Codex TUI.

The role agent is instructed to periodically run:

```bash
tmux-team inbox next --role orchestrator
tmux-team inbox ack <message-id>
```

If `send-keys` is explicitly enabled, it is guarded against known tmux modes such as copy mode and should still be treated as an unsafe/debug option for human-visible panes.

### Phase 2: Agent-Facing MCP Tool

Expose the same service as an MCP server:

- `team_inbox_list(role)`
- `team_inbox_claim(role)`
- `team_message_ack(message_id)`
- `team_message_complete(message_id, status, summary, evidence)`
- `team_send(to, body, summary, priority)`
- `stable_commit_get(role)`
- `run_registry_update(...)`

This is cleaner than teaching agents to parse CLI output.

### Phase 3: App-Server Remote TUI Delivery

For Codex-backed roles that need guaranteed wake without prompt collision, run the pane as a remote TUI and deliver through app-server:

- start `codex app-server --listen ws://127.0.0.1:<port>`;
- run the role pane with `codex --remote ws://127.0.0.1:<port>`;
- discover the app-server thread ID created by that remote TUI;
- store each role's app-server endpoint and discovered thread ID;
- submit a wake turn with app-server `turn/start`;
- stream events into the service;
- update message status from the actual Codex result.

Tmux still owns the visible pane. It does not own delivery.

The current default Codex workflow starts here through `tmux-team bootstrap`.

## Delivery Modes

### Notify Only

Use when the human may be typing in the pane.

The service writes the durable message and sends only a non-typing marker.

Status:

```text
queued -> notified
```

The message is not considered delivered until the agent claims it.

### Inject Short Command

Use only when the pane is known idle and not in copy mode.

The injected command is short and idempotent:

```text
tmux-team inbox drain --role collector-data
```

Never inject long task content.

### App-Server Remote TUI Turn

Use for pane-resident Codex sessions that need guaranteed wake.

The service owns the delivery channel to the Codex thread and can reliably mark:

```text
queued -> submitted -> running -> completed
```

This is the only mode where "delivered" can mean "submitted to Codex".

## Role Execution Policy

App-server wake delivery does not automatically grant role agents permission to run local control commands.

If a role is expected to route work to another role, it must be launched with a Codex execution policy that allows `tmux-team` control commands and local app-server access. Otherwise the message can be queued successfully while the notifying role parks on an approval prompt.

Current bootstrap options:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

`--role-profile` is the preferred hook for a narrower Codex policy. `--role-yolo` is the pragmatic all-allowed mode for managed role panes inside a trusted project sandbox.

## Pane Safety Rules

- Never paste full task bodies into a pane if the human might be typing.
- Never treat pane capture as authoritative delivery.
- Prefer a visible marker over content injection for human-visible panes.
- Use a per-pane delivery lease before any tmux send.
- If a pane is in copy mode, has an approval prompt, or may have active composer text, mark notification as deferred.
- Retry notification with backoff, not blind repeated paste.

## Status Model

Suggested message fields:

```json
{
  "id": "msg_20260701_000001",
  "from": "collector-data",
  "to": "orchestrator",
  "priority": "normal",
  "summary": "B19 health failed",
  "body_path": "messages/msg_20260701_000001.md",
  "state": "queued",
  "attempts": 0,
  "created_at": "2026-07-01T16:20:00Z",
  "updated_at": "2026-07-01T16:20:00Z",
  "claimed_by": null,
  "claim_expires_at": null,
  "thread_id": null,
  "pane_id": "%12"
}
```
