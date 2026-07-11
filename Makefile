UV ?= uv
UV_RUN = $(UV) run --with-editable .
UV_RUN_DEV = $(UV) run --with-editable . --extra dev
SKILL_PROVIDERS ?= codex
LIVE_DEMO_ROOT ?= /tmp/tmux-team-live-demo
LIVE_DEMO_SESSION ?= tt-live-demo
LIVE_DEMO_ACP_TUI_BIN ?= $(shell if [ -x "$(CURDIR)/.venv/bin/toad" ]; then printf '%s' "$(CURDIR)/.venv/bin/toad"; else command -v toad 2>/dev/null; fi)
LIVE_DEMO_ACP_AGENT_COMMAND ?= agent --force acp
LIVE_DEMO_ACP_PROVIDER ?= cursor
LIVE_DEMO_ACP_MODEL ?=
LIVE_DEMO_ACP_EFFORT ?=
LIVE_DEMO_ACP_FAST ?=
LIVE_DEMO_ACP_STARTUP_TIMEOUT ?= 180
LIVE_DEMO_ACP_INSTRUCTION_PROFILE ?= compact
LIVE_DEMO_ACP_CURSOR_COMMAND ?= agent --force acp
LIVE_DEMO_ACP_CURSOR_MODEL ?= gpt-5.6-terra[context=272k,reasoning=medium,fast=false]
LIVE_DEMO_ACP_CODEX_COMMAND ?= codex-acp
LIVE_DEMO_ACP_CODEX_MODEL ?= gpt-5.6-terra
LIVE_DEMO_ACP_CLAUDE_COMMAND ?= claude-agent-acp
LIVE_DEMO_ACP_CLAUDE_MODEL ?= us.anthropic.claude-opus-4-8
LIVE_DEMO_ACP_POOL_COMMAND ?= pool acp
LIVE_DEMO_ACP_POOL_MODEL ?=

.PHONY: require-uv require-live-acp install-dev install-skill install-cursor-skill install-pool-skill lint ruff-check format-check format test bootstrap-layout-smoke-test smoke-test congestion-smoke-test integration-test docker-smoke-test docker-congestion-smoke-test docker-test codex-integration-test codex-docker-fs-integration-test live-demo-setup live-demo-bootstrap live-demo-acp-bootstrap live-demo-acp-cursor-bootstrap live-demo-acp-codex-bootstrap live-demo-acp-claude-bootstrap live-demo-acp-pool-bootstrap live-demo-acp-start live-demo-sleep live-demo-resume live-demo-watchdog-now live-demo-verify live-demo-clean

require-uv:
	@command -v "$(UV)" >/dev/null 2>&1 || (echo "tmux-team tests require uv. Install with: brew install uv" >&2; exit 2)

require-live-acp:
	@test "$(TMUX_TEAM_RUN_LIVE_ACP)" = "1" || (echo "Real ACP demo disabled. Set TMUX_TEAM_RUN_LIVE_ACP=1 to accept provider usage." >&2; exit 2)
	@test -n "$(LIVE_DEMO_ACP_MODEL)" || (echo "Set LIVE_DEMO_ACP_MODEL explicitly so real-provider usage is intentional." >&2; exit 2)

install-dev:
	@if command -v uv >/dev/null 2>&1; then \
		uv tool install --force --editable .; \
	elif command -v pipx >/dev/null 2>&1; then \
		pipx install --force --editable .; \
	else \
		echo "tmux-team: install-dev requires uv or pipx." >&2; \
		echo "Install one with: brew install uv  # or: brew install pipx" >&2; \
		exit 2; \
	fi

install-skill:
	python3 scripts/install_skill.py --providers "$(SKILL_PROVIDERS)"

install-cursor-skill:
	$(MAKE) install-skill SKILL_PROVIDERS=cursor

install-pool-skill:
	$(MAKE) install-skill SKILL_PROVIDERS=pool

lint: ruff-check format-check

ruff-check: require-uv
	$(UV_RUN_DEV) ruff check .

format-check: require-uv
	$(UV_RUN_DEV) ruff format --check .

format: require-uv
	$(UV_RUN_DEV) ruff format .

test: require-uv
	$(UV_RUN) python -m unittest discover -s tests

bootstrap-layout-smoke-test: require-uv
	$(UV_RUN) python scripts/bootstrap_layout_smoke.py --session tt-bootstrap-layout-itest --root /tmp/tmux-team-bootstrap-layout-itest --force

smoke-test: require-uv
	$(UV_RUN) python scripts/sandbox_demo.py --spawn-session --cleanup-session --session tt-itest --root /tmp/tmux-team-itest --force

