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

The live scenario has an operator recovery phase. After the collector reports the first passing stable verification and the orchestrator arms the post-resume watchdog, trigger sleep/resume from the control side:

```bash
make live-demo-sleep
make live-demo-resume
make live-demo-watchdog-now
```

`live-demo-watchdog-now` changes the restored watchdog from report-only supervision to `app-server-turn` delivery and shortens it to `5s` so it wakes the orchestrator for a second post-resume operation. The orchestrator should then route one implementer test-only task, complete the post-resume obligation, and stop the watchdog once the tests stay passing.

After the agents report final completion, verify real success:

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
- operator-triggered `sleep`/`resume`;
- reinstantiation of a persistent watchdog runner after resume;
- post-resume watchdog `update` from report-only to wake-capable near-future delivery and final `stop` after its goal is reached;
- obligation start/update/complete state;
- milestone recording by the orchestrator only;
- stable commit approval and collector sync;
- `broadcast --notice --only` and `broadcast --notice --exclude`.

The verifier checks that the final target test passes in the collector worktree, the implementer produced a fix commit, the collector verified the approved stable commit in its own worktree, operator metadata and role Codex launch settings are present, the team slept and resumed, the persistent watchdog was restarted by resume, and the runtime database contains the expected messages, watchdog pressure escalations, completion notices, notice broadcasts, completed obligation state, stable approval, obligation/watchdog events, and milestones. It fails duplicate collector verification dispatches by requiring the exact expected correlation keys and message counts.

Role resizing is covered by the local integration tests rather than this live bugfix scenario.

Override the root or session when needed:

```bash
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo make live-demo-setup
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo make live-demo-bootstrap
```
