#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
MARKER = ".tmux-team-codex-integration"


@dataclass(frozen=True)
class Paths:
    root: Path
    project: Path
    config: Path
    runtime: Path
    codex_last_message: Path
    codex_stdout: Path
    codex_stderr: Path


def main() -> int:
    args = parse_args()
    if os.environ.get("TMUX_TEAM_RUN_CODEX") != "1":
        print("SKIPPED real Codex integration: set TMUX_TEAM_RUN_CODEX=1 to run it.")
        return 0

    codex = shutil.which(args.codex_bin)
    if codex is None:
        print(f"real Codex integration failed: codex binary not found: {args.codex_bin}", file=sys.stderr)
        return 1

    try:
        paths = reset_sandbox(Path(args.root).expanduser().resolve(), args.force)
        write_project(paths)
        write_config(paths)
        queue_task(paths)
        run_codex(codex, paths, args.model, args.timeout)
        verify(paths, verify_in_docker=args.verify_in_docker, docker_image=args.docker_image)
    except IntegrationError as exc:
        print(f"real Codex integration failed: {exc}", file=sys.stderr)
        return 1

    print("")
    print("REAL CODEX INTEGRATION OK")
    print(f"root: {paths.root}")
    print(f"project: {paths.project}")
    print(f"runtime: {paths.runtime}")
    print("verified: Codex claimed a tmux-team inbox item, fixed code, completed the item, and tests pass")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an opt-in real Codex task against tmux-team.")
    parser.add_argument("--root", default="/tmp/tmux-team-codex-itest", help="sandbox root")
    parser.add_argument("--force", action="store_true", help="replace an existing marked sandbox root")
    parser.add_argument("--codex-bin", default="codex", help="codex executable")
    parser.add_argument("--model", default=None, help="optional Codex model override")
    parser.add_argument("--timeout", type=float, default=240.0, help="codex exec timeout in seconds")
    parser.add_argument(
        "--verify-in-docker",
        action="store_true",
        help="verify the final task in Docker with the sandbox project bind-mounted",
    )
    parser.add_argument("--docker-image", default="python:3.12-slim", help="Docker image for bind-mounted verification")
    return parser.parse_args()


def reset_sandbox(root: Path, force: bool) -> Paths:
    if root.exists():
        marker = root / MARKER
        if not marker.exists():
            raise IntegrationError(f"refusing to replace unmarked directory: {root}")
        if not force:
            raise IntegrationError(f"sandbox already exists; rerun with --force to replace: {root}")
        shutil.rmtree(root)

    project = root / "project"
    config = project / ".tmux-team" / "team.toml"
    runtime = project / ".tmux-team" / "runtime"
    for path in (project, config.parent, runtime):
        path.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("owned by tmux-team codex_task_integration.py\n", encoding="utf-8")
    return Paths(
        root=root,
        project=project,
        config=config,
        runtime=runtime,
        codex_last_message=root / "codex-last-message.md",
        codex_stdout=root / "codex-stdout.jsonl",
        codex_stderr=root / "codex-stderr.log",
    )


def write_project(paths: Paths) -> None:
    (paths.project / "calculator.py").write_text(
        '''"""Tiny intentionally broken calculator used by the real Codex integration."""


def add(a: int, b: int) -> int:
    return a - b
''',
        encoding="utf-8",
    )
    (paths.project / "test_calculator.py").write_text(
        """import unittest

from calculator import add


class CalculatorTests(unittest.TestCase):
    def test_adds_two_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )


def write_config(paths: Paths) -> None:
    paths.config.write_text(
        """[team]
name = "codex-integration"
runtime_dir = ".tmux-team/runtime"

[roles.orchestrator]
mode = "managed"
state = "active"
can_edit = false

[roles.implementer]
mode = "managed"
state = "active"
can_edit = true
""",
        encoding="utf-8",
    )


def queue_task(paths: Paths) -> None:
    body = paths.root / "implementer-task.md"
    body.write_text(
        """Fix the failing calculator unit test with the smallest production-code change.

Acceptance:
- Claim and acknowledge this tmux-team message.
- Run `python -B -m unittest -v test_calculator` before and after the fix.
- Fix `calculator.py`.
- Complete this tmux-team message with status `fixed`.
""",
        encoding="utf-8",
    )
    command = tt(paths) + [
        "send",
        "--to",
        "implementer",
        "--from",
        "orchestrator",
        "--summary",
        "fix calculator add regression",
        "--body-file",
        str(body),
        "--no-notify",
    ]
    run(command, cwd=paths.project, check=True)


def run_codex(codex: str, paths: Paths, model: str | None, timeout: float) -> None:
    command = [
        codex,
        "exec",
        "--cd",
        str(paths.project),
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        "--output-last-message",
        str(paths.codex_last_message),
    ]
    if model:
        command.extend(["--model", model])
    command.append("-")

    prompt = f"""You are the implementer role in a tmux-team integration test.

Use this exact tmux-team command prefix:

PYTHONPATH={shlex.quote(str(SRC_DIR))} {shlex.quote(sys.executable)} -m tmux_team.cli --config .tmux-team/team.toml

Required steps:
1. Claim the next implementer message.
2. Acknowledge the claimed message.
3. Read the task body from the claim output.
4. Run `{shlex.quote(sys.executable)} -B -m unittest -v test_calculator` and observe the failure.
5. Fix `calculator.py` with the smallest production-code change.
6. Run `{shlex.quote(sys.executable)} -B -m unittest -v test_calculator` and verify it passes.
7. Complete the claimed message with `--status fixed --summary "calculator.add fixed; unittest passes"`.

Do not ask for clarification. Do not edit files outside the current directory.
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        command,
        cwd=paths.project,
        env=env,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    paths.codex_stdout.write_text(result.stdout, encoding="utf-8")
    paths.codex_stderr.write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise IntegrationError(
            f"codex exec exited {result.returncode}\n"
            f"stdout log: {paths.codex_stdout}\n"
            f"stderr log: {paths.codex_stderr}\n"
            f"stderr tail:\n{result.stderr[-4000:]}"
        )


def verify(paths: Paths, *, verify_in_docker: bool, docker_image: str) -> None:
    if verify_in_docker:
        result = run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-v",
                f"{paths.project}:/workspace",
                "-w",
                "/workspace",
                docker_image,
                "python",
                "-B",
                "-m",
                "unittest",
                "-v",
                "test_calculator",
            ],
            check=False,
        )
    else:
        result = run([sys.executable, "-B", "-m", "unittest", "-v", "test_calculator"], cwd=paths.project, check=False)
    if result.returncode != 0:
        raise IntegrationError(f"final unittest failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    conn = sqlite3.connect(paths.runtime / "team.sqlite")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT sender, recipient, state, result_status, result_summary
        FROM messages
        WHERE sender = 'orchestrator' AND recipient = 'implementer'
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()

    if row is None:
        raise IntegrationError("expected implementer message was not recorded")
    if row["state"] != "completed":
        raise IntegrationError(f"expected message completed, got {dict(row)}")
    if row["result_status"] != "fixed":
        raise IntegrationError(f"expected result_status fixed, got {dict(row)}")

    text = (paths.project / "calculator.py").read_text(encoding="utf-8")
    if "return a + b" not in text:
        raise IntegrationError("calculator.py was not fixed")


def tt(paths: Paths) -> list[str]:
    return [sys.executable, "-m", "tmux_team.cli", "--config", str(paths.config)]


def run(command: list[str], cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise IntegrationError(
            f"command failed: {' '.join(shlex.quote(part) for part in command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class IntegrationError(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
