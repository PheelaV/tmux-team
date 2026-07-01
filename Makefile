CODEX_NPM_PACKAGE ?= @openai/codex
DOCKER_CODEX_HOME ?= $(CURDIR)/.tmux-team/codex-home
UV ?= uv
UV_RUN = $(UV) run --with-editable .
UV_RUN_DEV = $(UV) run --with-editable . --extra dev

.PHONY: require-uv install-dev install-skill lint ruff-check format-check format test bootstrap-layout-smoke-test smoke-test congestion-smoke-test integration-test docker-smoke-test docker-congestion-smoke-test docker-test codex-integration-test codex-docker-fs-integration-test docker-codex-image docker-codex-login docker-codex-login-api-key docker-codex-login-status docker-codex-integration-test

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
	$(UV_RUN) python scripts/bootstrap_layout_smoke.py --session tmux-team-bootstrap-layout-itest --root /tmp/tmux-team-bootstrap-layout-itest --force

smoke-test: require-uv
	$(UV_RUN) python scripts/sandbox_demo.py --spawn-session --session tmux-team-itest --root /tmp/tmux-team-itest --force

congestion-smoke-test: require-uv
	$(UV_RUN) python scripts/sandbox_demo.py --spawn-session --session tmux-team-congestion-itest --root /tmp/tmux-team-congestion-itest --force --scenario congestion

integration-test: lint test bootstrap-layout-smoke-test smoke-test congestion-smoke-test

docker-smoke-test:
	docker build -f Dockerfile.sandbox -t tmux-team-sandbox .
	docker run --rm tmux-team-sandbox

docker-congestion-smoke-test:
	docker build -f Dockerfile.sandbox -t tmux-team-sandbox .
	docker run --rm tmux-team-sandbox python scripts/sandbox_demo.py --spawn-session --session tt-docker-congestion --root /tmp/tmux-team-congestion-sandbox --force --scenario congestion

docker-test: docker-smoke-test docker-congestion-smoke-test

codex-integration-test: require-uv
	$(UV_RUN) python scripts/codex_task_integration.py --root /tmp/tmux-team-codex-itest --force

codex-docker-fs-integration-test: require-uv
	$(UV_RUN) python scripts/codex_task_integration.py --root /tmp/tmux-team-codex-fs-itest --force --verify-in-docker

docker-codex-image:
	docker build -f Dockerfile.codex-integration -t tmux-team-codex-integration --build-arg CODEX_NPM_PACKAGE="$(CODEX_NPM_PACKAGE)" .

docker-codex-login: docker-codex-image
	mkdir -p "$(DOCKER_CODEX_HOME)"
	docker run --rm -it -e CODEX_HOME=/codex-home -v "$(DOCKER_CODEX_HOME):/codex-home" tmux-team-codex-integration codex login --device-auth

docker-codex-login-api-key: docker-codex-image
	@test -n "$$OPENAI_API_KEY" || (echo "Set OPENAI_API_KEY before running API-key login." >&2; exit 2)
	mkdir -p "$(DOCKER_CODEX_HOME)"
	printf '%s' "$$OPENAI_API_KEY" | docker run --rm -i -e CODEX_HOME=/codex-home -v "$(DOCKER_CODEX_HOME):/codex-home" tmux-team-codex-integration codex login --with-api-key

docker-codex-login-status: docker-codex-image
	mkdir -p "$(DOCKER_CODEX_HOME)"
	docker run --rm -e CODEX_HOME=/codex-home -v "$(DOCKER_CODEX_HOME):/codex-home" tmux-team-codex-integration codex login status

docker-codex-integration-test: docker-codex-image
	@mkdir -p "$(DOCKER_CODEX_HOME)"
	@test -n "$$OPENAI_API_KEY$$CODEX_API_KEY" || test -f "$(DOCKER_CODEX_HOME)/auth.json" || (echo "Run 'make docker-codex-login' once, or set OPENAI_API_KEY/CODEX_API_KEY." >&2; exit 2)
	docker run --rm -e TMUX_TEAM_RUN_CODEX=1 -e OPENAI_API_KEY -e CODEX_API_KEY -e CODEX_HOME=/codex-home -v "$(DOCKER_CODEX_HOME):/codex-home" tmux-team-codex-integration
