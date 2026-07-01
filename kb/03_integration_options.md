# Integration Options

## Option A: Keep Tmux, Add a Queue

This is the lowest-risk next step.

Build:

- `tmux-team send`;
- `tmux-team inbox next`;
- `tmux-team inbox ack`;
- `tmux-team inbox complete`;
- `tmux-team notify`;
- `tmux-team status`.

The visible agent sessions remain exactly as they are. Agents are told to read the queue instead of relying on pasted prompts.

Pros:

- preserves human takeover;
- minimal backend internals dependency;
- easy to debug over SSH;
- no app-server protocol work.

Cons:

- agents still need to voluntarily poll unless paired with app-server remote TUI delivery;
- tmux notification remains best-effort;
- no guaranteed turn submission without app-server.

## Option B: Hooks as Inbox Awareness

Hooks can help, but they should not be the primary delivery path.

Useful hook events:

- `SessionStart`: print role identity and pending inbox count;
- `UserPromptSubmit`: log manual human prompts into the service;
- `Stop`: remind an idle agent if pending messages exist;
- `PermissionRequest`: mark a role as parked on approval.

Hooks are not an async background listener. Current command hooks run at lifecycle points and are not a replacement for a message consumer.

## Option C: Codex App-Server Remote TUI for Wakeable Roles

Use this for roles where the agent must stay visible in tmux but wake must not use terminal stdin.

The service can:

- bind role -> app-server endpoint/thread id;
- submit a wake turn directly with `turn/start`;
- stream item and turn events;
- mark message status from actual completion;
- maintain history without reading tmux panes or typing into them.

This is the preferred path for collector-data, collector-diagnostics, trainer, and any implementer pane that must be reliably woken.

Keep the orchestrator human-facing at first.

## Option D: MCP Service

Expose the queue as MCP so agents can call typed tools instead of shelling out.

This is the best long-term agent interface:

- lower parsing ambiguity;
- clear schemas;
- easier ack/claim semantics;
- easy stable-commit and run-registry enforcement.

This can coexist with the CLI because both use the same database.

## Recommended Hybrid

Start with:

```text
human + orchestrator pane
  -> service queue
  -> app-server wake turn where possible, tmux marker otherwise
  -> agent claims message through CLI
```

Then add:

```text
service queue
  -> app-server remote TUI turn/start
  -> service status update
```

for pane-resident roles that require reliable wake.

## Do Not Build First

Avoid starting with:

- full custom terminal UI;
- app-server-only orchestration;
- complex distributed locking service;
- replacing all tmux panes at once;
- long pasted prompts as tmux delivery.

The first reliable improvement is separating durable message content from wake delivery, then using app-server for wakeable Codex panes.
