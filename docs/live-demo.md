# Live Demo Scenario

This repository includes repeatable Codex and external-ACP demo paths for validating tmux-team against one public codebase snapshot. They are dogfood investigation runs, not just passing-test fixtures.

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

Bootstrap opens the live Textual dashboard as a split next to `tt-control`, so the operator can watch durable state without leaving the control window.

Alternatively, start a provider-agnostic ACP/Toad team. Cursor is the default provider command for this demo:

```bash
TMUX_TEAM_RUN_LIVE_ACP=1 \
make live-demo-acp-cursor-bootstrap
tmux attach -t tt-live-demo
```

Replace `cursor` in the target with `codex`, `claude`, or `pool` to run the same orchestrated scenario through
`codex-acp`, `claude-agent-acp`, or `pool acp`. Set `LIVE_DEMO_ACP_POOL_MODEL` explicitly because Pool model catalogs
are deployment-specific. All providers run as local stdio adapters; Pool may separately target a remote deployment
through Pool-owned settings/environment.
The Pool target verifies its already-advertised runtime model instead of mutating model selection through ACP.

ACP bootstrap intentionally defers the goal so the operator can attach before work starts. From another shell, start
the durable scenario after the observer is ready:

```bash
make live-demo-acp-start
```

The real-provider path is deliberately gated. Set `TMUX_TEAM_RUN_LIVE_ACP=1` to accept provider usage and set
`LIVE_DEMO_ACP_MODEL` explicitly so the selected task model is intentional:

```bash
TMUX_TEAM_RUN_LIVE_ACP=1 \
LIVE_DEMO_ACP_MODEL='<provider-model-and-options>' \
make live-demo-acp-bootstrap
```

Override `LIVE_DEMO_ACP_AGENT_COMMAND`, `LIVE_DEMO_ACP_PROVIDER`, or `LIVE_DEMO_ACP_TUI_BIN` to exercise another
compatible ACP provider/TUI. The autonomous Cursor demo uses `agent --force acp`; choose a stricter command when a
human will approve role tools. ACP mode uses the same seeded repository and role worktrees and verifies control-socket
delivery plus exact provider-session sleep/resume. Toad must first establish each provider session; tmux-team then
confirms the requested model before dispatching the durable scenario goal.

The Cursor and Codex targets explicitly select GPT-5.6 Terra with medium reasoning and fast off. The Claude target
selects Claude Opus 4.8 with medium effort. Claude ACP does not currently advertise a fast-mode config option. Bootstrap
applies and confirms these options before sending
the first startup prompt. The Codex target also selects `INITIAL_AGENT_MODE=agent-full-access`; the Claude target
creates ignored worktree-local `bypassPermissions` settings only inside the disposable fixture.

The live scenario has an operator recovery phase. After the collector reports the first passing stable verification and the orchestrator arms the post-resume watchdog, trigger sleep/resume from the control side:

```bash
make live-demo-sleep
make live-demo-resume
make live-demo-watchdog-now
```

`live-demo-watchdog-now` changes the restored watchdog from report-only supervision to `app-server-turn` delivery and shortens it to `5s` so it wakes the orchestrator for a second post-resume operation. The orchestrator should then route one implementer test-only task, complete the post-resume obligation, and stop the watchdog once the tests stay passing.

After ACP role work finishes, run `make live-demo-sleep` and `make live-demo-resume`, then verify. ACP uses exact
`session/load` restoration and the verifier compares every resumed provider session ID with the sleep snapshot. The
watchdog nudge and second implementer operation apply only to the Codex runtime.

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
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo make live-demo-setup
LIVE_DEMO_ROOT=/tmp/my-tt-demo LIVE_DEMO_SESSION=tt-my-demo make live-demo-bootstrap
```
