"""Command-line bootstrap. Paper never loads the API-wallet environment key."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .client import OneApiClient, OneApiError, book_from_wire
from .config import load_config, write_config
from .paper import PaperExchange


async def watch_paper(config, *, client_factory=OneApiClient) -> None:
    paper = PaperExchange(config.paper_balance)
    async with client_factory(config) as client:
        # This foundation command observes/simulates live book state. Strategy
        # orchestration is supplied by the strategies/safety follow-up.
        async for frame in client.stream([{"type": "l2Book", "coin": config.symbol}]):
            if frame.get("channel") == "l2Book":
                book = book_from_wire(frame["data"])
                await paper.on_book(book)
                print(
                    f"PAPER {book.symbol} mid={book.mid} local_fills={len(paper.fills)}", flush=True
                )


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
        if selected.mode != "paper":
            raise ValueError("live runner requires strategy/risk integration; use paper")
        asyncio.run(watch_paper(selected))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, OneApiError):
        # Exception bodies can include HTTP headers or local credential paths.
        print(
            "Unable to run: check configuration, credentials and One API availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
