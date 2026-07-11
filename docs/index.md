# Human Documentation

Start here for operator-facing tmux-team docs. The `kb/` directory is agent-facing project memory and design history; do not treat every old KB item as current user documentation.

## Operator Flow

1. Install the CLI: `uv tool install git+https://github.com/PheelaV/tmux-team.git`, then install the plugin with `codex plugin marketplace add PheelaV/tmux-team --ref main` and `codex plugin add tmux-team@tmux-team`.
2. Start from a tmux control pane: `tmux new-session -s tt-<project> -c <project-root>`, then launch Codex.
3. Bootstrap the team through the `start-tmux-team` skill, or run `tmux-team bootstrap --project-root . --goal "..."`
4. Operate from durable state first: `status`, `dashboard`, `send`, `inbox`, `todo`, `obligation`, `watchdog`, `milestone`, `pane`, `role`, `stable`, `sleep`, and `resume`. See [CLI Reference](cli-reference.md) for the command map.
5. Stop managed panes with `tmux-team sleep`; restore them with `tmux-team resume`.
6. Test locally with `make integration-test`; use Docker and real-Codex tests only when needed.

## Core Docs

- [Invariants](invariants.md): product constraints that bootstrap, delivery, and lifecycle changes must preserve.
- [CLI Reference](cli-reference.md): structured command map for operators.
- [External ACP TUI Runtime](acp-runtime.md): Toad/provider setup, permissions, control-socket delivery, and runtime handoffs.
- [Receiving and Hooks](receiving-and-hooks.md): inbox flow, wake methods, and the current extension hook surface.
- [Extensions](extensions.md): project-local executable hook contract for humans.
- [Live Demo](live-demo.md): repeatable public-snapshot demo scenario for real Codex teams.
- [Testing](testing.md): local, Docker, fake-agent, and real-Codex test layers.

## Design Notes

- [Experimental Surfaces](experimental-surfaces.md): MCP and permission-hardening direction.
