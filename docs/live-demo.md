# Live Demo Scenario

This repository includes repeatable Codex and external-ACP demo paths for validating tmux-team against one public codebase snapshot. They are dogfood investigation runs, not just passing-test fixtures.

The scenario intentionally clones `https://github.com/PheelaV/tmux-team.git` at the fixed public snapshot `v0.1.3` / `78602d1497a81f0e8e5026999585a65c1eea19b1`, then seeds a small urgent-priority regression in that cloned target. The old tag is the demo target, not the current tmux-team release. The setup creates separate orchestrator/implementer/collector worktrees and writes a goal for the orchestrator. The goal describes the behavior and success criteria, but not the faulty function or patch.

Set it up:

```bash
just live-demo-setup
```

Start the live Codex team:

```bash
just live-demo-bootstrap
tmux attach -t tt-live-demo
```

Bootstrap opens the live Textual dashboard as a split next to `tt-control`, so the operator can watch durable state without leaving the control window.

To start an ACP team, pass a provider and an adapter-advertised model. Set `TMUX_TEAM_RUN_LIVE_ACP=1` to enable provider calls.

```bash
TMUX_TEAM_RUN_LIVE_ACP=1 just live-demo-acp-bootstrap cursor '<model-id>'
tmux attach -t tt-live-demo
```

Pass `codex` or `claude` to run the same scenario through `codex-acp` or `claude-agent-acp`. Each adapter runs locally over stdio.

ACP bootstrap intentionally defers the goal so the operator can attach before work starts. From another shell, start
the durable scenario after the observer is ready:

```bash
just live-demo-acp-start
```

Use `LIVE_DEMO_ACP_PROVIDER` and `LIVE_DEMO_ACP_MODEL` to supply defaults for the recipe arguments. Override
`LIVE_DEMO_ACP_AGENT_COMMAND` or `LIVE_DEMO_ACP_TUI_BIN` to test a specific adapter or Toad build. The Cursor demo
uses `agent --force acp`. Choose a stricter command when a human will approve role tools. ACP mode uses the same
seeded repository and role worktrees and verifies control-socket
delivery plus exact provider-session sleep/resume. Toad must first establish each provider session; tmux-team then
confirms the requested model before dispatching the durable scenario goal.

Codex defaults to medium reasoning with fast mode off. Claude defaults to medium effort and accepts
`LIVE_DEMO_ACP_FAST=true` or `false` when the adapter
advertises fast mode. For Cursor, choose an advertised model variant with the options
you intend to test. Bootstrap confirms these options before sending the first startup prompt. Codex uses
`INITIAL_AGENT_MODE=agent-full-access`. The Claude target
creates ignored worktree-local `bypassPermissions` settings only inside the disposable fixture.

The live scenario has an operator recovery phase. After the collector reports the first passing stable verification and the orchestrator arms the post-resume watchdog, trigger sleep/resume from the control side:

```bash
just live-demo-sleep
just live-demo-resume
just live-demo-watchdog-now
```

`live-demo-watchdog-now` changes the restored watchdog from report-only supervision to `app-server-turn` delivery and shortens it to `5s` so it wakes the orchestrator for a second post-resume operation. The orchestrator should then route one implementer test-only task, complete the post-resume obligation, and stop the watchdog once the tests stay passing.

After ACP role work finishes, run `just live-demo-sleep` and `just live-demo-resume`, then verify. ACP uses exact
`session/load` restoration and the verifier compares every resumed provider session ID with the sleep snapshot. The
watchdog nudge and second implementer operation apply only to the Codex runtime.

After the agents report final completion, verify real success:

```bash
just live-demo-verify
```

Clean up:

```bash
just live-demo-clean
```

The scenario asks the orchestrator to exercise the main operating surfaces:

- durable role dispatch with stable `--correlation-key` values;
- completion replies back to the original sender;
- operator recovery metadata plus configured role Codex launch settings;
- tmux truecolor session setup from bootstrap/resume;
- `status --verbose`, `dashboard --once`, `inbox list --verbose`, `pane list --all`, `pane capture --lines/--offset`, and `watchdog`;
- one-shot watchdog pressure delivery with `--delivery app-server-turn` and `--notify-role orchestrator`;
- operator-triggered `sleep`/`resume`;
- reinstantiation of a persistent watchdog runner after resume;
- post-resume watchdog `update` from report-only to wake-capable near-future delivery and final `stop` after its goal is reached;
- obligation start/update/complete state;
- milestone recording by the orchestrator only;
- stable commit approval and collector sync;
- `broadcast --notice --only implementer,collector` and `broadcast --notice --exclude orchestrator`.

The verifier checks that the final target test passes in the collector worktree, the implementer produced a fix commit, the collector verified the approved stable commit in its own worktree, operator metadata and role Codex launch settings are present, tmux truecolor session settings are active, the team slept and resumed, the persistent watchdog was restarted by resume, and the runtime database contains the expected messages, watchdog pressure escalations, completion notices, notice broadcasts, completed obligation state, stable approval, obligation/watchdog events, and milestones. It fails duplicate collector verification dispatches by requiring the exact expected correlation keys and message counts.

Role resizing is covered by the local integration tests rather than this live bugfix scenario.

Override the root or session when needed:

```bash
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo just live-demo-setup
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo just live-demo-bootstrap
```
