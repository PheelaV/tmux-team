# Live Demo Scenario

This repository includes a repeatable real-Codex demo scenario for validating tmux-team against a public codebase snapshot. It is meant to be a dogfood investigation run, not just a passing-test fixture.

The scenario intentionally clones `https://github.com/PheelaV/tmux-team.git` at the fixed public snapshot `v0.1.3` / `78602d1497a81f0e8e5026999585a65c1eea19b1`, then seeds a small urgent-priority regression in that cloned target. The old tag is the demo target, not the current tmux-team release. The setup creates separate orchestrator/implementer/collector worktrees and writes a goal for the orchestrator. The goal describes the behavior and success criteria, but not the faulty function or patch.

Set it up:

```bash
make live-demo-setup
```

Start the live Codex team:

```bash
make live-demo-bootstrap
tmux attach -t tt-live-demo
```

After the agents report completion, verify real success:

```bash
make live-demo-verify
```

Clean up:

```bash
make live-demo-clean
```

The scenario asks the orchestrator to exercise the main operating surfaces:

- durable role dispatch with stable `--correlation-key` values;
- completion replies back to the original sender;
- operator recovery metadata plus configured role Codex launch settings;
- `status --verbose`, `dashboard --once`, `inbox list --verbose`, `pane list --all`, `pane capture --lines/--offset`, and `watchdog`;
- one-shot watchdog pressure delivery with `--delivery app-server-turn` and `--notify-role orchestrator`;
- obligation start/update/complete state;
- milestone recording by the orchestrator only;
- stable commit approval and collector sync;
- `broadcast --notice --only` and `broadcast --notice --exclude`.

The verifier checks that the final target test passes in the collector worktree, the implementer produced a fix commit, the collector verified the approved stable commit in its own worktree, operator metadata and role Codex launch settings are present, and the runtime database contains the expected messages, watchdog pressure escalation, completion notices, notice broadcasts, completed obligation state, stable approval, obligation/watchdog events, and milestones. It fails duplicate collector verification dispatches by requiring the exact expected correlation keys and message counts.

Lifecycle features such as `sleep`, `resume`, and role resizing are covered by the local integration tests rather than this live bugfix scenario.

Override the root or session when needed:

```bash
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo make live-demo-setup
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo make live-demo-bootstrap
```
