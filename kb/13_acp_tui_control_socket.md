# ACP TUI Control Socket

This note defines the generic local control channel required for a visible ACP
TUI to accept asynchronous prompts without terminal input. It is deliberately
independent of tmux-team and any one agent provider so it can be proposed
upstream to terminal ACP clients.

## Boundary

The TUI remains the single owner of its ACP child process and session state.
External callers may enqueue prompts through a local Unix socket, but they do
not speak ACP directly and never write to the TUI's terminal stdin.

```text
external caller
  -> private Unix socket
  -> TUI event loop
  -> TUI prompt queue
  -> ACP session/prompt
```

The socket acknowledges queue acceptance, not agent completion. Completion
remains an agent/TUI event and, for tmux-team, durable inbox state remains the
source of truth.

## Transport

- AF_UNIX stream socket only.
- The caller supplies the socket path.
- The parent directory is created when missing.
- The socket is mode `0600`.
- Refuse to replace a non-socket filesystem entry.
- Refuse startup when a live listener already owns the path.
- Remove a stale socket before binding and remove the owned socket on exit.
- Newline-delimited UTF-8 JSON.
- One request and one response per connection.
- Maximum request line: 64 KiB.
- No TCP listener, HTTP server, authentication token, or terminal injection.

## Envelope

Every request has:

```json
{
  "version": 1,
  "id": "caller-generated-id",
  "action": "prompt"
}
```

Every response echoes `id` and has either:

```json
{"version": 1, "id": "caller-generated-id", "ok": true}
```

or:

```json
{
  "version": 1,
  "id": "caller-generated-id",
  "ok": false,
  "error": {"code": "invalid_request", "message": "prompt text is required"}
}
```

Unknown fields are ignored for forward compatibility. Unknown actions and
unsupported protocol versions fail explicitly.

## Actions

### `ping`

Confirms that the listener and TUI event loop are alive.

```json
{"version": 1, "id": "1", "action": "ping"}
```

The response includes process and active-session status when known:

```json
{
  "version": 1,
  "id": "1",
  "ok": true,
  "pid": 1234,
  "state": "idle",
  "sessionId": "session-id",
  "queueDepth": 0
}
```

### `status`

Returns the same runtime state as `ping`, plus available non-secret metadata:

- active TUI screen/session identifier;
- ACP session identifier;
- state: `starting`, `idle`, `busy`, `asking`, or `failed`;
- queued external prompt count;
- current agent name and mode when known.

Model, effort, usage, and context values are included only when the ACP agent
advertises them. Status never includes prompt bodies, credentials, or arbitrary
environment variables.

### `prompt`

```json
{
  "version": 1,
  "id": "2",
  "action": "prompt",
  "text": "Pending work is available. Check the durable inbox.",
  "priority": "normal",
  "coalesceKey": "inbox"
}
```

Required:

- `text`: non-empty string.

Optional:

- `priority`: `normal` or `urgent`; defaults to `normal`;
- `coalesceKey`: non-empty string used to replace an older unstarted external
  prompt with the same key;
- `sessionId`: target ACP session when the TUI hosts more than one session.

Response:

```json
{
  "version": 1,
  "id": "2",
  "ok": true,
  "state": "queued",
  "sessionId": "session-id",
  "queueDepth": 1
}
```

`state` is:

- `accepted`: the TUI was idle and scheduled the prompt immediately;
- `queued`: an active turn or blocking interaction owns the session;
- `coalesced`: an older unstarted prompt with the same key was replaced.

### `cancel`

Requests cooperative cancellation of the active ACP turn for the target
session. It does not clear queued prompts. The response says whether a running
turn existed and whether the cancellation request was submitted.

## Prompt Semantics

- External prompts never modify, clear, submit, or focus the human composer.
- External prompts use a queue separate from the human draft.
- At most one ACP `session/prompt` request is active per session.
- An idle session schedules an accepted external prompt through the same
  internal code path that renders and submits a human prompt.
- A busy or `asking` session queues external prompts.
- At a turn boundary, start one queued prompt and wait for that turn to finish.
- FIFO order is preserved within a priority.
- `urgent` runs before queued `normal` work but never automatically cancels the
  active turn.
- Coalescing affects only unstarted external prompts. It never rewrites an
  active turn or human input.
- A queued external prompt should be visible in TUI status, but its arrival
  must not steal transcript scroll position or composer focus.
- Process restart discards the in-memory external queue. Durable callers must
  retry or reconstruct a wake from their own state.

For tmux-team, the socket carries only a compact wake. Durable task bodies stay
in SQLite and are claimed by the role after the agent receives the wake.

## Multiple Sessions

The generic protocol permits `sessionId`, but the first implementation may
target only the active session.

- If one session is active, omitted `sessionId` targets it.
- If multiple sessions are active and the target is omitted, reject with
  `ambiguous_session`.
- If the requested session is not locally active, reject with
  `unknown_session`; do not create or resume it implicitly.

tmux-team launches one TUI process per role, so its normal path is
unambiguous.

## Errors

Stable error codes:

- `unsupported_version`
- `unknown_action`
- `invalid_request`
- `request_too_large`
- `not_ready`
- `ambiguous_session`
- `unknown_session`
- `queue_full`
- `agent_failed`
- `internal_error`

Malformed input must not terminate the listener.

## Non-Goals

- Delivering full durable task bodies.
- Marking external work complete.
- Persisting a second durable prompt queue.
- Attaching directly to the ACP child from another process.
- Exposing the socket over a network.
- Driving the TUI with PTY writes or synthetic keys.
- Providing arbitrary shell execution.

## Acceptance Checks

1. A prompt submitted while idle appears in transcript and starts one turn.
2. A prompt submitted while busy is queued and starts after the active turn.
3. External delivery leaves a partially written human composer unchanged.
4. External delivery does not change a scrolled-up transcript viewport.
5. Repeated `coalesceKey="inbox"` wakes collapse to the newest unstarted wake.
6. Urgent work runs next without cancelling the active turn.
7. Socket mode is `0600`; non-socket collisions fail closed.
8. Stale socket cleanup and normal exit cleanup are deterministic.
9. Invalid, oversized, and unknown requests return structured errors.
10. Status reports session, state, and queue depth without prompt content.
