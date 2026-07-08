# Hardening Checklist

Use this as agent-facing project memory. Human docs live in [../docs/index.md](../docs/index.md).

## Implemented Baseline

- Durable message IDs, states, body files, and SQLite storage.
- `inbox next`, `ack`, and `complete` state transitions.
- Atomic claim with expired-claim reclaim.
- Notification attempt records.
- App-server remote TUI wake for Codex roles.
- `send-keys` kept as unsafe/debug and deferred in tmux copy mode.
- Role state: `active`, `paused`, `draining`, `retired`, `failed`.
- Stable commits stored in SQLite with `approve/current/sync`.
- Message relation metadata, completion notices, notice broadcasts, and duplicate warnings.
- Role-owned active-message todos with recovery surfaces.
- Scratchpad memory and append-only milestones.
- Obligations for long-running role-owned commitments.
- `status --verbose`, dashboard snapshots/live TUI, pane listing, and pane capture/summary.
- Single-shot watchdog checks, pressure delivery, and visible watchdog runners.
- `tmux-team sleep` TOML snapshots and managed-window teardown.
- First-pass role policy with permissive breakglass.
- Project-local executable hooks through `TeamService`.
- Fake-agent, congestion, Docker, and opt-in real-Codex tests.

## Remaining Priority Work

1. Add a lifecycle lock for bootstrap/sleep/bind/notify/claim races.
2. Make permissive policy mode visible in `status`.
3. Add per-role runtime credentials before treating MCP as an auth boundary.
4. Add restrictive runtime file permissions when credentials exist.
5. Add `team_stable_current` to the MCP-shaped surface if roles need it.
6. Prefer Unix socket app-server endpoints once Codex support is easy to wire.
7. Keep the live demo scenario exercising the current supervision feature set.

## Later, Only If Forced

- Active Slurm run registry.
- Retry/backoff for notifications.
- richer obligation status filters.
- Subagent join ledger.
- Real MCP SDK dependency.
- Custom notification providers.
- Non-Codex agent backends.

## Success Criteria

The design is working when:

- no agent can overwrite a human's half-written prompt;
- no task body depends on tmux paste delivery;
- every message has a durable status;
- failed notification does not imply lost work;
- a restarted agent can recover pending messages;
- collector/trainer cannot accidentally sync unapproved commits;
- the human can inspect message history without reading panes.
