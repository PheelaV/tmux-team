# Agent Instructions

This file is the canonical contributor guide for automated agents working in this repository.

## Start Here

Read these before changing code:

- `CONTRIBUTING.md`
- `README.md`
- `docs/index.md`
- `docs/invariants.md`

Use `kb/` as agent-facing design memory and history. Verify current behavior against `src/`, tests, `README.md`, and `docs/` before treating KB plans as implementation truth.

## Repository Rules

- Keep changes small and aligned with the existing Python standard-library-first style.
- Use `apply_patch` for manual edits.
- Prefer `rg` for search.
- Keep examples generic. Do not add private project names, absolute operator paths, branches, job IDs, tokens, or customer data to docs, tests, KB files, or fixtures.
- Update `CHANGELOG.md` for user-visible CLI, config, runtime-state, skill, or behavior changes.
- Put human operating guidance in `README.md` or `docs/`; put design history and future plans in `kb/`.
- When team workflow rules change, update both `skills/start-tmux-team/SKILL.md` and `skills/start-tmux-team/references/invariants.md`.

## Test Expectations

Run the narrowest useful tests while iterating. Before handing off a substantial change, run:

```bash
make lint
make test
```

Run `make integration-test` for bootstrap, delivery, layout, lifecycle, or workflow changes.

## Runtime State Concepts

- SQLite inbox state is the durable task transport.
- Codex app-server wake turns notify role panes; tmux stdin is not the production wake path.
- Scratchpad memory is per-role durable operational state and should keep recent important state near the top.
- Milestones are the append-only operator timeline for broad achievements and state changes.
