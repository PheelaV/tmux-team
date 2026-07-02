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

1. Bump both versions to the same value:
   - `pyproject.toml` -> `[project].version`
   - `.codex-plugin/plugin.json` -> `"version"`
2. Set `.agents/plugins/marketplace.json` plugin source `ref` to the matching tag, for example `v0.1.1`.
3. Validate:

```bash
make integration-test
uv run --with pyyaml python /path/to/validate_plugin.py .
```

4. Commit, tag, and push:

```bash
git commit -am "Release v0.1.1"
git tag v0.1.1
git push
git push origin v0.1.1
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
