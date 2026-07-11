# Receiving and Hooks

## Message Receiving

`tmux-team` uses app-server wake for Codex role panes and an explicit durable claim loop for work. Roles do not run hidden background pollers.

The durable message lives in SQLite. After startup, resume, or wake, a role receives work by running:

```bash
tmux-team memory show --role implementer
tmux-team inbox next --role implementer
tmux-team inbox ack <message-id> --role implementer
tmux-team todo add --role implementer --message <message-id> "Run focused regression"
tmux-team inbox complete <message-id> --role implementer --status fixed --summary "..." --body-file result.md --reply-to-sender
```

`inbox next` atomically moves the next claimable message from:

```text
queued/notified/retrying -> claimed
```

It claims one message. If a wake says there are multiple pending messages, the role should:

```text
memory show -> inbox next -> ack -> optional active todos -> do work -> memory update only for high-value durable changes -> complete todos -> complete --reply-to-sender -> inbox next again
```

and repeat until `inbox next` reports no pending messages. This keeps claim leases and completion evidence attached to one task at a time instead of letting a role hoard the whole backlog.

For read-only inspection, `tmux-team inbox list --state pending` uses the same
definition as `status pending=N`: queued, notified, retrying, and expired claimed
messages. A concrete-state filter such as `--state queued` is intentionally
narrow and must not be used as a drain check. `inbox next` remains the
authoritative claim-and-drain operation.

If `inbox next` reports no new pending messages but the role already has claimed or acknowledged work, it points at that active message and any open todos. Use `tmux-team todo recover --role <role>` after context reset to rebuild the active subplan from durable state.

Ordering is by priority first, then creation time:

```text
urgent -> high -> normal -> low
```

Messages blocked by role state, such as `blocked_by_role_paused`, are recorded but not claimable.

## Wake-Up

Non-app-server roles have two tmux notification modes, but only one should be considered safe by default.

### `display-message`

This is a human-visible tmux status-line marker:

```bash
tmux display-message -t <pane> "[tmux-team] N pending message(s). Run: tmux-team inbox next --role <role>"
```

It does not wake an idle agent because the agent does not see tmux status messages as user input.

This is the default for human-visible panes. It should be treated as a visible marker for the operator, not a delivery guarantee.

### `send-keys`

This is an explicit unsafe/debug wake-up path:

```bash
tmux send-keys -t <pane> "You have N pending tmux-team inbox message(s). Run ..." Enter
```

It still does not inject task content. It submits only a short inbox-check prompt, and the agent must claim durable task content from SQLite.

However, it can still collide with a human or agent composer. It is not safe as a default delivery mechanism for a human-visible Codex TUI pane.

The implementation now fails closed if tmux reports the pane is in a mode such as copy mode:

```text
notify_deferred: pane is in tmux copy/mode; not sending keys
```

The queued message remains claimable, and the notification attempt is recorded. This handles the known copy-mode edge case, but it still cannot prove that a Codex prompt composer is empty.

Use `send-keys` only when an operator or external pane-state guard has established that the pane is idle and the composer is empty:

```toml
[roles.implementer]
pane = "example-team:1"
notify_method = "send-keys"
```

Or force it from the CLI:

```bash
tmux-team notify implementer --method send-keys
tmux-team send --to implementer --summary "..." --body-file task.md --notify-method send-keys
```

## Correct Wake-Up Boundary

For wakeable Codex roles, the service should not wake an interactive TUI through tmux stdin.

Use Codex app-server remote TUI mode instead:

```text
tmux-team queue
  -> app-server turn/start
  -> pane-resident Codex TUI receives and renders the same turn
  -> service records turn submission status
```

The agent remains in the tmux pane. The difference is that the pane is connected to Codex through app-server, and `tmux-team` submits turns through the app-server control protocol instead of terminal keystrokes.

Operator shape:

```bash
codex app-server --listen ws://127.0.0.1:4500
codex --remote ws://127.0.0.1:4500
tmux-team codex bind implementer --endpoint ws://127.0.0.1:4500 --thread-id <thread-id>
tmux-team send --to implementer --summary "..." --body-file task.md --notify-method app-server-turn
```

The normal startup path is `tmux-team bootstrap`, which creates role panes and discovers their app-server thread IDs for you.

### Experimental ACP TUI Boundary

The external ACP runtime launches one visible Toad TUI per role. Toad owns the configured ACP child command and
session state; tmux-team does not speak ACP directly:

```text
tmux-team queue
  -> private role Unix socket
  -> Toad's external prompt queue
  -> ACP session/prompt
  -> provider output and tool activity remain in the visible pane
  -> role claims durable inbox work
```

