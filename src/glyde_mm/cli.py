"""Command-line bootstrap. Paper never loads the API-wallet environment key."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
from pathlib import Path
from typing import TypedDict

from .client import OneApiError
from .config import Config, load_config, write_config
from .market_preflight import MarketPreflightRequired
from .paper_result import PaperResult
from .preflight import LivePreflightRefused, summary
from .risk import RiskViolation
from .runner import run as run_strategy
from .version import build_identity


class _LogOptions(TypedDict, total=False):
    log_directory: Path
    paper_result: PaperResult


async def run_with_signals(
    selected: Config,
    state_directory: Path,
    *,
    allow_existing_exposure: bool = False,
    duration_seconds: float | None = None,
    log_directory: Path | None = None,
    report_path: Path | None = None,
) -> bool:
    if duration_seconds is not None and not 0 < duration_seconds <= (
        86400 if selected.mode == "paper" else 1800
    ):
        raise ValueError("session duration exceeds its allowed bound")
    result = PaperResult(report_path, selected) if report_path is not None else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    timer = None
    automatic = False
    interrupted = False
    failed = False

    def automatic_stop() -> None:
        nonlocal automatic
        automatic = True
        stop.set()

    def request_stop(*_: object) -> None:
        nonlocal interrupted
        interrupted = True
        loop.call_soon_threadsafe(stop.set)

    try:
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.signal(kind, request_stop)
        if duration_seconds is not None:
            timer = loop.call_later(duration_seconds, automatic_stop)
        options: _LogOptions = {"log_directory": log_directory} if log_directory is not None else {}
        if result is not None:
            options["paper_result"] = result
        await run_strategy(
            selected,
            state_directory=state_directory,
            stop_event=stop,
            allow_existing_exposure=allow_existing_exposure,
            **options,
        )
        return duration_seconds is None or (automatic and not interrupted)
    except BaseException:
        failed = True
        raise
    finally:
        if timer is not None:
            timer.cancel()
        for kind, handler in previous.items():
            signal.signal(kind, handler)
        if result is not None:
            try:
                row = result.write(
                    "error"
                    if failed
                    else "signal"
                    if interrupted
                    else "duration_elapsed"
                    if automatic
                    else "completed"
                )
                print(
                    f"PAPER RESULT | outcome={row['outcome']} | termination={row['termination']}"
                    f" | uptime={row['uptime_seconds']}s"
                    f" | quotes={row['quotes'].get('placed', 0)}"
                    f" | fills={row['fills']}"
                    f" | realized_pnl={row['realized_pnl_usdc']}"
                    f" | unrealized_pnl={row['unrealized_pnl_usdc']}"
                    f" | max_drawdown={row['max_drawdown_usdc']} | report={result.path}"
                )
            except OSError:
                if not failed:
                    raise
                print("Paper result could not be written; session failed.", file=sys.stderr)


def duration(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h)?", value)
    if match is None:
        raise argparse.ArgumentTypeError("use positive seconds, minutes or hours (20s, 10m, 1h)")
    seconds = float(match[1]) * {None: 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if not 0 < seconds <= 86400:
        raise argparse.ArgumentTypeError("paper duration must be positive and at most 24h")
    return seconds


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
    for name in ("paper", "run", "doctor", "preflight", "stop", "live-wizard"):
        run = commands.add_parser(name)
        run.add_argument("config_path", nargs="?")
        run.add_argument("--config", dest="config_option")
        if name == "paper":
            run.add_argument(
                "--duration", type=duration, help="stop cleanly after e.g. 20s, 10m or 1h (max 24h)"
            )
            run.add_argument(
                "--report", type=Path, help="write a new private JSON paper-session result"
            )
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
    dashboard = commands.add_parser("dashboard", help="serve one local read-only bot snapshot")
    dashboard.add_argument("--state", required=True, type=Path)
    dashboard.add_argument("--port", type=int, default=8080)
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
    reconciliation = commands.add_parser(
        "reconcile", help="compare recorded private fills with paper on the same tape"
    )
    reconciliation.add_argument("--session-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "reconcile":
            from .reconciliation import reconcile_session

            print(json.dumps(asyncio.run(reconcile_session(args.session_file)), indent=2))
            return 0
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
        if args.command == "dashboard":
            from .dashboard import serve

            serve(args.state, args.port)
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

                replay_result = asyncio.run(
                    replay(
                        selected,
                        args.book_file,
                        args.minutes,
                        state_directory=Path(args.config_option).parent / ".glyde-mm",
                    )
                )
                print(
                    f"PAPER REPLAY | inventory={replay_result.inventory:f}"
                    f" | simulated_pnl={replay_result.pnl:+f} USDC | fills={replay_result.fills}"
                    f" | risk_stop={replay_result.risk_stop or 'none'}"
                    f" | simulated_seconds="
                    f"{(replay_result.end_ms - replay_result.start_ms) / 1000:g}"
                )
                print("Quotes cancelled. Simulation excludes fees, funding and queue priority.")
            return 0
        if args.config_path and args.config_option:
            parser.error("use either a positional config or --config")
        config_path = Path(args.config_path or args.config_option or "glyde-mm.yaml")
        if args.command == "live-wizard":
            from .live_wizard import first_live_session

            return asyncio.run(first_live_session(config_path, run_session=run_with_signals))
        if args.command == "doctor":
            from .doctor import check

            return asyncio.run(check(config_path, private=args.private))
        selected = load_config(config_path)
        if args.command == "preflight":
            from .market_preflight import check as check_markets

            return asyncio.run(check_markets(selected, config_path.parent / ".glyde-mm"))
        if args.command == "stop":
            from .shutdown import stop_bot

            stop_result, path = asyncio.run(stop_bot(selected, config_path.parent / ".glyde-mm"))
            print(f"STOP {','.join(selected.symbols)}: {stop_result['outcome']} | report={path}")
            if stop_result["outcome"] != "stopped":
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
                confirmed = input(f"Type {','.join(selected.symbols)} to confirm LIVE trading: ")
            except EOFError:
                print("Live start cancelled: symbol confirmation is required.", file=sys.stderr)
                return 1
            if confirmed != ",".join(selected.symbols):
                print("Live start cancelled: symbol confirmation did not match.", file=sys.stderr)
                return 1
        if selected.mode == "live":
            from .market_preflight import require_preflight

            require_preflight(selected, config_path.parent / ".glyde-mm")
        asyncio.run(
            run_with_signals(
                selected,
                config_path.parent / ".glyde-mm",
                allow_existing_exposure=getattr(args, "i_understand", False),
                **(
                    {
                        key: value
                        for key, value in {
                            "duration_seconds": args.duration,
                            "report_path": args.report,
                        }.items()
                        if value is not None
                    }
                    if args.command == "paper"
                    else {}
                ),
            )
        )
        return 0
    except MarketPreflightRequired:
        print(
            "Run glyde-mm preflight with this exact config; PASS expires after 5 minutes.",
            file=sys.stderr,
        )
        return 1
    except LivePreflightRefused:
        print(
            "Live start refused: existing orders or a position on this symbol. "
            "Review the account and use --i-understand only to accept that exposure.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 1 if args.command in {"stop", "live-wizard"} else 0
    except (ValueError, OSError, OneApiError, RiskViolation, TimeoutError):
        # Exception bodies can include HTTP headers or local credential paths.
        print(
            "Unable to run: check configuration, credentials and One API availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
