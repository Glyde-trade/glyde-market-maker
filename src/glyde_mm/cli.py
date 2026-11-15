"""Command-line bootstrap. Paper never loads the API-wallet environment key."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from .client import OneApiError
from .config import load_config, write_config
from .risk import RiskViolation
from .runner import run as run_strategy


async def run_with_signals(selected, state_directory: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    try:
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.signal(kind, request_stop)
        await run_strategy(selected, state_directory=state_directory, stop_event=stop)
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glyde-mm", description="Glyde paper-first market maker")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="write a private paper configuration")
    init.add_argument("--output", default="glyde-mm.yaml")
    init.add_argument("--non-interactive", action="store_true")
    config = commands.add_parser("config", help="manage configuration")
    sub = config.add_subparsers(dest="config_command", required=True)
    create = sub.add_parser("init")
    create.add_argument("--output", default="glyde-mm.yaml")
    for name in ("paper", "run"):
        run = commands.add_parser(name)
        run.add_argument("config_path", nargs="?")
        run.add_argument("--config", dest="config_option")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            from .wizard import run_wizard

            run_wizard(args.output, interactive=not args.non_interactive)
            return 0
        if args.command == "config":
            write_config(args.output)
            print("Paper configuration created; API keys belong in environment variables.")
            return 0
        if args.config_path and args.config_option:
            parser.error("use either a positional config or --config")
        selected = load_config(args.config_path or args.config_option or "glyde-mm.yaml")
        if args.command == "paper":
            from dataclasses import replace

            selected = replace(selected, mode="paper")
        config_path = Path(args.config_path or args.config_option or "glyde-mm.yaml")
        asyncio.run(run_with_signals(selected, config_path.parent / ".glyde-mm"))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, OneApiError, RiskViolation, TimeoutError):
        # Exception bodies can include HTTP headers or local credential paths.
        print(
            "Unable to run: check configuration, credentials and One API availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
