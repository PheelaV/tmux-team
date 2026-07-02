# tmux-team

![tmux-team banner](docs/assets/banner.png)

Minimal tmux-backed control plane for pane-visible agent teams.

Codex is the first target backend. Other agent CLIs can be added later without changing the core invariant: agents stay visible in tmux, while durable work moves through `tmux-team` state.

## Install

Prerequisites: `tmux`, Codex CLI authenticated locally, and either `uv` or `pipx`.

Install the CLI from GitHub:

```bash
uv tool install git+https://github.com/PheelaV/tmux-team.git
# or
pipx install git+https://github.com/PheelaV/tmux-team.git
```

Install the Codex plugin/skill from the public marketplace metadata:

```bash
codex plugin marketplace add PheelaV/tmux-team --ref main
codex plugin add tmux-team@tmux-team
```

You can also add the marketplace from Codex and install through `/plugins install`.

The plugin installs the `start-tmux-team` skill. The CLI is still installed separately with `uv` or `pipx`; the plugin does not mutate global Python tools.

If the skill says `tmux-team` is missing, install the CLI with one of the commands above and retry.

Checkout fallback for the skill:

```bash
git clone https://github.com/PheelaV/tmux-team.git
cd tmux-team
make install-skill
```

For local development, use the checkout:

```bash
make install-dev
make install-skill
```

Read the human docs before changing bootstrap or delivery behavior:

- [docs/index.md](docs/index.md)
- [docs/invariants.md](docs/invariants.md)

## Versioning And Updates

Maintainer release checklist:

1. Bump both versions to the same value:
   - `pyproject.toml` -> `[project].version`
   - `.codex-plugin/plugin.json` -> `"version"`
2. Set `.agents/plugins/marketplace.json` plugin source `ref` to the matching tag, for example `v0.1.1`.
3. Run:

```bash
make lint
make test
uv run --with pyyaml python /path/to/validate_plugin.py .
```

4. Commit, tag, and push:

```bash
git commit -am "Release v0.1.1"
git tag v0.1.1
git push
git push origin v0.1.1
```

User update:

```bash
uv tool install --force git+https://github.com/PheelaV/tmux-team.git
# or
pipx install --force git+https://github.com/PheelaV/tmux-team.git

codex plugin marketplace upgrade tmux-team
codex plugin add tmux-team@tmux-team
```

Start a new Codex thread after updating the plugin so Codex reloads the skill.

## Getting Started: Fix A Failing Test

Start with a low-risk repo. The point is not to manually chat with four panes; give the orchestrator one durable goal and let roles pass work through the inbox.

```bash
cd /path/to/project
tmux new-session -s my-project -c "$PWD"
codex
```

In that Codex control pane, ask:

```text
Use the start-tmux-team skill.

Goal:
Run the smallest failing test, route implementation work to the implementer,
and report the final test command and result. Keep changes inside this repo.
```

Equivalent direct command:

```bash
tmux-team bootstrap --project-root . --goal "Run the smallest failing test, route implementation work to the implementer, and report the final test command and result. Keep changes inside this repo."
```

If bootstrap is launched from inside tmux, it uses the current tmux session unless `--session` is provided. Otherwise it creates a named session from the project directory.

Bootstrap names the launcher window `control-plane`, starts a visible `app-server` tmux window, opens remote Codex TUI panes in a tiled `agents` window with `codex --remote ...`, waits for each TUI to create a loaded app-server thread, writes those discovered thread IDs and pane targets to `.tmux-team/team.toml`, queues the initial goal to `orchestrator`, and wakes the orchestrator with app-server `turn/start`. It does not type into any tmux prompt.

Watch progress from the control pane:

```bash
tmux-team status
tmux-team inbox list --role orchestrator
tmux-team inbox list --role implementer
```

Stop the managed team without killing your control pane:

```bash
tmux-team sleep
```

## How It Works

`tmux-team` is a small Python CLI backed by SQLite, TOML config, tmux windows, and Codex app-server remote TUI wake delivery.

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
tmux-team ext list
tmux-team ext doctor
tmux-team sleep
```

`inbox next` claims one message. If a role is woken with multiple pending messages, it should claim, ack, do, and complete one message, then run `inbox next` again until there is no pending work.

Config lives at `.tmux-team/team.toml` by default. Runtime state lives in the configured runtime directory and includes:

- `team.sqlite` for durable state;
- `events.jsonl` for append-only audit;
- `messages/*.md` for message bodies;
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

Project-local extensions live under `.tmux-team/extensions/<name>/extension.toml`. The first extension surface supports executable JSON hooks around message creation, claim, ack, completion, and notification operations. See [docs/extensions.md](docs/extensions.md).

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

Agent-facing design memory lives in [kb/00_index.md](kb/00_index.md).
