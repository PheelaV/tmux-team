#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
MARKER = ".tmux-team-sandbox"


@dataclass(frozen=True)
class Paths:
    root: Path
    project: Path
    config: Path
    runtime: Path
    steps: Path


def main() -> int:
    args = parse_args()
    cleanup_session = False
    try:
        paths = reset_sandbox(Path(args.root).expanduser().resolve(), args.force)
        write_project(paths, args.scenario)
        write_config(paths, args.session)

        if args.spawn_session:
            cleanup_session = ensure_session(args.session, paths.root, replace=args.cleanup_session)
        else:
            require_session(args.session)

        ensure_layout(args.session, paths)
        run_flow(args.session, paths, args.timeout, args.scenario)
        verify(paths, args.scenario)
    except DemoError as exc:
        print(f"sandbox demo failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_session:
            kill_session(args.session)

    print("")
    print("SANDBOX DEMO OK")
    print(f"root: {paths.root}")
    print(f"project: {paths.project}")
    print(f"runtime: {paths.runtime}")
    print("verified: failing test was routed to implementer, fixed, completed, and recorded in SQLite")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a controlled tmux-team fake-agent smoke test.")
    parser.add_argument("--session", default="tt-sandbox", help="tmux session name")
    parser.add_argument("--root", default="/tmp/tmux-team-sandbox", help="sandbox root")
    parser.add_argument("--spawn-session", action="store_true", help="create the tmux session if needed")
    parser.add_argument("--cleanup-session", action="store_true", help="kill the spawned tmux session before exit")
    parser.add_argument("--force", action="store_true", help="replace an existing marked sandbox root")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for each pane step")
    parser.add_argument("--scenario", choices=("basic", "congestion"), default="basic", help="smoke scenario")
    return parser.parse_args()


def reset_sandbox(root: Path, force: bool) -> Paths:
    if root.exists():
        marker = root / MARKER
        if not marker.exists():
            raise DemoError(f"refusing to replace unmarked directory: {root}")
        if not force:
            raise DemoError(f"sandbox already exists; rerun with --force to replace: {root}")
        shutil.rmtree(root)

    project = root / "project"
    runtime = root / "runtime"
    config = root / ".tmux-team" / "team.toml"
    steps = root / "steps"
    for path in (project, runtime, config.parent, steps):
        path.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("owned by tmux-team sandbox_demo.py\n", encoding="utf-8")
    return Paths(root=root, project=project, config=config, runtime=runtime, steps=steps)


def write_project(paths: Paths, scenario: str) -> None:
    if scenario == "congestion":
        write_congestion_project(paths)
        return

    (paths.project / "calculator.py").write_text(
        '''"""Tiny intentionally broken calculator used by the tmux-team sandbox demo."""


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
    (paths.project / "README.md").write_text(
        "# Sandbox Project\n\nThe fake collector finds a failing calculator test. The fake implementer fixes it.\n",
        encoding="utf-8",
    )


def write_congestion_project(paths: Paths) -> None:
    (paths.project / "calculator.py").write_text(
        '''"""Tiny intentionally broken calculator used by the tmux-team congestion smoke test."""


def add(a: int, b: int) -> int:
    return a - b


def multiply(a: int, b: int) -> int:
    return a + b
''',
        encoding="utf-8",
    )
    (paths.project / "test_calculator.py").write_text(
        """import unittest

from calculator import add, multiply


class CalculatorTests(unittest.TestCase):
    def test_adds_two_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_multiplies_two_numbers(self) -> None:
        self.assertEqual(multiply(2, 3), 6)


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )
    (paths.project / "README.md").write_text(
        "# Congestion Sandbox Project\n\nTwo calculator regressions are routed through a congested queue.\n",
        encoding="utf-8",
    )


def write_config(paths: Paths, session: str) -> None:
    paths.config.write_text(
        f"""[team]
name = "sandbox"
runtime_dir = "{paths.runtime}"

[roles.orchestrator]
mode = "human_visible"
state = "active"
pane = "{session}:orchestrator.0"
can_edit = false
notify_method = "display-message"

[roles.collector]
mode = "human_visible"
state = "active"
pane = "{session}:collector.0"
can_edit = false
notify_method = "display-message"

[roles.implementer]
mode = "human_visible"
state = "active"
pane = "{session}:implementer.0"
can_edit = true
notify_method = "display-message"
""",
        encoding="utf-8",
    )


def ensure_session(session: str, cwd: Path, *, replace: bool) -> bool:
    if tmux_ok("has-session", "-t", session):
        if not replace:
            return False
        kill_session(session)
    run(["tmux", "new-session", "-d", "-s", session, "-c", str(cwd)], check=True)
    return True


def kill_session(session: str) -> None:
    run(["tmux", "kill-session", "-t", session], check=False)


def require_session(session: str) -> None:
    if tmux_ok("has-session", "-t", session):
        return
    raise DemoError(
        "tmux session is not running. Start it in another terminal with:\n"
        f"  tmux new-session -s {session} -c {shlex.quote(str(REPO_ROOT))}\n"
        "Then rerun this script."
    )


def ensure_layout(session: str, paths: Paths) -> None:
    windows = list_windows(session)
    if "orchestrator" not in windows:
        if windows:
            first_index = sorted(windows.values())[0]
            run(["tmux", "rename-window", "-t", f"{session}:{first_index}", "orchestrator"], check=True)
        else:
            run(["tmux", "new-window", "-t", session, "-n", "orchestrator", "-c", str(paths.project)], check=True)

    for role in ("collector", "implementer"):
        if role not in list_windows(session):
            run(["tmux", "new-window", "-t", session, "-n", role, "-c", str(paths.project)], check=True)

    for role in ("orchestrator", "collector", "implementer"):
        send_keys(session, role, f"cd {shlex.quote(str(paths.project))}")
        send_keys(session, role, "clear")
        send_keys(session, role, f"printf 'tmux-team sandbox role: {role}\\n'")


def list_windows(session: str) -> dict[str, str]:
    result = run(["tmux", "list-windows", "-t", session, "-F", "#{window_index}\t#{window_name}"], check=True)
    windows: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        index, name = line.split("\t", 1)
        windows[name] = index
    return windows


def run_flow(session: str, paths: Paths, timeout: float, scenario: str) -> None:
    if scenario == "congestion":
        run_congestion_flow(session, paths, timeout)
        return

    print("running collector step")
    run_step(session, "collector", paths, timeout, collector_script(paths))
    assert_pending_view(paths, role="orchestrator", expected_summary="calculator add regression")

    print("running orchestrator step")
    run_step(session, "orchestrator", paths, timeout, orchestrator_script(paths))

    print("running implementer step")
    run_step(session, "implementer", paths, timeout, implementer_script(paths))


def assert_pending_view(paths: Paths, *, role: str, expected_summary: str) -> None:
    result = run(
        tt(paths) + ["inbox", "list", "--role", role, "--state", "pending"],
        cwd=paths.project,
        check=True,
    )
    if "state=notified" not in result.stdout or f"summary={expected_summary}" not in result.stdout:
        raise DemoError(
            "pending inbox view did not include successfully notified work\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def run_congestion_flow(session: str, paths: Paths, timeout: float) -> None:
    print("seeding congestion")
    seed_congestion(paths)

    print("checking copy-mode notification guard")
    exercise_copy_mode_guard(session, paths)

    print("running collector congestion step")
    run_step(session, "collector", paths, timeout, collector_congestion_script(paths))

    print("running orchestrator congestion step")
    run_step(session, "orchestrator", paths, timeout, orchestrator_congestion_script(paths))

    print("running implementer congestion step")
    run_step(session, "implementer", paths, timeout, implementer_congestion_script(paths))


def seed_congestion(paths: Paths) -> None:
    run(tt(paths) + ["role", "pause", "implementer"], cwd=paths.project, check=True)
    blocked = paths.root / "blocked-task.md"
    blocked.write_text(
        "This normal-priority task should be recorded but blocked while implementer is paused.\n",
        encoding="utf-8",
    )
    blocked_result = run(
        tt(paths)
        + [
            "send",
            "--to",
            "implementer",
            "--from",
            "operator",
            "--summary",
            "operator blocked background task",
            "--body-file",
            str(blocked),
            "--no-notify",
        ],
        cwd=paths.project,
        check=False,
    )
    if blocked_result.returncode != 2:
        raise DemoError(
            f"expected paused role send to exit 2\nstdout:\n{blocked_result.stdout}\nstderr:\n{blocked_result.stderr}"
        )

    urgent = paths.root / "urgent-multiply.md"
    urgent.write_text(
        """Fix the multiply regression first.

Acceptance:
- Claim and acknowledge this urgent message.
- Change `multiply(2, 3)` so it returns 6.
- Complete with status `fixed`.
""",
        encoding="utf-8",
    )
    run(
        tt(paths)
        + [
            "send",
            "--to",
            "implementer",
            "--from",
            "operator",
            "--priority",
            "urgent",
            "--summary",
            "urgent multiply regression",
            "--body-file",
            str(urgent),
        ],
        cwd=paths.project,
        check=True,
    )
    run(tt(paths) + ["role", "resume", "implementer"], cwd=paths.project, check=True)

    cleanup = paths.root / "cleanup-readme.md"
    cleanup.write_text("Low-priority cleanup. Defer if higher-priority repair work exists.\n", encoding="utf-8")
    run(
        tt(paths)
        + [
            "send",
            "--to",
            "implementer",
            "--from",
            "operator",
            "--priority",
            "low",
            "--summary",
            "cleanup README",
            "--body-file",
            str(cleanup),
        ],
        cwd=paths.project,
        check=True,
    )


def exercise_copy_mode_guard(session: str, paths: Paths) -> None:
    target = f"{session}:implementer.0"
    run(["tmux", "copy-mode", "-t", target], check=True)
    try:
        result = run(tt(paths) + ["notify", "implementer", "--method", "send-keys"], cwd=paths.project, check=False)
        if result.returncode == 0:
            raise DemoError("expected send-keys notification to defer while implementer pane is in copy mode")
        if "notify_deferred: pane is in tmux copy/mode" not in result.stderr:
            raise DemoError(
                f"expected copy-mode notification deferral\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    finally:
        run(["tmux", "send-keys", "-t", target, "-X", "cancel"], check=False)


def collector_script(paths: Paths) -> str:
    return f"""
cd {q(paths.project)}
echo '[collector] running baseline unittest; failure is expected'
set +e
{q(sys.executable)} -B -m unittest -v test_calculator > collector_unittest.log 2>&1
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo '[collector] expected the baseline test to fail'
  exit 12
fi
cat > collector_report.md <<'REPORT'
The sandbox calculator test is failing.

Expected: add(2, 3) == 5
Observed: add(2, 3) returns the wrong value.

Please route to implementer for a minimal production-code fix.
REPORT
printf '\\n## unittest output\\n\\n```text\\n' >> collector_report.md
cat collector_unittest.log >> collector_report.md
printf '\\n```\\n' >> collector_report.md
"${{TT[@]}}" send --to orchestrator --from collector --summary 'calculator add regression' --body-file collector_report.md
"""


def collector_congestion_script(paths: Paths) -> str:
    return f"""
cd {q(paths.project)}
echo '[collector] running baseline unittest; two failures are expected'
set +e
{q(sys.executable)} -B -m unittest -v test_calculator > collector_unittest.log 2>&1
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo '[collector] expected the baseline test to fail'
  exit 12
fi
cat > source_note.md <<'NOTE'
Source health note: calculator tests are present and deterministic.
NOTE
"${{TT[@]}}" send --to orchestrator --from collector --priority low --summary 'collector source note' --body-file source_note.md
cat > add_report.md <<'REPORT'
The sandbox calculator add test is failing.

Expected: add(2, 3) == 5
Observed: add(2, 3) returns the wrong value.

Please route to implementer after higher-priority operator work is respected.
REPORT
printf '\\n## unittest output\\n\\n```text\\n' >> add_report.md
cat collector_unittest.log >> add_report.md
printf '\\n```\\n' >> add_report.md
"${{TT[@]}}" send --to orchestrator --from collector --priority high --summary 'collector add regression' --body-file add_report.md
"""


def orchestrator_script(paths: Paths) -> str:
    return f"""
cd {q(paths.project)}
echo '[orchestrator] claiming collector report'
"${{TT[@]}}" inbox next --role orchestrator > orchestrator_claim.txt
msg_id="$(awk -F': ' '/^id:/ {{ print $2; exit }}' orchestrator_claim.txt)"
test -n "$msg_id"
"${{TT[@]}}" inbox ack "$msg_id" --role orchestrator
cat > implementer_task.md <<'TASK'
Fix the failing sandbox calculator test with the smallest production-code change.

Acceptance:
- `python -m unittest -v test_calculator` passes.
- Complete this message with a short evidence summary.
TASK
"${{TT[@]}}" send --to implementer --from orchestrator --summary 'fix calculator add regression' --body-file implementer_task.md
"${{TT[@]}}" inbox complete "$msg_id" --role orchestrator --status routed --summary 'routed failing test to implementer'
"""


def orchestrator_congestion_script(paths: Paths) -> str:
    return f"""
cd {q(paths.project)}
echo '[orchestrator] draining two collector messages by priority'
"${{TT[@]}}" inbox next --role orchestrator > orchestrator_claim_1.txt
msg_id="$(awk -F': ' '/^id:/ {{ print $2; exit }}' orchestrator_claim_1.txt)"
summary="$(awk -F': ' '/^summary:/ {{ print $2; exit }}' orchestrator_claim_1.txt)"
test "$summary" = 'collector add regression'
"${{TT[@]}}" inbox ack "$msg_id" --role orchestrator
cat > implementer_add_task.md <<'TASK'
Fix the add regression after any urgent operator work.

Acceptance:
- `add(2, 3)` returns 5.
- Complete this message with status `fixed`.
TASK
"${{TT[@]}}" send --to implementer --from orchestrator --priority high --summary 'fix add regression' --body-file implementer_add_task.md
"${{TT[@]}}" inbox complete "$msg_id" --role orchestrator --status routed --summary 'routed add regression to implementer'

"${{TT[@]}}" inbox next --role orchestrator > orchestrator_claim_2.txt
msg_id="$(awk -F': ' '/^id:/ {{ print $2; exit }}' orchestrator_claim_2.txt)"
summary="$(awk -F': ' '/^summary:/ {{ print $2; exit }}' orchestrator_claim_2.txt)"
test "$summary" = 'collector source note'
"${{TT[@]}}" inbox ack "$msg_id" --role orchestrator
"${{TT[@]}}" inbox complete "$msg_id" --role orchestrator --status noted --summary 'source note recorded'
"""


def implementer_script(paths: Paths) -> str:
    return f"""
cd {q(paths.project)}
echo '[implementer] claiming routed task'
"${{TT[@]}}" inbox next --role implementer > implementer_claim.txt
msg_id="$(awk -F': ' '/^id:/ {{ print $2; exit }}' implementer_claim.txt)"
test -n "$msg_id"
"${{TT[@]}}" inbox ack "$msg_id" --role implementer
"${{TT[@]}}" todo add --role implementer --message "$msg_id" 'Patch add implementation' > implementer_todo.txt
todo_id="$(awk '{{ for (i = 1; i <= NF; i++) if ($i ~ /^todo_/) {{ print $i; exit }} }}' implementer_todo.txt)"
test -n "$todo_id"
set +e
{q(sys.executable)} -B -m unittest -v test_calculator > implementer_before.log 2>&1
before_status=$?
set -e
if [ "$before_status" -eq 0 ]; then
  echo '[implementer] expected test to fail before fix'
  exit 13
fi
{q(sys.executable)} - <<'PY'
from pathlib import Path

path = Path("calculator.py")
text = path.read_text(encoding="utf-8")
old = "return a - b"
if old not in text:
    raise SystemExit("expected buggy implementation was not found")
path.write_text(text.replace(old, "return a + b"), encoding="utf-8")
PY
{q(sys.executable)} -B -m unittest -v test_calculator > implementer_after.log 2>&1
"${{TT[@]}}" todo done --role implementer "$todo_id"
"${{TT[@]}}" inbox complete "$msg_id" --role implementer --status fixed --summary 'calculator.add fixed; unittest passes'
"""


def implementer_congestion_script(paths: Paths) -> str:
    return f"""
cd {q(paths.project)}
echo '[implementer] draining congested inbox by priority'
processed=0
while true; do
  idx=$((processed + 1))
  set +e
  "${{TT[@]}}" inbox next --role implementer > "implementer_claim_${{idx}}.txt"
  claim_status=$?
  set -e
  if [ "$claim_status" -ne 0 ]; then
    break
  fi
  msg_id="$(awk -F': ' '/^id:/ {{ print $2; exit }}' "implementer_claim_${{idx}}.txt")"
  summary="$(awk -F': ' '/^summary:/ {{ print $2; exit }}' "implementer_claim_${{idx}}.txt")"
  test -n "$msg_id"
  "${{TT[@]}}" inbox ack "$msg_id" --role implementer
  case "$summary" in
    'urgent multiply regression')
      "${{TT[@]}}" todo add --role implementer --message "$msg_id" 'Patch multiply implementation' > "implementer_todo_${{idx}}.txt"
      todo_id="$(awk '{{ for (i = 1; i <= NF; i++) if ($i ~ /^todo_/) {{ print $i; exit }} }}' "implementer_todo_${{idx}}.txt")"
      test -n "$todo_id"
      {q(sys.executable)} - <<'PY'
from pathlib import Path

path = Path("calculator.py")
text = path.read_text(encoding="utf-8")
old = "def multiply(a: int, b: int) -> int:\\n    return a + b\\n"
new = "def multiply(a: int, b: int) -> int:\\n    return a * b\\n"
if old not in text:
    raise SystemExit("expected multiply regression was not found")
path.write_text(text.replace(old, new), encoding="utf-8")
PY
      "${{TT[@]}}" todo done --role implementer "$todo_id"
      "${{TT[@]}}" inbox complete "$msg_id" --role implementer --status fixed --summary 'multiply fixed; add still pending'
      ;;
    'fix add regression')
      "${{TT[@]}}" todo add --role implementer --message "$msg_id" 'Run add-only fix' > "implementer_todo_${{idx}}.txt"
      old_todo_id="$(awk '{{ for (i = 1; i <= NF; i++) if ($i ~ /^todo_/) {{ print $i; exit }} }}' "implementer_todo_${{idx}}.txt")"
      test -n "$old_todo_id"
      "${{TT[@]}}" todo supersede --role implementer "$old_todo_id" 'Patch add and run full unittest' > "implementer_todo_supersede_${{idx}}.txt"
      todo_id="$(awk '/^replacement:/ {{ for (i = 1; i <= NF; i++) if ($i ~ /^todo_/) {{ print $i; exit }} }}' "implementer_todo_supersede_${{idx}}.txt")"
      test -n "$todo_id"
      {q(sys.executable)} - <<'PY'
from pathlib import Path

path = Path("calculator.py")
text = path.read_text(encoding="utf-8")
old = "def add(a: int, b: int) -> int:\\n    return a - b\\n"
new = "def add(a: int, b: int) -> int:\\n    return a + b\\n"
if old not in text:
    raise SystemExit("expected add regression was not found")
path.write_text(text.replace(old, new), encoding="utf-8")
PY
      {q(sys.executable)} -B -m unittest -v test_calculator > implementer_after_add.log 2>&1
      "${{TT[@]}}" todo done --role implementer "$todo_id"
      "${{TT[@]}}" inbox complete "$msg_id" --role implementer --status fixed --summary 'add fixed; unittest passes'
      ;;
    'cleanup README')
      "${{TT[@]}}" inbox complete "$msg_id" --role implementer --status deferred --summary 'deferred behind repair work'
      ;;
    *)
      echo "unexpected implementer summary: $summary"
      exit 14
      ;;
  esac
  processed=$((processed + 1))
done
test "$processed" -eq 3
"""


def run_step(session: str, role: str, paths: Paths, timeout: float, body: str) -> None:
    sentinel = paths.steps / f"{role}.exit"
    script = paths.steps / f"{role}.sh"
    script.write_text(step_script(paths, sentinel, body), encoding="utf-8")
    script.chmod(0o755)
    if sentinel.exists():
        sentinel.unlink()
    send_keys(session, role, f"bash {q(script)}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sentinel.exists():
            code = sentinel.read_text(encoding="utf-8").strip()
            if code == "0":
                return
            pane = capture_pane(session, role)
            artifacts = role_artifacts(paths, role)
            raise DemoError(f"{role} step exited {code}\n\nLast pane output:\n{pane}\n\nArtifacts:\n{artifacts}")
        time.sleep(0.1)
    pane = capture_pane(session, role)
    raise DemoError(f"{role} step timed out after {timeout:.1f}s\n\nLast pane output:\n{pane}")


def step_script(paths: Paths, sentinel: Path, body: str) -> str:
    return f"""#!/usr/bin/env bash
set -u
trap 'status=$?; echo "$status" > {q(sentinel)}' EXIT
set -e
export PYTHONPATH={q(SRC_DIR)}
export PYTHONDONTWRITEBYTECODE=1
TT=({q(sys.executable)} -m tmux_team.cli --config {q(paths.config)})
{body}
"""


def verify(paths: Paths, scenario: str) -> None:
    if scenario == "congestion":
        verify_congestion(paths)
        return

    print("verifying sandbox state")
    result = run([sys.executable, "-B", "-m", "unittest", "-v", "test_calculator"], cwd=paths.project, check=False)
    if result.returncode != 0:
        raise DemoError(f"final unittest failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    db_path = paths.runtime / "team.sqlite"
    if not db_path.exists():
        raise DemoError(f"database missing: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    messages = conn.execute(
        "SELECT sender, recipient, state, result_status, summary FROM messages ORDER BY created_at"
    ).fetchall()
    notifications = conn.execute("SELECT role, method, state FROM notifications ORDER BY id").fetchall()
    todos = conn.execute("SELECT role, state, text FROM todos ORDER BY created_at").fetchall()
    conn.close()

    expected = {
        ("collector", "orchestrator", "completed", "routed"),
        ("orchestrator", "implementer", "completed", "fixed"),
    }
    observed = {(row["sender"], row["recipient"], row["state"], row["result_status"]) for row in messages}
    missing = expected - observed
    if missing:
        rendered = "\n".join(str(dict(row)) for row in messages)
        raise DemoError(f"missing expected message states: {sorted(missing)}\nmessages:\n{rendered}")

    notified_roles = {row["role"] for row in notifications if row["state"] == "notified"}
    if not {"orchestrator", "implementer"}.issubset(notified_roles):
        rendered = "\n".join(str(dict(row)) for row in notifications)
        raise DemoError(f"expected tmux notification records for orchestrator and implementer\n{rendered}")
    if [(row["role"], row["state"], row["text"]) for row in todos] != [
        ("implementer", "done", "Patch add implementation")
    ]:
        rendered = "\n".join(str(dict(row)) for row in todos)
        raise DemoError(f"expected implementer todo to be completed\n{rendered}")

    calculator = (paths.project / "calculator.py").read_text(encoding="utf-8")
    if "return a + b" not in calculator:
        raise DemoError("calculator.py was not fixed")
    dashboard = run(tt(paths) + ["dashboard", "--once", "--no-pane-preview"], cwd=paths.project, check=True).stdout
    for expected in ("tmux-team dashboard", "Roles", "Active Work", "implementer"):
        if expected not in dashboard:
            raise DemoError(f"dashboard snapshot missing {expected!r}\n{dashboard}")

    print("messages:")
    for row in messages:
        print(f"  {row['sender']} -> {row['recipient']}: {row['state']} / {row['result_status']} / {row['summary']}")
    print("notifications:")
    for row in notifications:
        print(f"  {row['role']}: {row['state']}")
    print("todos:")
    for row in todos:
        print(f"  {row['role']}: {row['state']} / {row['text']}")


def verify_congestion(paths: Paths) -> None:
    print("verifying congestion sandbox state")
    result = run([sys.executable, "-B", "-m", "unittest", "-v", "test_calculator"], cwd=paths.project, check=False)
    if result.returncode != 0:
        raise DemoError(f"final unittest failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    db_path = paths.runtime / "team.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    messages = conn.execute(
        "SELECT sender, recipient, priority, state, result_status, summary FROM messages ORDER BY created_at"
    ).fetchall()
    claims = conn.execute(
        """
        SELECT m.recipient, m.summary
        FROM events e
        JOIN messages m ON m.id = e.ref_id
        WHERE e.type = 'message.claimed'
        ORDER BY e.id
        """
    ).fetchall()
    notifications = conn.execute("SELECT role, method, state FROM notifications ORDER BY id").fetchall()
    todos = conn.execute("SELECT role, state, text, superseded_by FROM todos ORDER BY created_at").fetchall()
    conn.close()

    expected_states = {
        "operator blocked background task": ("implementer", "blocked_by_role_paused", None),
        "urgent multiply regression": ("implementer", "completed", "fixed"),
        "cleanup README": ("implementer", "completed", "deferred"),
        "collector source note": ("orchestrator", "completed", "noted"),
        "collector add regression": ("orchestrator", "completed", "routed"),
        "fix add regression": ("implementer", "completed", "fixed"),
    }
    by_summary = {row["summary"]: row for row in messages}
    missing = set(expected_states) - set(by_summary)
    if missing:
        raise DemoError(f"missing expected messages: {sorted(missing)}")
    for summary, (recipient, state, result_status) in expected_states.items():
        row = by_summary[summary]
        if row["recipient"] != recipient or row["state"] != state or row["result_status"] != result_status:
            raise DemoError(f"unexpected state for {summary!r}: {dict(row)}")

    claim_order = [(row["recipient"], row["summary"]) for row in claims]
    expected_claim_order = [
        ("orchestrator", "collector add regression"),
        ("orchestrator", "collector source note"),
        ("implementer", "urgent multiply regression"),
        ("implementer", "fix add regression"),
        ("implementer", "cleanup README"),
    ]
    if claim_order != expected_claim_order:
        raise DemoError(f"unexpected claim order:\nobserved={claim_order}\nexpected={expected_claim_order}")

    notified_roles = {row["role"] for row in notifications if row["state"] == "notified"}
    if not {"orchestrator", "implementer"}.issubset(notified_roles):
        rendered = "\n".join(str(dict(row)) for row in notifications)
        raise DemoError(f"expected tmux notification records for orchestrator and implementer\n{rendered}")
    if not any(
        row["role"] == "implementer" and row["method"] == "send-keys" and row["state"] == "notify_deferred"
        for row in notifications
    ):
        rendered = "\n".join(str(dict(row)) for row in notifications)
        raise DemoError(f"expected copy-mode send-keys notification deferral\n{rendered}")
    todo_states = {(row["state"], row["text"]) for row in todos}
    expected_todos = {
        ("done", "Patch multiply implementation"),
        ("superseded", "Run add-only fix"),
        ("done", "Patch add and run full unittest"),
    }
    if not expected_todos.issubset(todo_states):
        rendered = "\n".join(str(dict(row)) for row in todos)
        raise DemoError(f"missing expected todo states\n{rendered}")
    open_todos = [dict(row) for row in todos if row["state"] == "open"]
    if open_todos:
        raise DemoError(f"expected no open todos after congestion flow\n{open_todos}")

    calculator = (paths.project / "calculator.py").read_text(encoding="utf-8")
    if "return a + b" not in calculator or "return a * b" not in calculator:
        raise DemoError("calculator.py was not fully fixed")
    dashboard = run(tt(paths) + ["dashboard", "--once", "--no-pane-preview"], cwd=paths.project, check=True).stdout
    for expected in ("tmux-team dashboard", "Roles", "Active Work", "implementer"):
        if expected not in dashboard:
            raise DemoError(f"dashboard snapshot missing {expected!r}\n{dashboard}")

    print("messages:")
    for row in messages:
        print(
            f"  {row['sender']} -> {row['recipient']}: {row['priority']} / "
            f"{row['state']} / {row['result_status']} / {row['summary']}"
        )
    print("claim order:")
    for recipient, summary in claim_order:
        print(f"  {recipient}: {summary}")
    print("todos:")
    for row in todos:
        suffix = f" superseded_by={row['superseded_by']}" if row["superseded_by"] else ""
        print(f"  {row['role']}: {row['state']} / {row['text']}{suffix}")


def send_keys(session: str, role: str, command: str) -> None:
    target = f"{session}:{role}.0"
    run(["tmux", "send-keys", "-t", target, command, "Enter"], check=True)


def capture_pane(session: str, role: str) -> str:
    result = run(["tmux", "capture-pane", "-p", "-t", f"{session}:{role}.0", "-S", "-120"], check=False)
    return result.stdout or result.stderr


def role_artifacts(paths: Paths, role: str) -> str:
    chunks: list[str] = []
    for path in sorted(paths.project.glob(f"{role}_*.log")) + sorted(paths.project.glob(f"{role}_*.txt")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = "<non-text artifact>"
        chunks.append(f"--- {path.name} ---\n{text[-4000:]}")
    if not chunks:
        return "(none)"
    return "\n".join(chunks)


def tmux_ok(*args: str) -> bool:
    result = run(["tmux", *args], check=False)
    return result.returncode == 0


def run(command: list[str], cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = str(SRC_DIR)
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
        raise DemoError(
            f"command failed: {' '.join(shlex.quote(part) for part in command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def tt(paths: Paths) -> list[str]:
    return [sys.executable, "-m", "tmux_team.cli", "--config", str(paths.config)]


def q(value: object) -> str:
    return shlex.quote(str(value))


class DemoError(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
