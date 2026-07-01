from __future__ import annotations

import logging
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str | None = None, log_file: str | None = None) -> None:
    numeric_level = parse_level(level or "WARNING")
    handlers: list[logging.Handler] = []
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=numeric_level,
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def parse_level(level: str) -> int:
    normalized = level.strip().upper()
    value = getattr(logging, normalized, None)
    if not isinstance(value, int):
        raise ValueError(f"invalid log level: {level}")
    return value