congestion-smoke-test: require-uv
	$(UV_RUN) python scripts/sandbox_demo.py --spawn-session --cleanup-session --session tt-congestion-itest --root /tmp/tmux-team-congestion-itest --force --scenario congestion

integration-test: lint test bootstrap-layout-smoke-test smoke-test congestion-smoke-test

docker-smoke-test:
	docker build -f Dockerfile.sandbox -t tmux-team-sandbox .
	docker run --rm tmux-team-sandbox

docker-congestion-smoke-test:
	docker build -f Dockerfile.sandbox -t tmux-team-sandbox .
	docker run --rm tmux-team-sandbox python scripts/sandbox_demo.py --spawn-session --cleanup-session --session tt-docker-congestion --root /tmp/tmux-team-congestion-sandbox --force --scenario congestion

docker-test: docker-smoke-test docker-congestion-smoke-test

codex-integration-test: require-uv
	$(UV_RUN) python scripts/codex_task_integration.py --root /tmp/tmux-team-codex-itest --force

codex-docker-fs-integration-test: require-uv
	$(UV_RUN) python scripts/codex_task_integration.py --root /tmp/tmux-team-codex-fs-itest --force --verify-in-docker

live-demo-setup: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) setup --force

live-demo-bootstrap: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) bootstrap --session $(LIVE_DEMO_SESSION) --role-yolo --force-config

live-demo-acp-bootstrap: require-uv require-live-acp
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) bootstrap --session $(LIVE_DEMO_SESSION) --agent-runtime acp --acp-tui-bin "$(LIVE_DEMO_ACP_TUI_BIN)" --acp-agent-command "$(LIVE_DEMO_ACP_AGENT_COMMAND)" --acp-provider "$(LIVE_DEMO_ACP_PROVIDER)" --acp-model "$(LIVE_DEMO_ACP_MODEL)" $(if $(LIVE_DEMO_ACP_EFFORT),--acp-effort "$(LIVE_DEMO_ACP_EFFORT)") $(if $(LIVE_DEMO_ACP_FAST),--acp-fast "$(LIVE_DEMO_ACP_FAST)") --acp-startup-timeout "$(LIVE_DEMO_ACP_STARTUP_TIMEOUT)" --instruction-profile "$(LIVE_DEMO_ACP_INSTRUCTION_PROFILE)" --defer-goal --force-config

live-demo-acp-cursor-bootstrap:
	$(MAKE) live-demo-acp-bootstrap LIVE_DEMO_ACP_PROVIDER=cursor LIVE_DEMO_ACP_AGENT_COMMAND='$(LIVE_DEMO_ACP_CURSOR_COMMAND)' LIVE_DEMO_ACP_MODEL='$(LIVE_DEMO_ACP_CURSOR_MODEL)'

live-demo-acp-codex-bootstrap:
	$(MAKE) live-demo-acp-bootstrap LIVE_DEMO_ACP_PROVIDER=codex LIVE_DEMO_ACP_AGENT_COMMAND='$(LIVE_DEMO_ACP_CODEX_COMMAND)' LIVE_DEMO_ACP_MODEL='$(LIVE_DEMO_ACP_CODEX_MODEL)' LIVE_DEMO_ACP_EFFORT=medium LIVE_DEMO_ACP_FAST=false

live-demo-acp-claude-bootstrap:
	$(MAKE) live-demo-acp-bootstrap LIVE_DEMO_ACP_PROVIDER=claude LIVE_DEMO_ACP_AGENT_COMMAND='$(LIVE_DEMO_ACP_CLAUDE_COMMAND)' LIVE_DEMO_ACP_MODEL='$(LIVE_DEMO_ACP_CLAUDE_MODEL)' LIVE_DEMO_ACP_EFFORT=medium

live-demo-acp-pool-bootstrap:
	$(MAKE) live-demo-acp-bootstrap LIVE_DEMO_ACP_PROVIDER=pool LIVE_DEMO_ACP_AGENT_COMMAND='$(LIVE_DEMO_ACP_POOL_COMMAND)' LIVE_DEMO_ACP_MODEL='$(LIVE_DEMO_ACP_POOL_MODEL)'

live-demo-acp-start: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) start-goal

live-demo-sleep: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) sleep

live-demo-resume: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) resume --role-yolo

live-demo-watchdog-now: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) watchdog-now

live-demo-verify: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) verify

live-demo-clean: require-uv
	$(UV_RUN) python scripts/live_demo_scenario.py --root $(LIVE_DEMO_ROOT) clean --session $(LIVE_DEMO_SESSION)
