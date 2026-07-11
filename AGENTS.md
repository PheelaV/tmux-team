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
- Keep examples generic. Do not add private project names, absolute operator paths, branches, job IDs, tokens, or customer data to docs, tests, KB files, or fixtures.
- Update `CHANGELOG.md` for user-visible CLI, config, runtime-state, skill, or behavior changes.
- Put human operating guidance in `README.md` or `docs/`; put design history and future plans in `kb/`.
- When team workflow rules change, update both `skills/start-tmux-team/SKILL.md` and `skills/start-tmux-team/references/invariants.md`.

## Preferred CLI Tools

Prefer these tools when they are installed and usable. If a preferred tool is
missing or broken and installing it would help, ask before installing it. Until
then, use the closest standard fallback and mention the fallback when it affects
the command, output, or reproducibility.

Do not borrow syntax from the tool being replaced. Use each preferred tool's
own flags and argument shape.

- Use `rg` (ripgrep) instead of `grep` to search file contents. `rg` searches
  recursively by default. `-r` / `--replace` sets replacement text; it is not
  `grep`'s recursive flag. Never write `rg -rn` or bare `rg -r` for recursive
  search. Use `rg PATTERN`, adding `-n` only when explicit line numbers help.
- Use `fd` instead of `find` to locate files. `fd` takes a regex or pattern
  directly (`fd PATTERN`), not `find`'s `-name` / `-type` expression syntax.
- Use `sd` instead of `sed` for find-and-replace. `sd` uses
  `sd FIND REPLACE` (regex by default), not `sed`'s `s/find/replace/` syntax.
- Use `ast-grep` for structural AST searches and rewrites.
- Use `jq` to query and transform JSON.
- Use `bat` instead of `cat` to view files with syntax highlighting.

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
