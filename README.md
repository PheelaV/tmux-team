# tmux-team

![tmux-team banner](docs/assets/banner.png)

`tmux-team` is a tiny tmux-native control plane for visible Codex agent teams.

Plain tmux is great until you have four agents: prompts collide, panes enter copy mode, messages disappear into scrollback, and nobody knows what is actually done.

`tmux-team` keeps the useful part of tmux: every agent remains visible, interruptible, and human-operable. It moves coordination out of pane text and into durable state: SQLite inboxes, ack/complete tracking, role-owned todos, obligations, scratchpad memory, app-server wakeups, milestones, watchdogs, and sleep/resume snapshots.

The bias is boring reliability: visible panes, explicit states, recoverable claims, and no terminal stdin as production transport.

## Feel The Magic

From a checkout, run the repeatable live demo:

```bash
make live-demo-setup
make live-demo-bootstrap
tmux attach -t tt-live-demo
make live-demo-verify
make live-demo-clean
```

The demo starts a visible Codex team, gives the orchestrator a failing test, routes implementation work, verifies the fix in a collector worktree, approves a stable commit, and exits with a clean inbox.

Expected shape:

```text
orchestrator: routed failing test to implementer
implementer: fixed regression and produced a commit
collector: verified the approved commit in a separate worktree
watchdog: no stale claims or overdue obligations
stable: approved commit recorded
verifier: LIVE DEMO VERIFY OK
```

## What This Is Not

`tmux-team` is not a general agent framework, a virtual office, or a hidden background daemon. It is a local control plane for a handful of visible Codex agents working in tmux.

## What You Get

- Visible tmux panes for every role, with `tt-control` and `tt-app-server` kept separate.
- Durable SQLite inboxes with claim, ack, complete, completion replies, and reclaimable stale work.
- App-server wake turns instead of production `tmux send-keys`.
- Per-role scratchpad memory for long-lived state and active-message todos for reset-safe substeps.
- Operator timelines, obligations, watchdog runners, pane capture, and an optional Textual dashboard.
- Sleep/resume snapshots so a team can stop and come back without losing role bindings or watchdog runners.

## Install

Prerequisites: `tmux`, Codex CLI authenticated locally, and either `uv` or `pipx`.

Install the CLI from GitHub:

```bash
uv tool install git+https://github.com/PheelaV/tmux-team.git
# or
pipx install git+https://github.com/PheelaV/tmux-team.git
```

Install the optional Textual dashboard extra when you want the live operator dashboard:

```bash
uv tool install "tmux-team[dashboard] @ git+https://github.com/PheelaV/tmux-team.git"
# or
pipx install "tmux-team[dashboard] @ git+https://github.com/PheelaV/tmux-team.git"
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

## Getting Started: Fix A Failing Test

Start with a low-risk repo. The point is not to manually chat with four panes; give the orchestrator one durable goal and let roles pass work through the inbox.

```bash
cd /path/to/project
tmux new-session -s tt-my-project -c "$PWD"
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

If bootstrap is launched from inside tmux, it uses the current tmux session unless `--session` is provided. Otherwise it creates `tt-<project>` from the project directory name.

Bootstrap names the launcher window `tt-control`, starts a visible `tt-app-server` tmux window, opens remote Codex TUI panes in a tiled `tt-agents` window with `codex --cd <role-worktree> --remote ...`, waits for each TUI to create a loaded app-server thread, writes those discovered thread IDs and pane targets to `.tmux-team/team.toml`, queues the initial goal to `orchestrator`, and wakes the orchestrator with app-server `turn/start`. It does not type into any tmux prompt.

`--goal` and `--goal-file` seed only the initial operator message to `orchestrator`. Keep them to the objective, boundaries, and success criteria; the orchestrator should decompose that into scoped role inbox messages.

Each spawned role starts with a small tmux-team bootstrap prompt: load the `start-tmux-team` skill, read scratchpad memory, then claim inbox work or park. Scratchpads keep latest operational state near the top so context compression or pane restart does not erase the role's long-term goal.

Watch progress from the control pane:

```bash
tmux-team status
tmux-team status --verbose
tmux-team inbox list --role orchestrator
tmux-team inbox list --role implementer
tmux-team pane capture implementer --lines 80 --offset 0
tmux-team milestone list --today
```

Stop the managed team without killing your control pane:

```bash
tmux-team sleep
tmux-team resume
```

## How It Works

`tmux-team` is a small Python CLI backed by SQLite, TOML config, tmux windows, and Codex app-server remote TUI wake delivery.

If role agents need to message each other without stopping at Codex approval prompts, launch managed role panes with an explicit role execution policy:

```bash
tmux-team bootstrap --project-root . --role-profile tmux-team-role
tmux-team bootstrap --project-root . --role-yolo
```

`--role-profile` passes a named Codex profile to each managed role TUI. `--role-yolo` passes Codex `--dangerously-bypass-approvals-and-sandbox` to managed role TUIs only. Use YOLO mode only when the project/worktree is already the sandbox you accept for those agents.

Set role-specific Codex launch options when roles need different models, reasoning effort, or profiles:

