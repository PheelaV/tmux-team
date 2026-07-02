# Changelog

All notable user-visible changes should be recorded here. Keep migration notes concrete enough that an operator or agent can resume an older tmux-team session safely.

## 0.1.2 - 2026-07-02

- Added append-only milestones via `tmux-team milestone add/list` and `.tmux-team/runtime/milestones.jsonl`.
- Documented milestone usage as the operator timeline for "what happened today?" and "what changed in the last 4h?" summaries.
- Restricted default milestone writes to the operator/control plane and orchestrator; non-orchestrator roles report evidence through inbox completion.
- Added detailed completion results with `tmux-team inbox complete --body/--body-file`; `--summary` remains the concise result.
- Added `tmux-team codex session-context` so Codex `SessionStart` hooks can restore role/framework context after startup, resume, clear, or compact without competing with the initial role spawn prompt.
- Clarified that `--goal` and `--goal-file` seed only the initial operator message to orchestrator, not role startup prompts.
- Shortened app-server wake prompts so they are blunt interrupts instead of repeating the role workflow.
- Tightened scratchpad memory guidance with a score threshold to avoid startup/parking/status spam.
- Added root agent guidance in `AGENTS.md` and a `CLAUDE.md` pointer to keep contributor instructions consistent.

Migration notes:

- Existing teams can keep running. New milestone logging starts when agents or operators begin using `tmux-team milestone add`.
- Update installed skills after pulling these changes so spawned agents receive the milestone and scratchpad instructions.
- If you add the optional Codex `SessionStart` recovery hook, review/trust it through Codex's normal hook trust flow before relying on it for reset recovery.
