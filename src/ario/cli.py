"""Командная строка: `ario run <id>`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ario.config import Settings
from ario.orchestrator import Orchestrator, TicketAlreadyFinishedError
from ario.store import InvalidTicketIdError, TicketNotFoundError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ario")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="провести один тикет по потоку")
    run.add_argument("ticket_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings.load(args.env_file)
    try:
        outcome = Orchestrator(settings).run(args.ticket_id)
    except (TicketNotFoundError, InvalidTicketIdError, TicketAlreadyFinishedError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    print(f"Тикет {args.ticket_id}: {outcome}")
    return 0 if outcome == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