This avoids tmux stdin and preserves the human composer. Each role uses a unique mode-`0600` socket. Bootstrap waits
for versioned `ping` and `status` responses before sending the startup prompt. Wakes use `prompt` with
`coalesceKey="inbox"`; urgent work is marked urgent but does not cancel an active turn. ACP exact resume uses the
provider's negotiated `session/load` capability; explicit handoff resume creates a fresh session from durable state.

The same socket exposes live ACP session configuration when Toad advertises
`configOptions`/`setConfig`. `runtime options` is observational.
`runtime configure` is role-state-change authorized, requires idle and stable
session identity, and validates only the IDs, select values, and boolean types
advertised by Toad. Every confirmed full response replaces stored
`acp_config`, updates category summaries, and appends same-session lineage. It
does not send inbox data, quiesce for replacement, create a capsule, or start a
new provider session.

Replacing an ACP provider/model command is a controlled session boundary.
`runtime prepare` drains the role before confirming an idle, empty TUI queue and captures bounded durable role state
without task bodies. `runtime switch` accepts only that role's latest unchanged capsule for the same source session,
atomically quiesces external Toad prompts, respawns Toad in the same pane, waits for a new session, records lineage,
and sends one recovery prompt. A switch never
injects a full transcript or treats provider conversation IDs as role identity.

ACP inbox wakes intentionally share `coalesceKey="inbox"`; they only signal that durable work exists. Notice wakes use
`coalesceKey="notice:<message-id>"`, preserving distinct announcements while allowing retries of one notice to coalesce.

`app-server-turn` submits a short wake turn that tells the role durable inbox work exists. The wake turn is deliberately blunt. It does not restate the skill, command syntax, scratchpad rules, ack/complete syntax, or role boundaries. Role panes spawned by bootstrap already received the startup prompt and have the `start-tmux-team` skill available; the wake is only an interrupt that says "claim durable inbox work now."

The wake does include a compact subject line for the highest-priority pending message: sender, priority, summary, total pending count, and urgent count. It never includes the durable task body. If the highest-priority message is urgent, the wake explicitly tells the role to stop at the current safe point, claim the urgent message before continuing other work, then drain by priority.

Role panes spawned by bootstrap are bound to team config and role, but Codex tool shells do not always inherit pane-local env. Bootstrap therefore gives each role startup prompt explicit `--role <role>` commands. Short commands are still supported when discovery works. The fast path is `TMUX_TEAM_CONFIG` and `TMUX_TEAM_ROLE`; fallback discovery uses the role worktree `.tmux-team/team.env` pointer, tmux pane role option, and cwd inference when that is unambiguous. Shared worktrees intentionally do not get a single role value in `.tmux-team/team.env`.

Each spawned role also receives a startup prompt that tells it to load the `start-tmux-team` skill, read scratchpad memory, then claim inbox work or park. Scratchpads are top-loaded operational memory, not transport: use them for long-term goals, role boundaries, current task, blocker, stable inputs, owned artifacts, and next action. Use `tmux-team memory append --body "..."` only for high-value durable updates; it records the newest note near the top of the file. Routine startup, parking, no-pending, and "still waiting" notes should not be appended.

Role todos are the durable active-message checklist. They are useful when work has several execution steps that should survive context compression, pane restart, or sleep/resume, but they are not a second inbox and they do not wake other roles. Mark real finished steps with `todo done`; use `todo supersede` when a step is obsolete and replaced by a new one. Open todos block `inbox complete` unless the caller explicitly passes `--allow-open-todos`.

If the wake says `N pending` with `N > 1`, the role follows its loaded tmux-team role loop and drains one durable inbox message at a time until `inbox next` returns no pending work.

`--reply-to-sender` is the lazy conversational path: completion is recorded on the original message, and a reply message is queued back to the sender when the sender is a managed role. Use `--summary` for the concise result and `--body` or `--body-file` for evidence, test output, or handoff detail that should travel with the completion reply.

Completion notices are local bookkeeping until the goal owner reconciles them. If a non-orchestrator role receives a completion notice that changes team-level state, stable commit readiness, blocker state, external run status, or an operator-visible result, it should forward a concise result to `orchestrator` or complete the still-active orchestrator-owned task with `--reply-to-sender`. Do not rely on a p2p completion notice alone to close team-level work.

Broadcast is just repeated durable send:

