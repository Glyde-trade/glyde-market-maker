"""Command-line bootstrap. Paper never loads the API-wallet environment key."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .client import OneApiError
from .config import load_config, write_config
from .preflight import LivePreflightRefused, summary
from .risk import RiskViolation
from .runner import run as run_strategy
from .version import build_identity


async def run_with_signals(
    selected, state_directory: Path, *, allow_existing_exposure: bool = False
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    try:
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.signal(kind, request_stop)
        await run_strategy(
            selected,
            state_directory=state_directory,
            stop_event=stop,
            allow_existing_exposure=allow_existing_exposure,
        )
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glyde-mm", description="Glyde paper-first market maker")
    installed_version = build_identity()["version"]
    parser.add_argument("--version", action="version", version=f"glyde-mm {installed_version}")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="write a private paper configuration")
    init.add_argument("--output", default="glyde-mm.yaml")
    init.add_argument("--non-interactive", action="store_true")
    config = commands.add_parser("config", help="manage configuration")
    sub = config.add_subparsers(dest="config_command", required=True)
    create = sub.add_parser("init")
    create.add_argument("--output", default="glyde-mm.yaml")
    for name in ("paper", "run", "doctor", "stop"):
        run = commands.add_parser(name)
        run.add_argument("config_path", nargs="?")
        run.add_argument("--config", dest="config_option")
        if name == "run":
            run.add_argument(
                "--i-understand",
                action="store_true",
                help=(
                    "allow existing symbol orders/positions; confirmation and risk caps still apply"
                ),
            )
        if name == "doctor":
            run.add_argument(
                "--private", action="store_true", help="check live read permissions only"
            )
    report = commands.add_parser("report", help="summarize private run logs for a support ticket")
    report.add_argument("logdir", type=Path)
    for name in ("record", "replay"):
        history = commands.add_parser(name, help="capture/replay local public books in paper mode")
        history.add_argument("--config", dest="config_option", default="glyde-mm.yaml")
        history.add_argument("--minutes", type=int, default=5)
        history.add_argument("--book-file", required=True, type=Path)
    compare = commands.add_parser(
        "compare", help="compare paper strategies on a recorded public tape"
    )
    compare.add_argument("--config", dest="config_option", default="glyde-mm.yaml")
    compare.add_argument("--tape-file", required=True, type=Path)
    compare.add_argument(
        "--preset", choices=("conservative", "balanced", "active"), default="balanced"
    )
    tape = commands.add_parser(
        "record-tape", help="capture public Glyde books/trades for comparison"
    )
    tape.add_argument("--config", dest="config_option", default="glyde-mm.yaml")
    tape.add_argument("--tape-file", required=True, type=Path)
    tape.add_argument("--minutes", type=int, default=5)
    tape.add_argument(
        "--include-hl", action="store_true", help="also record public HL reference books"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            from .compare import compare_strategies, format_comparison

            result = asyncio.run(
                compare_strategies(
                    load_config(args.config_option), args.tape_file, preset=args.preset
                )
            )
            print(format_comparison(result))
            return 0
        if args.command == "record-tape":
            from .public_tape import record_tape

            count = asyncio.run(
                record_tape(
                    load_config(args.config_option),
                    args.tape_file,
                    args.minutes,
                    include_hl=args.include_hl,
                )
            )
            print(f"Recorded {count} public tape events; no private data or orders accessed.")
            return 0
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

                result = asyncio.run(
                    replay(
                        selected,
                        args.book_file,
                        args.minutes,
                        state_directory=Path(args.config_option).parent / ".glyde-mm",
                    )
                )
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

            return asyncio.run(check(config_path, private=args.private))
        selected = load_config(config_path)
        if args.command == "stop":
            from .shutdown import stop_bot

            result, path = asyncio.run(stop_bot(selected, config_path.parent / ".glyde-mm"))
            print(f"STOP {selected.symbol}: {result['outcome']} | report={path}")
            if result["outcome"] != "stopped":
                print(
                    "Cancellation NOT confirmed; check the venue before restarting.",
                    file=sys.stderr,
                )
                return 1
            print("No positions closed. Other symbols are unchanged.")
            return 0
        if args.command == "paper":
            from dataclasses import replace

            selected = replace(selected, mode="paper")
        print(summary(selected), flush=True)
        if selected.mode == "live":
            print("Daily loss is an equity stop threshold, not a guaranteed maximum loss.")
            print("Cleanup can cancel other orders in this account; use a dedicated account.")
            try:
                confirmed = input(f"Type {selected.symbol} to confirm LIVE trading: ")
            except EOFError:
                print("Live start cancelled: symbol confirmation is required.", file=sys.stderr)
                return 1
            if confirmed != selected.symbol:
                print("Live start cancelled: symbol confirmation did not match.", file=sys.stderr)
                return 1
        asyncio.run(
            run_with_signals(
                selected,
                config_path.parent / ".glyde-mm",
                allow_existing_exposure=getattr(args, "i_understand", False),
            )
        )
        return 0
    except LivePreflightRefused:
        print(
            "Live start refused: existing orders or a position on this symbol. "
            "Review the account and use --i-understand only to accept that exposure.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 1 if args.command == "stop" else 0
    except (ValueError, OSError, OneApiError, RiskViolation, TimeoutError):
        # Exception bodies can include HTTP headers or local credential paths.
        print(
            "Unable to run: check configuration, credentials and One API availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
