"""Командная строка: `ario run <id>`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ario.agents.backend import OpenHandsBackend
from ario.config import Settings
from ario.fsutil import RunInProgressError
from ario.guardrails import Guardrails
from ario.orchestrator import (
    Orchestrator,
    TicketAlreadyFinishedError,
    TicketNotReopenableError,
    TicketNotTakenError,
)
from ario.pipeline import build_pipeline
from ario.store import InvalidTicketIdError, TicketNotFoundError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ario")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="провести один тикет по потоку")
    run.add_argument("ticket_id")
    resume = sub.add_parser("resume", help="возобновить тикет из needs-human с упавшего этапа")
    resume.add_argument("ticket_id")
    trace = sub.add_parser("trace", help="разбор трассы тикета")
    trace.add_argument("ticket_id")
    trace.add_argument("--steps", action="store_true", help="пошаговая лента вызовов агентов")
    trace.add_argument("--stage", default=None, help="показать только этот этап (с --steps)")
    trace.add_argument(
        "--full", action="store_true", help="подробнее: задача, мысли, длинные тексты"
    )
    return parser


def _trace(settings: Settings, args: argparse.Namespace) -> int:
    from ario.store import validate_ticket_id
    from ario.trace_report import load_events, render_steps, render_summary, summarize

    try:
        validate_ticket_id(args.ticket_id)
    except InvalidTicketIdError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    path = settings.traces_dir / args.ticket_id / "events.jsonl"
    if not path.is_file():
        print(f"Ошибка: нет трассы тикета {args.ticket_id} ({path})", file=sys.stderr)
        return 2
    events, skipped = load_events(path)
    if args.steps:
        print(render_steps(events, args.stage, full=args.full), end="")
    else:
        print(render_summary(summarize(events, skipped)), end="")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # консоль Windows может быть в cp1251: незнакомый символ не должен ронять вывод
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = _build_parser().parse_args(argv)
    settings = Settings.load(args.env_file)
    if args.command == "trace":
        return _trace(settings, args)
    try:
        backend = OpenHandsBackend(settings.llm_model, settings.openrouter_api_key)
        stages = build_pipeline(backend)
        guardrails = Guardrails.load(settings.guardrails_path)
        orchestrator = Orchestrator(settings, stages, guardrails)
        if args.command == "resume":
            outcome = orchestrator.reopen(args.ticket_id)
        else:
            outcome = orchestrator.run(args.ticket_id)
    except (
        TicketNotFoundError,
        InvalidTicketIdError,
        TicketAlreadyFinishedError,
        TicketNotReopenableError,
        RunInProgressError,
    ) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    except TicketNotTakenError as e:
        print(f"Тикет {args.ticket_id} не взят в работу: {e.reason}", file=sys.stderr)
        return 3
    print(f"Тикет {args.ticket_id}: {outcome}")
    return 0 if outcome == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
