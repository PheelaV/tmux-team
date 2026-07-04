# Contributing

Use the checkout for local development:

```bash
make install-dev
make install-skill
```

Run the default local checks before committing:

```bash
make integration-test
```

`make integration-test` runs lint, unit tests, the tmux bootstrap/sleep layout smoke test, and deterministic fake-agent workflows.

## Change Discipline

Keep user-visible changes easy to migrate.

- Update `CHANGELOG.md` for every user-visible CLI, config, runtime-state, skill, or behavior change.
- Include migration notes when an existing team/session might need a new command, config field, or restart.
- Keep human operating docs in `README.md` and `docs/`; keep agent design memory in `kb/`.
- Do not put private project names, absolute operator paths, branches, job IDs, tokens, or customer data in reusable docs, tests, or KB examples.
- When workflow rules change, update `skills/start-tmux-team/SKILL.md` and `skills/start-tmux-team/references/invariants.md` together.

## Versioning And Releases

For a release:

1. Bump all version fields to the same value:
   - `pyproject.toml` -> `[project].version`
   - `src/tmux_team/__init__.py` -> `__version__`
   - `.codex-plugin/plugin.json` -> `"version"`
   - `uv.lock` -> editable `tmux-team` package version
2. Update `CHANGELOG.md` for the same version.
3. If repo-local marketplace metadata is present, set its plugin source `ref` to the matching tag, for example `vX.Y.Z`.
4. Validate:

```bash
make integration-test
uv run --with pyyaml python /path/to/validate_plugin.py .
```

5. Open and merge a release PR. `main` is protected; do not push release commits directly to it.
6. Tag the merged `main` commit and push the tag:

```bash
git switch main
git pull --ff-only
git tag vX.Y.Z
git push origin vX.Y.Z
```

Users update with:

```bash
uv tool install --force git+https://github.com/PheelaV/tmux-team.git
# or
pipx install --force git+https://github.com/PheelaV/tmux-team.git

codex plugin marketplace upgrade tmux-team
codex plugin add tmux-team@tmux-team
```

Start a new Codex thread after updating the plugin so Codex reloads the skill.