```bash
tmux-team bootstrap \
  --project-root . \
  --role-model orchestrator=gpt-5.5 \
  --role-reasoning-effort orchestrator=xhigh \
  --role-model collector=gpt-5.5 \
  --role-reasoning-effort collector=high \
  --role-codex-profile implementer=tmux-team-role
```

For advanced Codex config, pass repeatable per-role `-c` overrides:

```bash
tmux-team bootstrap --project-root . --role-codex-config collector='model_reasoning_effort="high"'
```

Configured role Codex launch settings are recorded in `team.toml`, included in sleep snapshots, and replayed by `tmux-team resume`. Running watchdog runners are also reinstantiated on resume. If a host or tmux session ends before a graceful sleep, resume can build a recovery snapshot from `team.toml` and SQLite runtime state. Live TUI-only state that Codex does not expose, such as `/fast`, is reported as unknown; verify it manually after recovery if it matters.

Bootstrap and resume set tmux truecolor options on the managed session by default: `default-terminal` to `tmux-256color`, RGB terminal features when supported, and `COLORTERM=truecolor`. Use `--no-truecolor` only for unusual terminal stacks that mis-render color.

The default agent layout is grouped:

```bash
tmux-team bootstrap --project-root . --agent-layout grouped
```

Use separate role windows only when you explicitly want that layout:

```bash
tmux-team bootstrap --project-root . --agent-layout separate-windows
```

Use per-role worktrees when roles need isolated checkout state:

```bash
tmux-team bootstrap \
  --project-root /repo/main \
  --roles orchestrator,implementer,collector,trainer \
  --role-worktree orchestrator=/repo/main \
  --role-worktree implementer=/repo/main \
  --role-worktree collector=/repo/main-collector \
  --role-worktree trainer=/repo/main-trainer \
  --allow-shared-worktree orchestrator,implementer
```

`project_root` remains the control/config root and the default role worktree. Each role with `--role-worktree ROLE=PATH` launches its Codex TUI with tmux `-c <path>` and Codex `--cd <path>`, and the generated `.tmux-team/team.toml` records `worktree = "..."` for the role.

To create missing worktrees before launch:

```bash
tmux-team bootstrap \
  --project-root /repo/main \
  --role-worktree collector=/repo/main-collector \
  --create-missing-worktrees \
  --worktree-base-ref HEAD
```

Bootstrap refuses explicitly mapped missing worktrees, non-git directories, dirty tracked files, and duplicated role worktrees unless allowed. Use `--allow-dirty-role ROLE` or `--allow-shared-worktree ROLE,ROLE` only when that is intentional.

## Common Operations

After bootstrap, most operator work uses a small command set. Use the full [CLI Reference](docs/cli-reference.md) when you need less common flags.

```bash
tmux-team status --verbose
tmux-team dashboard --once --provenance
tmux-team send --to implementer --summary "Fix failing parser test" --body-file task.md
tmux-team inbox next --role orchestrator --auto-ack
tmux-team inbox complete <message-id> --role orchestrator --summary "routed" --reply-to-sender
tmux-team todo recover --role collector
tmux-team obligation start --role collector --summary "Monitor verification" --next-update-in 15m
tmux-team pane capture collector --lines 120 --offset 40
tmux-team watchdog
tmux-team watchdog run --once --delivery app-server-turn --notify-role orchestrator
tmux-team watchdog start --name default --interval 15m --notify-role orchestrator --delivery app-server-turn
tmux-team watchdog update default --interval 10m --goal "Escalate stale work"
tmux-team watchdog pause default --reason "blocked by prerequisite" --review-in 30m
tmux-team watchdog resume default
tmux-team milestone add --team --kind routing --summary "Team started"
tmux-team milestone list --today
tmux-team sleep
tmux-team resume
tmux-team operator bind --pane %0 --codex-thread-id <thread-id>
```

The optional live dashboard is optimized for an operator tmux split: work/supervision and context/history are separate pages, pane preview starts off by default, and local theme plus concise/verbose preferences are stored under `.tmux-team/runtime/dashboard_preferences.json`.

The important loop is durable and one-message-at-a-time:

```text
send -> app-server wake -> inbox next -> ack -> work -> complete -> optional reply-to-sender
```

Use scratchpad memory for long-lived role state, todos for active-message substeps, milestones for operator summaries, obligations for long-running commitments, and pane capture only for live observation. Pane text is never the source of truth for delivery or completion.

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

Repeatable live Codex dogfood scenario:

```bash
make live-demo-setup
make live-demo-bootstrap
tmux attach -t tt-live-demo
make live-demo-verify
make live-demo-clean
```

The live demo clones a public snapshot, seeds a real failing test, and asks a visible Codex team to diagnose, fix, approve, and verify the change across separate role worktrees. Its verifier checks the final test result and durable coordination state, including correlation-key discipline, completion replies, notice broadcasts, obligations, milestones, stable approval, and clean final inbox state. See [docs/live-demo.md](docs/live-demo.md).

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

## Docs

Read the human docs before changing bootstrap or delivery behavior:

- [docs/index.md](docs/index.md)
- [docs/invariants.md](docs/invariants.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)

Agent-facing design memory lives in [kb/00_index.md](kb/00_index.md).
