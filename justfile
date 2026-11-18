uv := quote(env("UV", "uv"))
uv_run := uv + " run --with-editable ."
uv_dev := uv_run + " --extra dev"
demo_root := quote(env("LIVE_DEMO_ROOT", "/tmp/tmux-team-live-demo"))
demo_session := quote(env("LIVE_DEMO_SESSION", "tt-live-demo"))
demo_provider := env("LIVE_DEMO_ACP_PROVIDER", "cursor")
demo_model := env("LIVE_DEMO_ACP_MODEL", "")

[private]
default:
    @{{quote(just_executable())}} --list

[private]
require-uv:
    @command -v {{uv}} >/dev/null 2>&1 || { echo "tmux-team tests require uv. Install it with brew install uv." >&2; exit 2; }

install-dev:
    #!/bin/sh
    set -eu
    if command -v {{uv}} >/dev/null 2>&1; then
        exec {{uv}} tool install --force --editable .
    elif command -v pipx >/dev/null 2>&1; then
        exec pipx install --force --editable .
    fi
    echo "tmux-team: install-dev requires uv or pipx." >&2
    exit 2

install-skill $providers="codex":
    python3 scripts/install_skill.py --providers "$providers"

install-cursor-skill: (install-skill "cursor")

lint: ruff-check format-check

ruff-check: require-uv
    {{uv_dev}} ruff check .

format-check: require-uv
    {{uv_dev}} ruff format --check .

format: require-uv
    {{uv_dev}} ruff format .

test: require-uv
    {{uv_run}} python -m unittest discover -s tests

bootstrap-layout-smoke-test: require-uv
    {{uv_run}} python scripts/bootstrap_layout_smoke.py --session tt-bootstrap-layout-itest --root /tmp/tmux-team-bootstrap-layout-itest --force

smoke-test: require-uv
    {{uv_run}} python scripts/sandbox_demo.py --spawn-session --cleanup-session --session tt-itest --root /tmp/tmux-team-itest --force

congestion-smoke-test: require-uv
    {{uv_run}} python scripts/sandbox_demo.py --spawn-session --cleanup-session --session tt-congestion-itest --root /tmp/tmux-team-congestion-itest --force --scenario congestion

integration-test: lint test bootstrap-layout-smoke-test smoke-test congestion-smoke-test

docker-smoke-test:
    docker build -f Dockerfile.sandbox -t tmux-team-sandbox .
    docker run --rm tmux-team-sandbox

docker-congestion-smoke-test:
    docker build -f Dockerfile.sandbox -t tmux-team-sandbox .
    docker run --rm tmux-team-sandbox python scripts/sandbox_demo.py --spawn-session --cleanup-session --session tt-congestion-itest --root /tmp/tmux-team-congestion-sandbox --force --scenario congestion

docker-test: docker-smoke-test docker-congestion-smoke-test

codex-integration-test: require-uv
    {{uv_run}} python scripts/codex_task_integration.py --root /tmp/tmux-team-codex-itest --force

codex-docker-fs-integration-test: require-uv
    {{uv_run}} python scripts/codex_task_integration.py --root /tmp/tmux-team-codex-fs-itest --force --verify-in-docker

live-demo-setup: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} setup --force

live-demo-bootstrap: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} bootstrap --session {{demo_session}} --role-yolo --force-config

# Start a deferred ACP demo with an adapter-advertised model.
live-demo-acp-bootstrap $provider=demo_provider $model=demo_model: require-uv
    #!/bin/sh
    set -eu
    if [ "${TMUX_TEAM_RUN_LIVE_ACP:-0}" != "1" ]; then
        echo "Real ACP demo disabled. Set TMUX_TEAM_RUN_LIVE_ACP=1 to accept provider usage." >&2
        exit 2
    fi
    if [ -z "$model" ]; then
        echo "Pass an adapter-advertised model to live-demo-acp-bootstrap." >&2
        exit 2
    fi

    effort="${LIVE_DEMO_ACP_EFFORT:-}"
    fast="${LIVE_DEMO_ACP_FAST:-}"
    case "$provider" in
        cursor)
            command="agent --force acp"
            ;;
        codex)
            command=codex-acp
            effort="${effort:-medium}"
            fast="${fast:-false}"
            ;;
        claude)
            command=claude-agent-acp
            effort="${effort:-medium}"
            ;;
        *)
            echo "Unsupported ACP provider '$provider'. Choose cursor, codex, or claude." >&2
            exit 2
            ;;
    esac

    tui_bin=toad
    if [ -x .venv/bin/toad ]; then
        tui_bin="$PWD/.venv/bin/toad"
    fi
    set -- --root {{demo_root}} bootstrap --session {{demo_session}} --agent-runtime acp \
        --acp-provider "$provider" --acp-agent-command "${LIVE_DEMO_ACP_AGENT_COMMAND:-$command}" \
        --acp-tui-bin "${LIVE_DEMO_ACP_TUI_BIN:-$tui_bin}" --acp-model "$model" \
        --acp-startup-timeout "${LIVE_DEMO_ACP_STARTUP_TIMEOUT:-180}" \
        --instruction-profile "${LIVE_DEMO_ACP_INSTRUCTION_PROFILE:-compact}" --defer-goal --force-config
    if [ -n "$effort" ]; then
        set -- "$@" --acp-effort "$effort"
    fi
    if [ -n "$fast" ]; then
        set -- "$@" --acp-fast "$fast"
    fi
    exec {{uv_run}} python scripts/live_demo_scenario.py "$@"

live-demo-acp-start: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} start-goal

live-demo-sleep: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} sleep

live-demo-resume: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} resume

live-demo-watchdog-now: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} watchdog-now

live-demo-verify: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} verify

live-demo-clean: require-uv
    {{uv_run}} python scripts/live_demo_scenario.py --root {{demo_root}} clean --session {{demo_session}}
