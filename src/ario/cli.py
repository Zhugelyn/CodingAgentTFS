"""Командная строка `ario`: run, watch, rework, fetch, resume, trace, eval, export-otel."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ario.agents.backend import OpenHandsBackend
from ario.config import ConfigError, Settings, check_sdk_telemetry
from ario.errors import StageFailed
from ario.fsutil import RunInProgressError
from ario.guardrails import Guardrails
from ario.orchestrator import (
    Orchestrator,
    TicketAlreadyFinishedError,
    TicketNotReopenableError,
    TicketNotTakenError,
)
from ario.pipeline import build_pipeline
from ario.reporting import build_reporter, write_config_problem
from ario.store import InvalidTicketIdError, TicketNotFoundError, TicketStore
from ario.trace import Masker
from ario.webhook import DEFAULT_PATH

if TYPE_CHECKING:
    from ario.tfs.fetch import FetchReport
    from ario.watch import SeenLedger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ario")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="провести один тикет по потоку")
    run.add_argument("ticket_id")
    run.add_argument(
        "--repo", default=None, help="профиль репозитория (иначе выбирается по area path и тегам)"
    )
    watch = sub.add_parser(
        "watch", help="автоматически забирать тикеты из TFS и вести их по потоку (очередь inbox/)"
    )
    watch.add_argument("--once", action="store_true", help="один цикл (опрос и очередь) и выход")
    watch.add_argument(
        "--webhook",
        action="store_true",
        help="принимать вебхуки TFS (нужен ARIO_WEBHOOK_SECRET); опрос остаётся запасным путём",
    )
    watch.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что будет взято и что в очереди, ничего не записывая и не запуская",
    )
    watch.add_argument(
        "--interval",
        type=int,
        default=None,
        help="минут между опросами (по умолчанию ARIO_POLL_MINUTES)",
    )
    rework = sub.add_parser(
        "rework", help="доработка завершённого тикета по замечаниям человека (к PR)"
    )
    rework.add_argument("ticket_id")
    rework.add_argument(
        "--note", action="append", default=[], help="текст замечания (можно несколько)"
    )
    rework.add_argument(
        "--file",
        type=Path,
        action="append",
        default=[],
        help="файл с замечаниями (можно несколько)",
    )
    rework.add_argument(
        "--from-pr", action="store_true", help="взять новые замечания из обсуждения PR тикета"
    )
    rework.add_argument(
        "--dry-run", action="store_true", help="показать замечания и выйти, ничего не запуская"
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


def _tfs_missing(settings: Settings) -> list[str]:
    return [
        name
        for name, value in (
            ("TFS_URL", settings.tfs_url),
            ("TFS_PROJECT", settings.tfs_project),
            ("TFS_PAT", settings.tfs_pat),
        )
        if not value
    ]


def _show_fetch_item(item: Any) -> None:
    # заголовок тикета недоверенный: управляющие символы терминала не выводим
    detail = _CONTROL_CHARS.sub("?", item.detail)
    print(f"[{_FETCH_MARKS[item.status]}] {item.ticket_id}: {detail}", flush=True)


def _pull(
    settings: Settings,
    store: TicketStore,
    masker: Masker,
    *,
    explicit_ids: list[int] | None = None,
    dry_run: bool = False,
    ledger: SeenLedger | None = None,
    skip_seen: bool = False,
) -> tuple[FetchReport, list[int]]:
    """Забирает тикеты из TFS в очередь: единый путь для `fetch` и `watch`.

    Ошибки TFS поднимаются исключением. `ledger` запоминает взятые и уже имевшиеся тикеты,
    `skip_seen` не берёт тикеты из журнала (так делает `watch`, `fetch --id` их возвращает).
    """
    from ario.tfs.client import TfsClient
    from ario.tfs.fetch import fetch_tickets, query_ids

    with TfsClient(
        settings.tfs_url or "", settings.tfs_project or "", settings.tfs_pat or ""
    ) as client:
        ids = explicit_ids if explicit_ids else query_ids(client, settings.tfs_wiql)
        candidates = ids
        if skip_seen and ledger is not None and not explicit_ids:
            seen = ledger.load()
            candidates = [i for i in ids if str(i) not in seen]
        report = fetch_tickets(
            client,
            candidates,
            store,
            limit=None if explicit_ids else settings.tfs_max_items,
            dry_run=dry_run,
            mask=masker.mask_text,
            on_item=_show_fetch_item,
        )
    if ledger is not None and not dry_run:
        ledger.add(i.ticket_id for i in report.items if i.status in ("fetched", "skipped"))
    return report, ids


def _watch_dry_run(settings: Settings, store: TicketStore, ledger: SeenLedger) -> int:
    """Проверка опроса без последствий: что взял бы TFS-запрос и что уже стоит в очереди."""
    from ario.tfs.client import TfsError

    masker = Masker([settings.tfs_pat or ""])
    try:
        report, _ = _pull(settings, store, masker, dry_run=True, ledger=ledger, skip_seen=True)
    except (TfsError, ValueError) as e:
        print(f"Ошибка: {masker.mask_text(str(e))}", file=sys.stderr)
        return 2
    queue = [*store.list_ids("in-progress"), *store.list_ids("inbox")]
    print(
        f"\nБудет взято {len(report.of('would-fetch'))}; уже в очереди: "
        f"{', '.join(queue) if queue else 'пусто'}. Ничего не записано и не запущено."
    )
    return 0


def _build_orchestrator(
    settings: Settings, repo_override: str | None
) -> tuple[Orchestrator, Masker]:
    """Оркестратор с агентами и отчётом на доске: общая сборка для `run`, `resume` и `watch`."""
    backend = OpenHandsBackend(
        settings.llm_model, settings.openrouter_api_key, role_models=settings.role_models
    )
    stages = build_pipeline(backend)
    guardrails = Guardrails.load(settings.guardrails_path)
    if settings.tfs_write:
        _use_system_certificates()  # корпоративный CA нужен уже на первом переводе состояния
        if (problem := write_config_problem(settings)) is not None:
            print(f"Предупреждение: {problem}; запись на доску отключена", file=sys.stderr)
    masker = Masker.from_environment(
        os.environ,
        guardrails.config.env,
        [settings.openrouter_api_key, settings.tfs_pat, settings.tfs_write_pat],
    )
    reporter = build_reporter(settings, masker.mask_text)
    for problem in reporter.problems(settings.work_item_types):
        print(f"Предупреждение: поток состояний: {problem}", file=sys.stderr)
    orchestrator = Orchestrator(
        settings, stages, guardrails, repo_override=repo_override, observer=reporter
    )
    return orchestrator, masker


def _gather_pr_remarks(settings: Settings, ticket_id: str) -> tuple[str, dict[str, int]]:
    """Новые замечания из обсуждения PR тикета и обновление журнала учтённых комментариев.

    Исключения: `ValueError` (нет PR, нет блока publish, нет токена), `TfsError`, `ConfigError`,
    `RepoSelectionError`. Пустой текст: новых замечаний нет.
    """
    from ario.pr_remarks import collect_remarks, load_handled
    from ario.publish import AGENT_REPLY_MARK, default_client, read_publication
    from ario.repos import RepoRegistry
    from ario.state import StateStore

    run_dir = settings.runs_dir / ticket_id
    state = StateStore(settings.runs_dir).load(ticket_id)
    publication = read_publication(run_dir)
    if state is None or not publication or not publication.get("pr_id"):
        raise ValueError("у тикета нет PR (runs/<id>/publish.json): сначала публикация")
    registry = RepoRegistry.from_settings(settings)
    profile = registry.get(state.repo) if state.repo else registry.default
    config = profile.publish if profile is not None else None
    if config is None:
        raise ValueError("в профиле репозитория нет блока publish")
    token = os.environ.get(config.pat_env, "").strip()
    if not token:
        raise ValueError(f"не задан токен {config.pat_env}")
    _use_system_certificates()
    pr_id = int(publication["pr_id"])
    with default_client(config, token) as client:
        threads = client.pull_request_threads(config.repository, pr_id)
    return collect_remarks(threads, pr_id, load_handled(run_dir), agent_mark=AGENT_REPLY_MARK)


def _rework_for_pr(
    settings: Settings, store: TicketStore, orchestrator: Orchestrator, pr_id: int
) -> str | None:
    """Доработка по сигналу вебхука: только для нашего PR и тикета в `done/`, и только если в
    обсуждении есть новые замечания. `None`: делать нечего."""
    from ario.pr_remarks import find_ticket_by_pr, save_handled

    ticket_id = find_ticket_by_pr(settings.runs_dir, pr_id)
    if ticket_id is None:
        print(f"PR {pr_id} не относится к нашим тикетам, пропущено", flush=True)
        return None
    if store.locate(ticket_id) != "done":
        print(f"Тикет {ticket_id} не в done/, доработка по PR {pr_id} пропущена", flush=True)
        return None
    text, handled = _gather_pr_remarks(settings, ticket_id)
    if not text:
        print(f"PR {pr_id}: новых замечаний нет", flush=True)
        return None
    run_dir = settings.runs_dir / ticket_id
    outcome = orchestrator.rework(
        ticket_id,
        text,
        on_recorded=lambda: save_handled(run_dir, handled) if handled else None,
    )
    print(f"Тикет {ticket_id}: доработка по PR {pr_id}: {outcome}", flush=True)
    _auto_export(settings, ticket_id)
    return str(outcome)


def _watch(settings: Settings, args: argparse.Namespace) -> int:
    import signal
    import threading

    from ario.fsutil import RunLock
    from ario.watch import SeenLedger, Watcher
    from ario.webhook import WebhookServer, check_secret

    missing = _tfs_missing(settings)
    if missing:
        print(f"Ошибка настройки: не заданы {', '.join(missing)}", file=sys.stderr)
        return 2
    interval = args.interval if args.interval is not None else settings.poll_minutes
    if interval < 1:
        print("Ошибка: интервал опроса — не меньше 1 минуты", file=sys.stderr)
        return 2
    if args.webhook:
        try:
            check_secret(settings.webhook_secret)
        except ValueError as e:
            print(f"Ошибка настройки: {e}", file=sys.stderr)
            return 2
    _use_system_certificates()
    store = TicketStore(settings.tickets_dir)
    ledger = SeenLedger(settings.data_dir / "watch-seen.json")
    if args.dry_run:
        return _watch_dry_run(settings, store, ledger)
    stop = threading.Event()

    def on_interrupt(signum: int, frame: Any) -> None:
        if stop.is_set():  # второй Ctrl+C: остановить немедленно
            raise KeyboardInterrupt
        stop.set()
        print("Остановка: текущий тикет дорабатывает, новые не берутся (Ctrl+C ещё раз: сразу)")

    previous = signal.signal(signal.SIGINT, on_interrupt)
    server: WebhookServer | None = None
    try:
        with RunLock(settings.runs_dir / "_watch"):  # до сборки агентов: второй наблюдатель отказ
            try:
                orchestrator, masker = _build_orchestrator(settings, None)
            except ConfigError as e:
                print(f"Ошибка: {e}", file=sys.stderr)
                return 2

            def fetch() -> int:
                report, _ = _pull(settings, store, masker, ledger=ledger, skip_seen=True)
                return len(report.of("fetched"))

            def process(ticket_id: str) -> str:
                outcome = orchestrator.run(ticket_id)
                print(f"Тикет {ticket_id}: {outcome}", flush=True)
                _auto_export(settings, ticket_id)
                return str(outcome)

            def rework(pr_id: int) -> str | None:
                return _rework_for_pr(settings, store, orchestrator, pr_id)

            watcher = Watcher(
                store=store,
                fetch=fetch,
                process=process,
                rework=rework,
                interval_seconds=interval * 60,
                mask=masker.mask_text,
                stop=stop,
                log=lambda line: print(line, flush=True),
            )
            if args.webhook:
                try:
                    server = WebhookServer(
                        host=settings.webhook_host,
                        port=settings.webhook_port,
                        secret=settings.webhook_secret or "",
                        on_signal=watcher.notify,
                        log=lambda line: print(line, flush=True),
                    )
                except OSError as e:  # например, порт занят: только создание сервера
                    print(f"Ошибка: приёмник вебхука не запущен: {e}", file=sys.stderr)
                    return 2
                server.start()
                print(f"Вебхук слушает {settings.webhook_host}:{server.port} {DEFAULT_PATH}")
                if settings.webhook_host not in ("127.0.0.1", "localhost", "::1"):
                    print(
                        "Предупреждение: приёмник доступен не только с этой машины: нужен "
                        "TLS-прокси или закрытая сеть (пароль Basic идёт открытым текстом)",
                        file=sys.stderr,
                    )
            print(f"ario watch: опрос каждые {interval} мин, очередь tickets/in-progress, inbox")
            watcher.run(once=args.once)
    except RunInProgressError:
        print("Ошибка: другой ario watch уже запущен (блокировка runs/_watch)", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.stop()
        signal.signal(signal.SIGINT, previous)
    return 0


def _rework(settings: Settings, args: argparse.Namespace) -> int:
    """Доработка по замечаниям: из аргументов, файлов и обсуждения PR."""
    from ario.orchestrator import ReworkLimitError, TicketNotReworkableError
    from ario.pr_remarks import save_handled
    from ario.repos import RepoSelectionError
    from ario.tfs.client import TfsError

    run_dir = settings.runs_dir / args.ticket_id
    parts: list[str] = [note.strip() for note in args.note if note.strip()]
    for path in args.file:
        try:
            parts.append(path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as e:  # файл не в UTF-8 тоже не трейсбек
            print(f"Ошибка: не прочитан файл замечаний {path}: {e}", file=sys.stderr)
            return 2
    handled_update: dict[str, int] = {}
    if args.from_pr:
        try:
            text, handled_update = _gather_pr_remarks(settings, args.ticket_id)
        except (ValueError, TfsError, ConfigError, RepoSelectionError) as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 2
        if text:
            parts.append(text)
    if not parts:
        print(
            "Ошибка: нет замечаний: укажите --note, --file или --from-pr "
            "(новых замечаний в PR нет)",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        # замечания недоверенный текст: управляющие символы терминала не выводим
        print(_CONTROL_CHARS.sub("?", "\n\n".join(parts)))
        print("\nДоработка не запущена (--dry-run).")
        return 0
    try:
        orchestrator, _ = _build_orchestrator(settings, None)
        outcome = orchestrator.rework(
            args.ticket_id,
            "\n\n".join(parts),
            on_recorded=lambda: save_handled(run_dir, handled_update) if handled_update else None,
        )
    except (
        ConfigError,
        TicketNotFoundError,
        InvalidTicketIdError,
        TicketNotReworkableError,
        ReworkLimitError,
        RunInProgressError,
    ) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    except StageFailed as e:  # замечания не прошли проверку injection
        print(f"Ошибка: {e.reason}", file=sys.stderr)
        return 2
    except TicketNotTakenError as e:
        print(f"Доработка тикета {args.ticket_id} не начата: {e.reason}", file=sys.stderr)
        return 3
    print(f"Тикет {args.ticket_id}: {outcome}")
    _auto_export(settings, args.ticket_id)
    return 0 if outcome == "done" else 1


def _fetch(settings: Settings, args: argparse.Namespace) -> int:
    from ario.tfs.client import TfsError
    from ario.tfs.fetch import CANDIDATES_LIMIT
    from ario.watch import SeenLedger

    missing = _tfs_missing(settings)
    if missing:
        print(f"Ошибка настройки: не заданы {', '.join(missing)}", file=sys.stderr)
        return 2
    masker = Masker([settings.tfs_pat or ""])
    _use_system_certificates()
    store = TicketStore(settings.tickets_dir)
    explicit: list[int] | None = args.id
    try:
        report, ids = _pull(
            settings,
            store,
            masker,
            explicit_ids=explicit,
            dry_run=args.dry_run,
            ledger=SeenLedger(settings.data_dir / "watch-seen.json"),
        )
    except (TfsError, ValueError) as e:
        print(f"Ошибка: {masker.mask_text(str(e))}", file=sys.stderr)
        return 2
    if not ids:
        print("Подходящих тикетов в TFS нет")
        return 0
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
    if args.command in ("run", "resume", "eval", "watch", "rework"):
        try:
            check_sdk_telemetry(os.environ)  # SDK не должен слать тексты запросов наружу
        except ConfigError as e:
            print(f"Ошибка настройки: {e}", file=sys.stderr)
            return 2
    uses_model = args.command in ("run", "resume", "eval", "watch", "rework") and not getattr(
        args, "dry_run", False
    )  # `watch --dry-run` модель не вызывает: выгрузка запросов ему не нужна
    llm_content = uses_model and settings.otlp_llm_content
    if llm_content and not _enable_llm_tracing(settings):
        return 2
    if args.command == "trace":
        return _trace(settings, args)
    if args.command == "eval":
        return _eval(settings, args)
    if args.command == "fetch":
        return _fetch(settings, args)
    if args.command == "watch":
        return _watch(settings, args)
    if args.command == "rework":
        return _rework(settings, args)
    if args.command == "export-otel":
        return _export_otel(settings, args)
    try:
        orchestrator, _ = _build_orchestrator(settings, getattr(args, "repo", None))
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
