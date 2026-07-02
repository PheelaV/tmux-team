# Installable Extension Model

## Current Status

`tmux-team` is now an installable Python package with:

- `tmux-team` CLI entry point;
- TOML team config;
- SQLite runtime store;
- role state commands;
- app-server wake delivery;
- sleep snapshots;
- stable commit commands;
- project-local executable hooks;
- a repo-installed `start-tmux-team` Codex skill.

This document is agent memory. Prefer current behavior in `README.md`, `docs/`, and tests.

## Current Contract

- Project config is `.tmux-team/team.toml`.
- Runtime truth is SQLite plus message body files under the runtime directory.
- `tmux-team bootstrap` creates the visible tmux shape and Codex app-server bindings.
- `tmux-team role pause/resume/drain/retire/fail` is the supported mutable team-shape surface.
- `tmux-team sleep` writes TOML snapshots under `.tmux-team/runtime/sleeps/`.
- Project extensions live under `.tmux-team/extensions/<id>/extension.toml`.

## Keep Small

Do not add a reconciler, global user config, role scaling, custom notification providers, or non-Codex backends until a real workflow needs them.

The useful next work is in [04_hardening_checklist.md](04_hardening_checklist.md).
