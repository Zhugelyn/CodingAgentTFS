"""Автоматический режим (S18): очередь inbox/, опрос TFS, остановка, команда `ario watch`."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ario.cli as cli
from ario.config import Settings
from ario.fsutil import RunLock
from ario.orchestrator import TicketNotTakenError
from ario.store import TicketStore
from ario.watch import Watcher
from tests.conftest import REPO_ROOT


def put(store: TicketStore, ticket_id: str, age: int) -> None:
    """Кладёт тикет в inbox; чем больше `age`, тем он старше."""
    store.ensure_dirs()
    path = store.path(ticket_id, "inbox")
    path.write_text(f"id: {ticket_id}\n", encoding="utf-8")
    stamp = 1_700_000_000 - age
    os.utime(path, (stamp, stamp))


class Queue:
    """Обработчик-заглушка: переносит тикет в done и запоминает порядок."""

    def __init__(self, store: TicketStore) -> None:
        self.store = store
        self.order: list[str] = []
        self.outcomes: dict[str, str] = {}

    def __call__(self, ticket_id: str) -> str:
        self.order.append(ticket_id)
        outcome = self.outcomes.get(ticket_id, "done")
        self.store.move(ticket_id, "inbox", "done" if outcome == "done" else "needs-human")
        return outcome


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    result = TicketStore(tmp_path / "tickets")
    result.ensure_dirs()
    return result


def watcher(
    store: TicketStore,
    process: Any,
    fetch: Any = lambda: 0,
    log: list[str] | None = None,
    stop: threading.Event | None = None,
    interval: float = 60.0,
) -> Watcher:
    lines = log if log is not None else []
    return Watcher(
        store=store,
        fetch=fetch,
        process=process,
        interval_seconds=interval,
        log=lines.append,
        mask=lambda text: text.replace("СЕКРЕТ", "***"),
        stop=stop,
    )


# --- очередь ----------------------------------------------------------------------------------
def test_queue_is_oldest_first_and_ignores_comment_files(store: TicketStore) -> None:
    put(store, "3", age=10)
    put(store, "1", age=300)
    put(store, "2", age=100)
    store.comments_path("1", "inbox").write_text("комментарий", encoding="utf-8")
    (store.path("x", "inbox").parent / "bad name.md").write_text("-", encoding="utf-8")
    assert store.list_ids("inbox") == ["1", "2", "3"]
    assert store.list_ids("done") == []


def test_cycle_fetches_then_processes_the_queue_in_order(store: TicketStore) -> None:
    put(store, "20", age=5)
    put(store, "10", age=50)
    queue = Queue(store)
    log: list[str] = []
    result = watcher(store, queue, fetch=lambda: 2, log=log).cycle()
    assert queue.order == ["10", "20"]
    assert result.fetched == 2 and result.processed == [("10", "done"), ("20", "done")]
    assert any("новых тикетов 2" in line for line in log)
    assert store.list_ids("inbox") == []


def test_tickets_that_need_a_human_do_not_block_the_next_ones(store: TicketStore) -> None:
    put(store, "1", age=20)
    put(store, "2", age=10)
    queue = Queue(store)
    queue.outcomes["1"] = "needs-human"
    result = watcher(store, queue).cycle()
    assert result.processed == [("1", "needs-human"), ("2", "done")]


# --- сбои -------------------------------------------------------------------------------------
def test_failed_poll_still_processes_what_is_already_queued(store: TicketStore) -> None:
    put(store, "7", age=1)

    def broken() -> int:
        raise RuntimeError("TFS упал, токен СЕКРЕТ")

    queue = Queue(store)
    log: list[str] = []
    result = watcher(store, queue, fetch=broken, log=log).cycle()
    assert result.fetched is None and queue.order == ["7"]
    joined = "\n".join(log)
    assert "Опрос TFS не удался" in joined and "СЕКРЕТ" not in joined  # секрет замаскирован


def test_a_ticket_that_was_not_taken_pauses_the_queue_until_the_next_cycle(
    store: TicketStore,
) -> None:
    put(store, "1", age=20)
    put(store, "2", age=10)
    calls: list[str] = []
    blocked = {"on": True}

    def process(ticket_id: str) -> str:
        calls.append(ticket_id)
        if blocked["on"]:
            raise TicketNotTakenError("в RX чужие правки")
        store.move(ticket_id, "inbox", "done")
        return "done"

    w = watcher(store, process)
    first = w.cycle()
    assert calls == ["1"] and first.blocked == "в RX чужие правки"  # второй даже не пробовали
    assert store.list_ids("inbox") == ["1", "2"]
    blocked["on"] = False  # человек закоммитил правки
    second = w.cycle()
    assert second.processed == [("1", "done"), ("2", "done")]


def test_a_crashing_ticket_is_tried_once_per_cycle_and_does_not_stop_the_others(
    store: TicketStore,
) -> None:
    put(store, "1", age=20)  # остаётся в inbox после сбоя
    put(store, "2", age=10)
    calls: list[str] = []

    def process(ticket_id: str) -> str:
        calls.append(ticket_id)
        if ticket_id == "1":
            raise RuntimeError("неожиданный сбой")
        store.move(ticket_id, "inbox", "done")
        return "done"

    log: list[str] = []
    result = watcher(store, process, log=log).cycle()
    assert calls == ["1", "2"]  # без зацикливания на сломанном тикете
    assert result.processed == [("2", "done")]
    assert any("ошибка RuntimeError" in line for line in log)


# --- остановка и цикл ---------------------------------------------------------------------------
def test_stop_between_tickets_lets_the_current_one_finish(store: TicketStore) -> None:
    put(store, "1", age=20)
    put(store, "2", age=10)
    stop = threading.Event()
    queue = Queue(store)

    def process(ticket_id: str) -> str:
        outcome = queue(ticket_id)
        stop.set()  # попросили остановиться посреди первого тикета
        return outcome

    result = watcher(store, process, stop=stop).cycle()
    assert queue.order == ["1"] and result.processed == [("1", "done")]
    assert store.list_ids("inbox") == ["2"]  # второй остался в очереди


def test_once_runs_a_single_cycle(store: TicketStore) -> None:
    fetched: list[int] = []

    def fetch() -> int:
        fetched.append(1)
        return 0

    watcher(store, Queue(store), fetch=fetch).run(once=True)
    assert fetched == [1]


def test_run_waits_between_cycles_and_stops_during_the_wait(store: TicketStore) -> None:
    stop = threading.Event()
    cycles: list[int] = []

    def fetch() -> int:
        cycles.append(1)
        if len(cycles) == 3:
            stop.set()
        return 0

    log: list[str] = []
    watcher(store, Queue(store), fetch=fetch, log=log, stop=stop, interval=0.01).run()
    assert len(cycles) == 3 and any("Следующий опрос" in line for line in log)


def test_stop_set_during_the_wait_ends_the_loop_quickly(store: TicketStore) -> None:
    stop = threading.Event()
    w = watcher(store, Queue(store), stop=stop, interval=3600)  # час ожидания
    timer = threading.Timer(0.3, stop.set)  # попросили остановиться уже ВО ВРЕМЯ паузы
    started = time.monotonic()
    timer.start()
    w.run()
    timer.join()
    assert time.monotonic() - started < 10  # не ждёт час


def test_in_progress_tickets_left_after_a_crash_are_resumed_first(store: TicketStore) -> None:
    store.ensure_dirs()
    stuck = store.path("5", "in-progress")
    stuck.write_text("id: 5\n", encoding="utf-8")  # остался после убитого процесса
    put(store, "6", age=1)
    order: list[str] = []

    def process(ticket_id: str) -> str:
        order.append(ticket_id)
        folder = "in-progress" if ticket_id == "5" else "inbox"
        store.move(ticket_id, folder, "done")  # type: ignore[arg-type]
        return "done"

    result = watcher(store, process).cycle()
    assert order == ["5", "6"] and len(result.processed) == 2


def test_ledger_survives_clearing_the_folders(tmp_path: Path) -> None:
    from ario.watch import SeenLedger

    ledger = SeenLedger(tmp_path / "seen.json")
    assert ledger.load() == set()
    ledger.add(["60010", "52042"])
    ledger.add(["52042", "7"])
    assert SeenLedger(tmp_path / "seen.json").load() == {"60010", "52042", "7"}
    (tmp_path / "seen.json").write_text("не json", encoding="utf-8")
    assert ledger.load() == set()  # битый журнал не роняет наблюдателя


# --- команда ------------------------------------------------------------------------------------
@pytest.fixture
def cli_env(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[str], Settings]:
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("ARIO_GUARDRAILS", str(REPO_ROOT / "config" / "guardrails.yaml"))
    monkeypatch.setattr(cli, "_use_system_certificates", lambda: None)
    return ["--env-file", str(empty)], settings


def set_tfs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFS_URL", "https://tfs.example.test/c")
    monkeypatch.setenv("TFS_PROJECT", "P")
    monkeypatch.setenv("TFS_PAT", "pat")


def test_watch_needs_tfs_settings_and_a_sane_interval(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, _ = cli_env
    assert cli.main([*base, "watch", "--once"]) == 2
    assert "TFS_URL" in capsys.readouterr().err
    set_tfs(monkeypatch)
    assert cli.main([*base, "watch", "--once", "--interval", "0"]) == 2
    assert "не меньше 1 минуты" in capsys.readouterr().err


def test_watch_once_fetches_and_processes_through_the_orchestrator(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, settings = cli_env
    set_tfs(monkeypatch)
    store = TicketStore(settings.tickets_dir)

    class FakeOrchestrator:
        def run(self, ticket_id: str) -> str:
            store.move(ticket_id, "inbox", "done")
            return "done"

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None: ...

    class Report:
        items = [SimpleNamespace(ticket_id="90001", status="fetched")]

        def of(self, status: str) -> list[int]:
            return [1] if status == "fetched" else []

    def fake_fetch(client: Any, ids: Any, store_: TicketStore, **kwargs: Any) -> Report:
        put(store_, "90001", age=1)
        return Report()

    from ario.trace import Masker

    monkeypatch.setattr(cli, "_build_orchestrator", lambda s, o: (FakeOrchestrator(), Masker([])))
    monkeypatch.setattr("ario.tfs.client.TfsClient", FakeClient)
    monkeypatch.setattr("ario.tfs.fetch.query_ids", lambda client, wiql: [90001])
    monkeypatch.setattr("ario.tfs.fetch.fetch_tickets", fake_fetch)
    assert cli.main([*base, "watch", "--once"]) == 0
    out = capsys.readouterr().out
    assert "Опрос TFS: новых тикетов 1" in out and "Тикет 90001: done" in out
    assert store.locate("90001") == "done"


def test_second_watcher_is_refused_while_the_first_is_running(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, settings = cli_env
    set_tfs(monkeypatch)
    from ario.trace import Masker

    monkeypatch.setattr(cli, "_build_orchestrator", lambda s, o: (object(), Masker([])))
    with RunLock(settings.runs_dir / "_watch"):
        assert cli.main([*base, "watch", "--once"]) == 2
    assert "уже запущен" in capsys.readouterr().err


def test_watch_dry_run_shows_the_plan_and_starts_nothing(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, settings = cli_env
    set_tfs(monkeypatch)
    store = TicketStore(settings.tickets_dir)
    put(store, "555", age=1)  # уже стоит в очереди
    seen: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None: ...

    class Report:
        def of(self, status: str) -> list[int]:
            return [1] if status == "would-fetch" else []

    def fake_fetch(client: Any, ids: Any, store_: Any, **kwargs: Any) -> Report:
        seen.update(kwargs)
        return Report()

    def must_not_build(*args: Any) -> None:
        raise AssertionError("агенты в dry-run не собираются")

    monkeypatch.setattr(cli, "_build_orchestrator", must_not_build)
    monkeypatch.setattr("ario.tfs.client.TfsClient", FakeClient)
    monkeypatch.setattr("ario.tfs.fetch.query_ids", lambda client, wiql: [1])
    monkeypatch.setattr("ario.tfs.fetch.fetch_tickets", fake_fetch)
    assert cli.main([*base, "watch", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert seen["dry_run"] is True
    assert "Будет взято 1" in out and "555" in out and "Ничего не записано" in out
    assert store.list_ids("inbox") == ["555"]  # очередь не тронута


def test_watch_does_not_take_a_ticket_again_after_its_files_were_removed(
    cli_env: tuple[list[str], Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ario.trace import Masker
    from ario.watch import SeenLedger

    base, settings = cli_env
    set_tfs(monkeypatch)
    SeenLedger(settings.data_dir / "watch-seen.json").add(["60010"])  # брали раньше, файлы убрали
    asked: list[list[int]] = []

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None: ...

    class Report:
        items: list[Any] = []

        def of(self, status: str) -> list[int]:
            return []

    def fake_fetch(client: Any, ids: Any, store_: Any, **kwargs: Any) -> Report:
        asked.append(list(ids))
        return Report()

    monkeypatch.setattr(cli, "_build_orchestrator", lambda s, o: (object(), Masker([])))
    monkeypatch.setattr("ario.tfs.client.TfsClient", FakeClient)
    monkeypatch.setattr("ario.tfs.fetch.query_ids", lambda client, wiql: [60010, 70001])
    monkeypatch.setattr("ario.tfs.fetch.fetch_tickets", fake_fetch)
    assert cli.main([*base, "watch", "--once"]) == 0
    assert asked == [[70001]]  # 60010 из журнала не запрашивается снова


def test_guard_wait_is_sliced_so_ctrl_c_is_not_blocked_for_minutes() -> None:
    from ario.llm_guard import _wait

    never = threading.Event()
    started = time.monotonic()
    assert _wait(never, 0.3, slice_seconds=0.05) is False
    assert 0.25 < time.monotonic() - started < 3
    done = threading.Event()
    done.set()
    assert _wait(done, 5, slice_seconds=0.05) is True
