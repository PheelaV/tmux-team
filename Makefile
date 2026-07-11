UV ?= uv
UV_RUN = $(UV) run --with-editable .
UV_RUN_DEV = $(UV) run --with-editable . --extra dev
LIVE_DEMO_ROOT ?= /tmp/tmux-team-live-demo
LIVE_DEMO_SESSION ?= tt-live-demo

.PHONY: require-uv install-dev install-skill install-cursor-skill lint ruff-check format-check format test bootstrap-layout-smoke-test smoke-test congestion-smoke-test integration-test docker-smoke-test docker-congestion-smoke-test docker-test codex-integration-test codex-docker-fs-integration-test live-demo-setup live-demo-bootstrap live-demo-sleep live-demo-resume live-demo-watchdog-now live-demo-verify live-demo-clean

require-uv:
	@command -v "$(UV)" >/dev/null 2>&1 || (echo "tmux-team tests require uv. Install with: brew install uv" >&2; exit 2)

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
	mkdir -p "$${CODEX_HOME:-$$HOME/.codex}/skills/start-tmux-team/agents"
	mkdir -p "$${CODEX_HOME:-$$HOME/.codex}/skills/start-tmux-team/references"
	cp skills/start-tmux-team/SKILL.md "$${CODEX_HOME:-$$HOME/.codex}/skills/start-tmux-team/SKILL.md"
	cp skills/start-tmux-team/agents/openai.yaml "$${CODEX_HOME:-$$HOME/.codex}/skills/start-tmux-team/agents/openai.yaml"
	cp skills/start-tmux-team/references/invariants.md "$${CODEX_HOME:-$$HOME/.codex}/skills/start-tmux-team/references/invariants.md"
	cp skills/start-tmux-team/references/team-shapes.md "$${CODEX_HOME:-$$HOME/.codex}/skills/start-tmux-team/references/team-shapes.md"

install-cursor-skill:
	mkdir -p "$${CURSOR_HOME:-$$HOME/.cursor}/skills/start-tmux-team/references"
	cp skills/start-tmux-team/SKILL.md "$${CURSOR_HOME:-$$HOME/.cursor}/skills/start-tmux-team/SKILL.md"
	cp skills/start-tmux-team/references/invariants.md "$${CURSOR_HOME:-$$HOME/.cursor}/skills/start-tmux-team/references/invariants.md"
	cp skills/start-tmux-team/references/team-shapes.md "$${CURSOR_HOME:-$$HOME/.cursor}/skills/start-tmux-team/references/team-shapes.md"

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
