#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

from tmux_team import bootstrap as bootstrap_mod

MARKER = ".tmux-team-bootstrap-layout-smoke"
DEFAULT_ROLES = ("orchestrator", "implementer", "collector", "trainer")


class SmokeError(RuntimeError):
    pass


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    session = args.session

    try:
        reset_root(root, args.force)
        install_test_skill(root)
        fake_codex = write_fake_codex(root)
        patch_app_server_probes()
        result = bootstrap_mod.bootstrap_team(
            project_root=root / "project",
            config_path=root / "project" / ".tmux-team" / "team.toml",
            runtime_dir=".tmux-team/runtime",
            session=session,
            roles=DEFAULT_ROLES,
            endpoint="ws://127.0.0.1:45555",
            codex_bin=str(fake_codex),
            tmux_bin="tmux",
            goal=None,
            force_config=True,
            start_app_server=True,
            agent_layout="grouped",
            control_window=bootstrap_mod.DEFAULT_CONTROL_WINDOW,
            control_mode="shell",
            agents_window=bootstrap_mod.DEFAULT_AGENTS_WINDOW,
            role_yolo=False,
            role_profile=None,
            dry_run=False,
        )
        config_path = root / "project" / ".tmux-team" / "team.toml"
        verify_layout(session, config_path, result.role_panes)
        verify_scratchpads(config_path)
        run_sleep(config_path, session)
        verify_sleep(session, config_path)
    except SmokeError as exc:
        print(f"bootstrap layout smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )

    print("")
    print("BOOTSTRAP LAYOUT SMOKE OK")
    print(f"root: {root}")
    print(f"session: {session}")
    print(
        "verified: bootstrap creates tt-control, tt-app-server, and one tiled tt-agents window; sleep snapshots TOML and tears down managed windows"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify tmux-team bootstrap creates the default grouped tmux layout.")
    parser.add_argument("--session", default="tt-bootstrap-layout-itest")
    parser.add_argument("--root", default="/tmp/tmux-team-bootstrap-layout-itest")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def reset_root(root: Path, force: bool) -> None:
    if root.exists():
        marker = root / MARKER
        if not marker.exists():
            raise SmokeError(f"refusing to replace unmarked directory: {root}")
        if not force:
            raise SmokeError(f"sandbox exists; rerun with --force: {root}")
        shutil.rmtree(root)

    (root / "project").mkdir(parents=True)
    (root / MARKER).write_text("owned by bootstrap_layout_smoke.py\n", encoding="utf-8")


def install_test_skill(root: Path) -> None:
    codex_home = root / "codex-home"
    target = codex_home / "skills" / "start-tmux-team"
    source = Path(__file__).resolve().parents[1] / "skills" / "start-tmux-team"
    shutil.copytree(source, target)
    os.environ["CODEX_HOME"] = str(codex_home)


def write_fake_codex(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf 'fake codex %s\\n' "$*" >&2
            while :; do
              sleep 60
            done
            """
        ),
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    return fake_codex


def patch_app_server_probes() -> None:
    bootstrap_mod.wait_for_app_server = lambda endpoint, timeout: None
    bootstrap_mod.loaded_threads = lambda endpoint: []
    bootstrap_mod.wait_for_new_loaded_thread = lambda endpoint, previous, timeout: f"thread-{len(previous) + 1}"


def verify_layout(session: str, config_path: Path, role_panes: dict[str, str]) -> None:
    windows = list_windows(session)
    expected_windows = [
        bootstrap_mod.DEFAULT_CONTROL_WINDOW,
        bootstrap_mod.DEFAULT_APP_SERVER_WINDOW,
        bootstrap_mod.DEFAULT_AGENTS_WINDOW,
    ]
    if windows != expected_windows:
        raise SmokeError(f"expected windows {expected_windows}, got {windows}")

    panes = list_panes(session, bootstrap_mod.DEFAULT_AGENTS_WINDOW)
    role_labels = [pane["role"] for pane in panes]
    if role_labels != list(DEFAULT_ROLES):
        raise SmokeError(f"expected agent role labels {list(DEFAULT_ROLES)}, got {role_labels}")
    if any(pane["dead"] != "0" for pane in panes):
        raise SmokeError(f"expected all role panes alive, got {panes}")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config_roles = config.get("roles", {})
    actual_pane_ids = {pane["id"] for pane in panes}
    for role in DEFAULT_ROLES:
        role_config = config_roles.get(role)
        if not isinstance(role_config, dict):
            raise SmokeError(f"missing role in config: {role}")
        pane = str(role_config.get("pane") or "")
        if pane not in actual_pane_ids:
            raise SmokeError(f"config pane for {role} is {pane!r}, expected one of {sorted(actual_pane_ids)}")
        if role_panes.get(role) != pane:
            raise SmokeError(f"bootstrap result pane for {role}={role_panes.get(role)!r}, config={pane!r}")
        if role_config.get("notify_method") != "app-server-turn":
            raise SmokeError(f"{role} notify_method is not app-server-turn")
        if role_config.get("scratchpad") != f".tmux-team/memory/{role}.md":
            raise SmokeError(f"{role} scratchpad path mismatch: {role_config.get('scratchpad')}")


def verify_scratchpads(config_path: Path) -> None:
    project_root = config_path.parent.parent
    for role in DEFAULT_ROLES:
        path = project_root / ".tmux-team" / "memory" / f"{role}.md"
        if not path.exists():
            raise SmokeError(f"missing scratchpad for {role}: {path}")
        text = path.read_text(encoding="utf-8")
        if "## Latest" not in text or "## Boundaries" not in text:
            raise SmokeError(f"scratchpad seed missing required sections for {role}: {path}")


def run_sleep(config_path: Path, session: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tmux_team.cli",
            "--config",
            str(config_path),
            "sleep",
            "--session",
            session,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SmokeError(f"sleep failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    if "snapshot:" not in result.stdout:
        raise SmokeError(f"sleep output did not report a snapshot:\n{result.stdout}")


def verify_sleep(session: str, config_path: Path) -> None:
    windows = list_windows(session)
    if windows != [bootstrap_mod.DEFAULT_CONTROL_WINDOW]:
        raise SmokeError(f"expected only {bootstrap_mod.DEFAULT_CONTROL_WINDOW} after sleep, got {windows}")

    latest = config_path.parent / "runtime" / "sleeps" / "latest.toml"
    if not latest.exists():
        raise SmokeError(f"missing sleep snapshot: {latest}")
    snapshot = tomllib.loads(latest.read_text(encoding="utf-8"))
    if snapshot["tmux"]["session"] != session:
        raise SmokeError(f"sleep snapshot session mismatch: {snapshot['tmux']['session']}")
    if set(snapshot["roles"]) != set(DEFAULT_ROLES):
        raise SmokeError(f"sleep snapshot roles mismatch: {sorted(snapshot['roles'])}")
    if snapshot["roles"]["orchestrator"]["app_server"]["thread_id"] != "thread-1":
        raise SmokeError("sleep snapshot did not preserve orchestrator thread id")


def list_windows(session: str) -> list[str]:
    result = run(["tmux", "list-windows", "-t", session, "-F", "#{window_name}"])
    return [line for line in result.stdout.splitlines() if line]


def list_panes(session: str, window: str) -> list[dict[str, str]]:
    result = run(
        [
            "tmux",
            "list-panes",
            "-t",
            f"{session}:{window}",
            "-F",
            "#{pane_id}\t#{@tmux-team-role}\t#{pane_dead}",
        ]
    )
    panes: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        pane_id, role, dead = line.split("\t", 2)
        panes.append({"id": pane_id, "role": role, "dead": dead})
    return panes


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise SmokeError(f"command failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


if __name__ == "__main__":
    sys.exit(main())
