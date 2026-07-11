# Testing Strategy

`tmux-team` needs three separate test layers.

## 1. Unit Tests

Purpose: verify deterministic local behavior with no tmux, no Docker, no Codex, no network.

Run:

```bash
make test
```

Coverage target:

- config loading;
- SQLite schema and state transitions;
- message lifecycle;
- role state changes;
- stable commit lookup;
- CLI parsing for common commands;
- fake Codex app-server WebSocket delivery for `app-server-turn`.
- fake ACP TUI control-socket delivery for `control-socket`.

The app-server delivery unit test uses `socketpair()` and a fake WebSocket JSON-RPC server. It verifies the wake turn protocol without opening a network listener, starting Codex, or using credentials.

The ACP TUI unit tests exercise the versioned Unix-socket client, readiness/status handshake, structured errors, and
SQLite wake routing without starting Toad or a provider. They verify that task bodies stay out of wake prompts.
Runtime-switch tests mock Toad readiness and tmux respawn while verifying
capsule redaction, idle/cancel gates, TOML preservation, failure-to-draining
behavior, recovery prompts, and append-only session lineage.

The live-demo fixture has two runtime entry points: `live-demo-bootstrap` for Codex/app-server recovery coverage and
`live-demo-acp-bootstrap` for external ACP/Toad control-socket coverage. ACP bootstrap intentionally waits for the
operator to attach; `live-demo-acp-start` then submits the durable goal. Both use the same public snapshot and
deterministic verifier.

## 2. Deterministic Fake-Agent Smoke Test

Purpose: verify the tmux-backed control plane with fake deterministic agents.

Run locally:

```bash
make integration-test
make bootstrap-layout-smoke-test
make smoke-test
make congestion-smoke-test
```

`integration-test` is the default local confidence suite. It runs:

- Ruff lint and format checks;
- unit tests;
- the real tmux bootstrap/sleep layout smoke;
- the basic fake-agent workflow;
- the congestion/multiple-message workflow.

`bootstrap-layout-smoke-test` verifies the real bootstrap tmux shape with a fake Codex binary: `tt-control`, isolated `tt-app-server`, and one tiled `tt-agents` window with the default role panes. It then runs `tmux-team sleep`, verifies a TOML sleep snapshot, and confirms only `tt-control` remains.

Run in Docker:

```bash
make docker-test
make docker-smoke-test
make docker-congestion-smoke-test
```

This test creates a disposable project with a failing calculator unit test. Three shell-driven fake roles run inside tmux:

- `collector` finds the failing test and sends evidence to `orchestrator`;
- `orchestrator` claims the message, acknowledges it, and routes a task to `implementer`;
- `implementer` claims the task, fixes the code, runs tests, and completes the message.

The test verifies:

- final project tests pass;
- expected messages are `completed`;
- tmux notification records exist;
- all state is stored under the sandbox runtime directory.

This is the default confidence test. It must stay deterministic and credential-free.

The congestion variant adds:

- a paused role that blocks normal work;
- an urgent message that bypasses the paused-role block;
- multiple queued priorities for one role;
- canonical pending visibility after successful notification, before claim;
- multiple orchestrator inbox items;
- two independent code regressions;
- claim-order verification from the SQLite event ledger.

## 3. Real Codex Integration Test

Purpose: verify that a real Codex agent can consume a `tmux-team` inbox item, execute a task, and complete the message.

Run on the host:

```bash
TMUX_TEAM_RUN_CODEX=1 make codex-integration-test
```

This test is opt-in because it requires:

- a working `codex` executable;
- Codex authentication;
- provider network reachability;
- model/runtime availability;
- willingness to spend a real model call.

The test uses `codex exec`, not the interactive TUI. That is intentional: the test should verify the service protocol and task execution, not terminal keystroke automation.

### Docker as Filesystem and Verifier

For individual Pro accounts, prefer the pass-through mode:

```bash
TMUX_TEAM_RUN_CODEX=1 make codex-docker-fs-integration-test
```

This mode keeps Codex in user space on the host, so it can use the normal local ChatGPT login. The disposable project is a host directory under `/tmp`, and Docker bind-mounts that directory only for final verification:

```text
host Codex process
  edits /tmp/tmux-team-codex-fs-itest/project
Docker verifier
  mounts that same directory at /workspace
```

This avoids trying to copy host keychain or ChatGPT browser-login state into Linux Docker. Docker is the isolated filesystem/verifier, not the Codex runtime.
