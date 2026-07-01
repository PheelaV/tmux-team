# tmux-team

Design notes and prototype space for a lightweight tmux-backed agent-team control plane.
Codex is the first target backend; other agent CLIs can be added later.

## MVP

The current MVP is a small Python CLI backed by SQLite.

Install requires `uv` or `pipx`; `make install-dev` prefers `uv` and falls back to `pipx`.

Read the operating invariants before changing bootstrap behavior:

- [docs/invariants.md](docs/invariants.md)

```bash
make install-dev
make install-skill
```

The intended Codex workflow starts from one operator-owned tmux pane:

```bash
tmux new-session -s my-project -c /path/to/project
codex
```

Then ask Codex to use the `start-tmux-team` skill and start the team for the current project. The skill calls:

```bash
tmux-team bootstrap --project-root . --goal "USER_GOAL"
```

If bootstrap is launched from inside tmux, it uses the current tmux session unless `--session` is provided. Otherwise it creates a named session from the project directory.

Bootstrap names the launcher window `control-plane`, starts a visible `app-server` tmux window, opens remote Codex TUI panes in a tiled `agents` window with `codex --remote ...`, waits for each TUI to create a loaded app-server thread, writes those discovered thread IDs and pane targets to `.tmux-team/team.toml`, queues the initial goal to `orchestrator`, and wakes the orchestrator with app-server `turn/start`. It does not type into any tmux prompt.

If role agents need to message each other without stopping at Codex approval prompts, launch managed role panes with an explicit role execution policy:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

`--role-profile` passes a named Codex profile to each managed role TUI. `--role-yolo` passes Codex `--dangerously-bypass-approvals-and-sandbox` to managed role TUIs only. Use YOLO mode only when the project/worktree is already the sandbox you accept for those agents.

The default agent layout is grouped:

```bash
tmux-team bootstrap --project-root . --agent-layout grouped
```

Use separate role windows only when you explicitly want that layout:

```bash
tmux-team bootstrap --project-root . --agent-layout separate-windows
```

Manual CLI operations are still available:

```bash
tmux-team init --name example-team --runtime-dir /tmp/tmux-team-example
tmux-team status
tmux-team send --to orchestrator --summary "B19 failed" --body-file report.md
tmux-team inbox next --role orchestrator
tmux-team inbox ack <message-id> --role orchestrator
tmux-team inbox complete <message-id> --role orchestrator --summary "routed"
tmux-team sleep
```

`inbox next` claims one message. If a role is woken with multiple pending messages, it should claim, ack, do, and complete one message, then run `inbox next` again until there is no pending work.

Config lives at `.tmux-team/team.toml` by default. Runtime state lives in the configured runtime directory and includes:

- `team.sqlite` for durable state;
- `events.jsonl` for append-only audit;
- `messages/*.md` for message bodies.
- `sleeps/*.toml` for operator-facing sleep/restart snapshots.

`tmux-team sleep` snapshots role state, pane targets, tmux session/window/pane IDs, and Codex app-server thread bindings before tearing down managed role/app-server windows. It leaves `control-plane` alive by default and marks active/draining roles paused so stale bindings do not receive new work. Use `tmux-team sleep --dry-run` to inspect the plan first.

Tmux notification uses `tmux display-message` by default. It does not type into the agent's prompt composer.

Wake-capable Codex delivery uses Codex app-server remote TUI mode. Bootstrap configures this automatically, but the manual form is:

```bash
codex app-server --listen ws://127.0.0.1:4500
codex --remote ws://127.0.0.1:4500
tmux-team codex bind implementer --endpoint ws://127.0.0.1:4500 --thread-id <thread-id>
tmux-team send --to implementer --summary "..." --body-file task.md --notify-method app-server-turn
```

`app-server-turn` submits a real Codex turn to the role's thread. The pane stays the live Codex UI, but `tmux-team` never types into the pane.

## Tests

Unit tests:

```bash
make lint
make test
```

The unit suite includes a fake app-server WebSocket test for `app-server-turn` delivery.

Deterministic fake-agent smoke test:

```bash
make integration-test
make bootstrap-layout-smoke-test
make smoke-test
make congestion-smoke-test
```

`make integration-test` is the default local confidence suite. It runs Ruff, unit tests, the real tmux bootstrap/sleep layout smoke, the basic fake-agent workflow, and the congestion/multiple-message workflow.

Visible tmux run:

```bash
tmux new-session -s tt-sandbox -c "$PWD"
```

Then, from another terminal:

```bash
cd /path/to/tmux-team
uv run --with-editable . python scripts/sandbox_demo.py --session tt-sandbox --root /tmp/tmux-team-sandbox --force
```

Dockerized deterministic smoke test:

```bash
make docker-test
make docker-smoke-test
make docker-congestion-smoke-test
```

Opt-in real Codex integration:

```bash
TMUX_TEAM_RUN_CODEX=1 make codex-integration-test
```

The real Codex test uses `codex exec` against a disposable project. It is skipped unless `TMUX_TEAM_RUN_CODEX=1` is set because it requires auth, network, model availability, and a real model call.

Pro-friendly Docker filesystem pass-through:

```bash
TMUX_TEAM_RUN_CODEX=1 make codex-docker-fs-integration-test
```

This runs Codex on the host, using your normal local Codex auth, while Docker only sees the bind-mounted sandbox filesystem for final verification.

Dockerized real Codex integration is configurable:

```bash
make docker-codex-login
make docker-codex-integration-test
```

For API-key auth instead:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" make docker-codex-integration-test
```

The Docker image installs Codex with `npm install -g @openai/codex`. Override with `CODEX_NPM_PACKAGE` if you need a pinned version. Docker Codex auth is persisted under `.tmux-team/codex-home`, which is ignored by git.

Start with the knowledge base:

- [kb/01_init.md](kb/01_init.md)
- [kb/02_delivery_design.md](kb/02_delivery_design.md)
- [kb/03_integration_options.md](kb/03_integration_options.md)
- [kb/04_hardening_checklist.md](kb/04_hardening_checklist.md)
- [kb/05_installable_extension.md](kb/05_installable_extension.md)
- [kb/06_hermes_comparison.md](kb/06_hermes_comparison.md)
- [kb/07_principles.md](kb/07_principles.md)
- [kb/08_extensibility_and_hooks.md](kb/08_extensibility_and_hooks.md)
- [docs/invariants.md](docs/invariants.md)