```bash
tmux-team broadcast --from orchestrator --summary "checkpoint" --body "Report status and blockers." --exclude orchestrator
tmux-team broadcast --from orchestrator --summary "collector check" --body "Report test status." --only collector
```

It creates one message per recipient. Each recipient has a separate message id, claim, ack, completion, and reply path. The orchestrator should use those per-role states rather than treating a broadcast as one shared task. Use either `--only` for a positive role filter or `--exclude` for a negative role filter; those switches are mutually exclusive.

For live supervision, the orchestrator or operator can inspect recent pane output:

```bash
tmux-team pane capture collector --lines 120 --offset 40
```

Pane capture is observation, not delivery. Use `--lines` or `--limit` for how much history to print, and `--offset` to page back from the newest output. It is useful for intermediate progress, approval prompts, visible test output, or stuck turns that have not yet been summarized in scratchpad memory or inbox completion. It must not be used to decide that a message was delivered or completed.

## Codex Reset Recovery

The initial role spawn prompt remains the normal first-turn instruction. It tells the role to load the `start-tmux-team` skill, read memory, then claim inbox work or park.

For context resets, use Codex's native `SessionStart` hook to inject the same role contract again from durable state. Configure it for `startup|resume|clear|compact` and have it print `tmux-team codex session-context` output:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "tmux-team codex session-context",
            "statusMessage": "Loading tmux-team role context"
          }
        ]
      }
    ]
  }
}
```

This hook is not a replacement for the startup prompt and is not a task. It only restores the role/framework context, current scratchpad excerpt, pending count, config path, and role loop after Codex starts, resumes, clears, or compacts a session. User messages, system/developer instructions, and claimed inbox task bodies still take precedence.

Do not rely on `PostCompact` for role-contract injection. Current Codex hook behavior supports model-context injection through `SessionStart`; the `compact` SessionStart source is the reset-safe path after compaction.

Milestones are the broad operator timeline, separate from the inbox and scratchpads. Record only durable achievements or state changes:

```bash
tmux-team milestone add --kind result --summary "Targeted tests passed" --subject-role implementer --tag test
tmux-team milestone list --today
tmux-team milestone list --since -4h
```

By default, non-orchestrator roles do not write milestones. They complete their inbox message with evidence; the orchestrator records a milestone only if the result is important enough for the operator timeline. Use `--subject-role` for role-scoped events and `--team` for team-wide events so dashboard filtering can distinguish writer from subject.

The task body is not pasted into the pane or into tmux history. It remains in the durable message body file until the agent claims it.

## Role Permissions

Wake delivery through app-server solves prompt-composer corruption, but it does not by itself authorize a role agent to run local control commands.

If a role is expected to send or notify other roles, launch the managed role panes with an appropriate Codex profile or explicit YOLO mode:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

`--role-profile` passes `--profile <name>` to each managed role TUI. Use this for a narrower policy when one is available.

`--role-yolo` passes Codex `--dangerously-bypass-approvals-and-sandbox` to each managed role TUI. This prevents command approval and sandbox prompts from parking role-to-role messaging, but it is all-or-nothing for those role panes. Use it only when the project/worktree itself is the accepted external sandbox.

Current Codex CLI supports non-interactive session resume:

```bash
codex exec resume <session-id> "prompt text"
```

That prompts a saved conversation through a separate Codex process. It is useful for automation, but the pane-resident path is app-server remote TUI plus `turn/start`.

## Test Harness Driving

The deterministic smoke tests also use `tmux send-keys ... Enter` to drive fake shell agents.

That is test automation. The production `send-keys` wake-up path sends only the inbox-check prompt, not task bodies.

## Hooks

There is now a first project-local extension hook implementation for message and notification operations.

Project extensions live under:

```text
.tmux-team/extensions/<extension-id>/extension.toml
```

The current executable-hook surface includes:

- `message.before_create`;
- `message.created`;
- `message.before_claim`;
- `message.claimed`;
- `message.acknowledged`;
- `message.before_complete`;
- `message.completed`;
- `notification.before`;
- `notification.after`;
- `notification.failed`.

Hooks run through the shared `TeamService`, so CLI and MCP message paths use the same extension behavior. Validate extensions with:

```bash
tmux-team ext list
tmux-team ext doctor
```

Still-future Codex lifecycle hooks include:

- `SessionStart`: print role identity and pending inbox count;
- `Stop`: remind an idle role if pending messages exist;
- `UserPromptSubmit`: optionally ledger human-entered operator messages;
- `PermissionRequest`: mark the role as parked on approval.

Hooks should not become the durable transport. They are lifecycle nudges and observability points around the SQLite queue.
