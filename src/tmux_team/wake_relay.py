from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from .config import ConfigError, load_config
from .extensions.runner import HookDenied, HookError
from .service import TeamService
from .store import Store

DEFAULT_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 60.0


def relay_once(store: Store, conn: sqlite3.Connection) -> tuple[int, int]:
    """Deliver one coalesced app-server wake for each role with queued work."""
    delivered = 0
    failed = 0
    service = TeamService(store)
    for role in store.queued_active_roles(conn):
        result = service.notify_role(
            conn,
            role,
            "app-server-turn",
            actor="wake-relay",
        )
        if result.ok:
            delivered += 1
            print(f"wake relay delivered role={role}: {result.details}", flush=True)
        else:
            failed += 1
            print(f"wake relay deferred role={role}: {result.details}", file=sys.stderr, flush=True)
    return delivered, failed


def run(config_path: Path, interval: float, *, once: bool = False) -> int:
    backoff = interval
    while True:
        try:
            config = load_config(config_path)
            store = Store(config)
            with closing(store.connect()) as conn:
                _delivered, failed = relay_once(store, conn)
            backoff = min(interval * 2, MAX_INTERVAL_SECONDS) if failed else interval
        except (ConfigError, HookDenied, HookError, OSError, sqlite3.Error, ValueError) as exc:
            if once:
                print(f"wake relay failed: {exc}", file=sys.stderr)
                return 1
            print(f"wake relay waiting: {exc}", file=sys.stderr, flush=True)
            backoff = min(max(backoff * 2, interval), MAX_INTERVAL_SECONDS)

        if once:
            return 0
        time.sleep(backoff)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tmux-team-wake-relay",
        description=(
            "Deliver durable queued tmux-team work through loopback Codex app-server "
            "bindings from outside worker sandboxes."
        ),
    )
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    result.add_argument("--once", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 0.1 <= args.interval <= MAX_INTERVAL_SECONDS:
        print(
            f"error: interval must be between 0.1 and {MAX_INTERVAL_SECONDS:g} seconds",
            file=sys.stderr,
        )
        return 2
    return run(args.config, args.interval, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
