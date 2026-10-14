"""Командная строка: `ario run`, `fetch`, `resume`, `trace`, `eval`, `export-otel`."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ario.agents.backend import OpenHandsBackend
from ario.config import ConfigError, Settings, check_sdk_telemetry
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
    run.add_argument(
        "--repo", default=None, help="профиль репозитория (иначе выбирается по area path и тегам)"
    )
    resume = sub.add_parser("resume", help="возобновить тикет из needs-human с упавшего этапа")
    resume.add_argument("ticket_id")
    ev = sub.add_parser("eval", help="прогнать набор тикетов с ожидаемыми исходами и метриками")
    ev.add_argument(
        "--dir", type=Path, default=Path("evals"), help="папка с cases/ и expected.yaml"
    )
    ev.add_argument("--only", nargs="*", default=None, help="id кейсов (по умолчанию все)")
    fetch = sub.add_parser("fetch", help="забрать тикеты из TFS в tickets/inbox/ (только чтение)")
    fetch.add_argument(
        "--id", type=int, nargs="+", default=None, help="id work items (по умолчанию запрос WIQL)"
    )
    fetch.add_argument(
        "--dry-run", action="store_true", help="показать, что будет взято, ничего не записывая"
    )
    otel = sub.add_parser(
        "export-otel",
        help="выгрузить метрики и тайминги трассы тикета в OTLP (Phoenix), без текста",
    )
    otel.add_argument("ticket_id")
    otel.add_argument(
        "--endpoint", default=None, help="OTLP/HTTP URL (по умолчанию ARIO_OTLP_ENDPOINT)"
    )
    otel.add_argument(
        "--dry-run", action="store_true", help="показать состав спанов, ничего не отправляя"
    )
    trace = sub.add_parser("trace", help="разбор трассы тикета")
    trace.add_argument("ticket_id")
    trace.add_argument("--steps", action="store_true", help="пошаговая лента вызовов агентов")
    trace.add_argument("--stage", default=None, help="показать только этот этап (с --steps)")
    trace.add_argument(
        "--full", action="store_true", help="подробнее: задача, мысли, длинные тексты"
    )
    return parser


def _load_trace_events(
    settings: Settings, ticket_id: str
) -> tuple[list[dict[str, Any]], int] | None:
    """События трассы тикета и число пропущенных строк; None, если трассы нет (ошибка выведена)."""
    from ario.store import validate_ticket_id
    from ario.trace_report import load_events

    try:
        validate_ticket_id(ticket_id)
    except InvalidTicketIdError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return None
    path = settings.traces_dir / ticket_id / "events.jsonl"
    if not path.is_file():
        print(f"Ошибка: нет трассы тикета {ticket_id} ({path})", file=sys.stderr)
        return None
    events, skipped = load_events(path)
    return list(events), skipped


def _enable_llm_tracing(settings: Settings) -> bool:
    """Живая выгрузка запросов к модели (с текстом) в локальный Phoenix, решение D07, вариант Б."""
    import atexit

    from ario import llm_telemetry
    from ario.guardrails import Guardrails
    from ario.trace import Masker

    try:
        endpoint = llm_telemetry.check_llm_content_settings(
            settings.otlp_endpoint, allow_remote=settings.allow_remote_llm_content
        )
        guardrails = Guardrails.load(settings.guardrails_path)
        masker = Masker.from_environment(
            os.environ, guardrails.config.env, [settings.openrouter_api_key, settings.tfs_pat]
        )
        llm_telemetry.enable(endpoint, masker)
    except (
        llm_telemetry.LlmTracingError,
        OSError,
        ValueError,
    ) as e:  # ValueError: схема guardrails
        print(f"Ошибка настройки: {e}", file=sys.stderr)
        return False
    atexit.register(llm_telemetry.shutdown)  # дослать накопленные спаны при выходе
    print(
        "Живая выгрузка запросов к модели (с текстом) включена: "
        f"{settings.otlp_endpoint}, проект {llm_telemetry.LLM_PROJECT}"
    )
    return True


def _use_system_certificates() -> None:
    import truststore

    truststore.inject_into_ssl()  # корпоративный прокси и внутренние серверы с собственным CA


def _trace(settings: Settings, args: argparse.Namespace) -> int:
    from ario.trace_report import render_steps, render_summary, summarize

    loaded = _load_trace_events(settings, args.ticket_id)
    if loaded is None:
        return 2
    events, skipped = loaded
    if args.steps:
        print(render_steps(events, args.stage, full=args.full), end="")
    else:
        print(render_summary(summarize(events, skipped)), end="")
    return 0


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_FETCH_MARKS = {
    "fetched": "взят",
    "would-fetch": "будет взят",
    "skipped": "пропущен",
    "failed": "ОШИБКА",
}


def _fetch(settings: Settings, args: argparse.Namespace) -> int:
    from ario.store import TicketStore
    from ario.tfs.client import TfsClient, TfsError
    from ario.tfs.fetch import CANDIDATES_LIMIT, FetchItem, fetch_tickets, query_ids
    from ario.trace import Masker

    missing = [
        name
        for name, value in (
            ("TFS_URL", settings.tfs_url),
            ("TFS_PROJECT", settings.tfs_project),
            ("TFS_PAT", settings.tfs_pat),
        )
        if not value
    ]
    if missing or not (settings.tfs_url and settings.tfs_project and settings.tfs_pat):
        print(f"Ошибка настройки: не заданы {', '.join(missing)}", file=sys.stderr)
        return 2
    masker = Masker([settings.tfs_pat])

    def show(item: FetchItem) -> None:
        # заголовок тикета недоверенный: управляющие символы терминала не выводим
        detail = _CONTROL_CHARS.sub("?", item.detail)
        print(f"[{_FETCH_MARKS[item.status]}] {item.ticket_id}: {detail}", flush=True)

    _use_system_certificates()
    store = TicketStore(settings.tickets_dir)
    try:
        with TfsClient(settings.tfs_url, settings.tfs_project, settings.tfs_pat) as client:
            explicit: list[int] | None = args.id
            ids = explicit if explicit else query_ids(client, settings.tfs_wiql)
            if not ids:
                print("Подходящих тикетов в TFS нет")
                return 0
            report = fetch_tickets(
                client,
                ids,
                store,
                limit=None if explicit else settings.tfs_max_items,
                dry_run=args.dry_run,
                mask=masker.mask_text,
                on_item=show,
            )
    except (TfsError, ValueError) as e:
        print(f"Ошибка: {masker.mask_text(str(e))}", file=sys.stderr)
        return 2
    new = len(report.of("would-fetch" if args.dry_run else "fetched"))
    if not explicit and len(ids) >= CANDIDATES_LIMIT and new < settings.tfs_max_items:
        print(
            f"Предупреждение: запрос вернул максимум ({CANDIDATES_LIMIT}) тикетов, более старые "
            "не просмотрены; сузьте отбор через TFS_WIQL",
            file=sys.stderr,
        )
    print(
        f"\nИтого: {'будет взято' if args.dry_run else 'взято'} {new}, "
        f"пропущено {len(report.of('skipped'))}, ошибок {len(report.of('failed'))}"
    )
    return 1 if report.failed else 0


def _export_otel(settings: Settings, args: argparse.Namespace) -> int:
    from collections import Counter

    from ario.otel_export import ExportError, build_spans, export_spans

    loaded = _load_trace_events(settings, args.ticket_id)
    if loaded is None:
        return 2
    spans = build_spans(loaded[0])
    if args.dry_run:
        kinds = Counter(str((s.attributes or {}).get("openinference.span.kind")) for s in spans)
        print(f"Спанов: {len(spans)} ({', '.join(f'{k}: {n}' for k, n in sorted(kinds.items()))})")
        print("Текст промптов, мыслей, аргументов и результатов инструментов не экспортируется.")
        return 0
    endpoint = args.endpoint or settings.otlp_endpoint
    if not endpoint:
        print("Ошибка: укажите --endpoint или ARIO_OTLP_ENDPOINT", file=sys.stderr)
        return 2
    _use_system_certificates()
    try:
        count = export_spans(spans, endpoint)
    except ExportError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    print(f"Отправлено спанов: {count} (тикет {args.ticket_id}) в {endpoint}")
    return 0


def _auto_export(settings: Settings, ticket_id: str) -> None:
    """Автоэкспорт в конце прогона: сбой телеметрии прогон не ломает."""
    if not settings.otlp_endpoint:
        return
    from ario.otel_export import build_spans, export_spans

    try:
        loaded = _load_trace_events(settings, ticket_id)
        if loaded and loaded[0]:
            _use_system_certificates()
            count = export_spans(build_spans(loaded[0]), settings.otlp_endpoint)
            print(f"Метрики отправлены в {settings.otlp_endpoint} (спанов: {count})")
    except Exception as e:  # телеметрия вторична
        print(f"Предупреждение: экспорт метрик не удался ({type(e).__name__})", file=sys.stderr)


def _eval(settings: Settings, args: argparse.Namespace) -> int:
    from ario.evals import CaseResult, run_evals

    def show(result: CaseResult) -> None:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.case_id}: {result.outcome}", flush=True)

    def factory(s: Settings) -> OpenHandsBackend:
        return OpenHandsBackend(s.llm_model, s.openrouter_api_key, role_models=s.role_models)

    try:
        run_dir, results = run_evals(settings, args.dir, factory, args.only, on_result=show)
    except (ValueError, FileNotFoundError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    print(f"\nОтчёт: {run_dir / 'report.md'}")
    return 0 if all(r.passed for r in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    # консоль Windows может быть в cp1251: незнакомый символ не должен ронять вывод
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = _build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.env_file)
    except ConfigError as e:
        print(f"Ошибка настройки: {e}", file=sys.stderr)
        return 2
    if args.command in ("run", "resume", "eval"):
        try:
            check_sdk_telemetry(os.environ)  # SDK не должен слать тексты запросов наружу
        except ConfigError as e:
            print(f"Ошибка настройки: {e}", file=sys.stderr)
            return 2
    llm_content = args.command in ("run", "resume", "eval") and settings.otlp_llm_content
    if llm_content and not _enable_llm_tracing(settings):
        return 2
    if args.command == "trace":
        return _trace(settings, args)
    if args.command == "eval":
        return _eval(settings, args)
    if args.command == "fetch":
        return _fetch(settings, args)
    if args.command == "export-otel":
        return _export_otel(settings, args)
    try:
        backend = OpenHandsBackend(
            settings.llm_model, settings.openrouter_api_key, role_models=settings.role_models
        )
        stages = build_pipeline(backend)
        guardrails = Guardrails.load(settings.guardrails_path)
        orchestrator = Orchestrator(
            settings, stages, guardrails, repo_override=getattr(args, "repo", None)
        )
        if args.command == "resume":
            outcome = orchestrator.reopen(args.ticket_id)
        else:
            outcome = orchestrator.run(args.ticket_id)
    except (
        ConfigError,
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
    _auto_export(settings, args.ticket_id)
    return 0 if outcome == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
