from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

PROVIDERS = ("codex", "cursor", "claude", "pool")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the start-tmux-team skill for selected agent providers.")
    parser.add_argument(
        "--providers",
        default="codex",
        help="Comma-separated codex,cursor,claude,pool or all; default: codex",
    )
    return parser.parse_args()


def selected_providers(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))
    if values == ("all",):
        return PROVIDERS
    unknown = set(values) - set(PROVIDERS)
    if not values or unknown:
        expected = ", ".join(PROVIDERS)
        raise ValueError(f"providers must be a comma-separated subset of {expected}, or all")
    return values


def provider_skill_root(provider: str, environ: dict[str, str] | None = None) -> Path:
    env = environ or os.environ
    home = Path(env.get("HOME") or Path.home()).expanduser()
    if provider == "codex":
        return Path(env.get("CODEX_HOME") or home / ".codex") / "skills"
    if provider == "cursor":
        return Path(env.get("CURSOR_HOME") or home / ".cursor") / "skills"
    if provider == "claude":
        return Path(env.get("CLAUDE_HOME") or home / ".claude") / "skills"
    if provider == "pool":
        config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        return Path(env.get("POOL_SKILLS_HOME") or config_home / "poolside" / "skills")
    raise ValueError(f"unknown provider: {provider}")


def install_skill(source: Path, provider: str, environ: dict[str, str] | None = None) -> Path:
    destination = provider_skill_root(provider, environ) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination


def main() -> int:
    args = parse_args()
    source = Path(__file__).resolve().parents[1] / "skills" / "start-tmux-team"
    try:
        providers = selected_providers(args.providers)
    except ValueError as exc:
        print(f"tmux-team: {exc}")
        return 2
    for provider in providers:
        print(f"{provider}: {install_skill(source, provider)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
