"""Command-line bootstrap. Paper never loads the API-wallet environment key."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import __version__
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
    try:
        installed_version = version("glyde-market-maker")
    except PackageNotFoundError:
        installed_version = __version__
    parser.add_argument("--version", action="version", version=f"glyde-mm {installed_version}")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="write a private paper configuration")
    init.add_argument("--output", default="glyde-mm.yaml")
    init.add_argument("--non-interactive", action="store_true")
    config = commands.add_parser("config", help="manage configuration")
    sub = config.add_subparsers(dest="config_command", required=True)
    create = sub.add_parser("init")
    create.add_argument("--output", default="glyde-mm.yaml")
    for name in ("paper", "run", "doctor"):
        run = commands.add_parser(name)
        run.add_argument("config_path", nargs="?")
        run.add_argument("--config", dest="config_option")
    report = commands.add_parser("report", help="summarize private run logs for a support ticket")
    report.add_argument("logdir", type=Path)
    for name in ("record", "replay"):
        history = commands.add_parser(name, help="capture/replay local public books in paper mode")
        history.add_argument("--config", dest="config_option", default="glyde-mm.yaml")
        history.add_argument("--minutes", type=int, default=5)
        history.add_argument("--book-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            from .report import summarize

            print(json.dumps(summarize(args.logdir), indent=2, sort_keys=True))
            return 0
        if args.command == "init":
            from .wizard import run_wizard

            run_wizard(args.output, interactive=not args.non_interactive)
            return 0
        if args.command == "config":
            write_config(args.output)
            print("Paper configuration created; API keys belong in environment variables.")
            return 0
        if args.command in {"record", "replay"}:
            selected = load_config(args.config_option)
            if args.command == "record":
                from .book_history import record_books

                count = asyncio.run(record_books(selected, args.book_file, args.minutes))
                print(f"Recorded {count} public books; no private account or orders accessed.")
            else:
                from .replay import replay

                result = asyncio.run(replay(selected, args.book_file, args.minutes))
                print(
                    f"PAPER REPLAY | inventory={result.inventory:f}"
                    f" | simulated_pnl={result.pnl:+f} USDC | fills={result.fills}"
                    f" | risk_stop={result.risk_stop or 'none'}"
                    f" | simulated_seconds={(result.end_ms - result.start_ms) / 1000:g}"
                )
                print("Quotes cancelled. Simulation excludes fees, funding and queue priority.")
            return 0
        if args.config_path and args.config_option:
            parser.error("use either a positional config or --config")
        config_path = Path(args.config_path or args.config_option or "glyde-mm.yaml")
        if args.command == "doctor":
            from .doctor import check

            return asyncio.run(check(config_path))
        selected = load_config(config_path)
        if args.command == "paper":
            from dataclasses import replace

            selected = replace(selected, mode="paper")
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
