# ruff: noqa: F811, F401  (фикстуры подключаются импортом из test_watch)
"""Вебхук TFS (S21): разбор событий, защита приёмника, пробуждение наблюдателя."""

from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import ario.cli as cli
from ario.config import Settings
from ario.pr_remarks import find_ticket_by_pr
from ario.store import TicketStore
from ario.watch import Watcher
from ario.webhook import (
    DEFAULT_PATH,
    MIN_SECRET_LENGTH,
    Signal,
    WebhookServer,
    check_secret,
    classify,
)
from tests.test_watch import Queue, cli_env, put, set_tfs, store, watcher

SECRET = "очень-длинный-секрет-вебхука"


def pr_event(
    vote: int | None, status: str = "active", event: str = "git.pullrequest.updated"
) -> dict[str, Any]:
    reviewers = [{"displayName": "Ревьюер", "vote": vote}] if vote is not None else []
    return {
        "eventType": event,
        "resource": {"pullRequestId": 73651, "status": status, "reviewers": reviewers},
    }


# --- разбор событий ----------------------------------------------------------------------------
@pytest.mark.parametrize("event", ["workitem.created", "workitem.updated"])
def test_work_item_events_request_a_poll(event: str) -> None:
    assert classify({"eventType": event, "resource": {"id": 1}}) == Signal("poll", detail=event)


@pytest.mark.parametrize(
    "payload",
    [
        {"eventType": "workitem.commented"},
        {"eventType": "git.push", "resource": {}},
        {"eventType": 5},
        {"resource": {}},
        [],
        None,
        "строка",
    ],
)
def test_other_events_and_garbage_are_ignored(payload: Any) -> None:
    assert classify(payload) is None


@pytest.mark.parametrize("vote", [-5, -10])
def test_returning_a_pull_request_to_the_author_requests_rework(vote: int) -> None:
    signal = classify(pr_event(vote))
    assert signal == Signal("rework", 73651, "git.pullrequest.updated")
    assert classify(pr_event(vote, event="git.pullrequest.created")) is not None


@pytest.mark.parametrize("vote", [0, 5, 10, None])
def test_votes_that_are_not_a_return_do_not_start_rework(vote: int | None) -> None:
    assert classify(pr_event(vote)) is None


def test_closed_or_malformed_pull_request_events_are_ignored() -> None:
    assert classify(pr_event(-5, status="completed")) is None
    assert classify(pr_event(-5, status="abandoned")) is None
    broken = pr_event(-5)
    broken["resource"]["pullRequestId"] = True  # bool не номер
    assert classify(broken) is None
    broken["resource"]["pullRequestId"] = "73651"
    assert classify(broken) is None


def test_secret_is_required_and_not_short() -> None:
    for bad in (None, "", "x" * (MIN_SECRET_LENGTH - 1)):
        with pytest.raises(ValueError, match="ARIO_WEBHOOK_SECRET"):
            check_secret(bad)
    assert check_secret(SECRET) == SECRET


# --- приёмник -------------------------------------------------------------------------------------
@pytest.fixture
def server() -> Iterator[tuple[WebhookServer, list[Signal], list[str]]]:
    signals: list[Signal] = []
    log: list[str] = []
    srv = WebhookServer(
        host="127.0.0.1", port=0, secret=SECRET, on_signal=signals.append, log=log.append
    )
    srv.start()
    yield srv, signals, log
    srv.stop()


def auth(password: str, user: str = "tfs") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def post(
    port: int,
    body: Any,
    *,
    path: str = DEFAULT_PATH,
    authorization: str | None = None,
    method: str = "POST",
) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    data = json.dumps(body).encode() if not isinstance(body, bytes) else body
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    result = (response.status, response.read().decode("utf-8"))
    conn.close()
    return result


def test_authorized_work_item_event_is_accepted_and_signalled(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, log = server
    status, body = post(
        srv.port, {"eventType": "workitem.updated"}, authorization=auth(SECRET, user="любое")
    )
    assert status == 200 and json.loads(body) == {"ok": True}
    assert signals == [Signal("poll", detail="workitem.updated")]
    assert any("workitem.updated" in line and "poll" in line for line in log)


def test_missing_or_wrong_password_is_rejected_without_a_signal(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, log = server
    event = {"eventType": "workitem.updated"}
    assert post(srv.port, event)[0] == 401
    assert post(srv.port, event, authorization=auth("не-тот-пароль-совсем"))[0] == 401
    assert post(srv.port, event, authorization="Basic not-base64!!")[0] == 401
    assert post(srv.port, event, authorization="Bearer token")[0] == 401
    assert signals == []
    text = "\n".join(log)
    assert "авторизация" in text and SECRET not in text and "не-тот-пароль" not in text


def test_only_post_on_the_configured_path(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, _ = server
    ok = auth(SECRET)
    assert post(srv.port, {}, path="/other", authorization=ok)[0] == 404
    assert post(srv.port, b"", authorization=ok, method="GET")[0] == 405
    assert post(srv.port, {}, authorization=ok, method="DELETE")[0] == 405
    assert signals == []


def test_malformed_and_oversized_bodies_are_refused(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, _ = server
    ok = auth(SECRET)
    assert post(srv.port, b"{not json", authorization=ok)[0] == 400
    assert post(srv.port, [1, 2, 3], authorization=ok)[0] == 400  # не объект
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.putrequest("POST", DEFAULT_PATH)
    conn.putheader("Authorization", ok)
    conn.putheader("Content-Length", str(5_000_000))
    conn.endheaders()  # тело не отправляем: сервер должен отказать до чтения
    assert conn.getresponse().status == 413
    conn.close()
    assert signals == []


def test_ignored_events_get_200_and_unprintable_names_are_sanitized(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, log = server
    status, _ = post(srv.port, {"eventType": "git.push\x1b[31mКРАСНЫЙ"}, authorization=auth(SECRET))
    assert status == 200 and signals == []  # TFS не должен считать подписку сбойной
    line = next(entry for entry in log if "игнорируется" in entry)
    assert "\x1b" not in line


def test_pull_request_return_is_signalled_with_its_number(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, _ = server
    assert post(srv.port, pr_event(-5), authorization=auth(SECRET))[0] == 200
    assert signals == [Signal("rework", 73651, "git.pullrequest.updated")]


def test_server_stops_and_releases_the_port() -> None:
    srv = WebhookServer(host="127.0.0.1", port=0, secret=SECRET, on_signal=lambda s: None)
    port = srv.port
    srv.start()
    srv.stop()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # порт свободен снова


def test_server_refuses_to_start_without_a_proper_secret() -> None:
    with pytest.raises(ValueError, match="ARIO_WEBHOOK_SECRET"):
        WebhookServer(host="127.0.0.1", port=0, secret="короткий", on_signal=lambda s: None)


# --- наблюдатель --------------------------------------------------------------------------------
def test_a_signal_wakes_the_watcher_before_the_interval_ends(store: TicketStore) -> None:
    cycles: list[float] = []
    started = threading.Event()

    def fetch() -> int:
        cycles.append(time.monotonic())
        started.set()
        return 0

    w = watcher(store, Queue(store), fetch=fetch, interval=3600)  # без сигнала ждал бы час
    thread = threading.Thread(target=w.run, daemon=True)
    thread.start()
    assert started.wait(10)
    time.sleep(0.2)  # наблюдатель уже ждёт следующий цикл
    w.notify(Signal("poll", detail="workitem.updated"))
    deadline = time.monotonic() + 10
    while len(cycles) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    w.stop.set()
    thread.join(timeout=10)
    assert len(cycles) >= 2  # второй цикл прошёл по сигналу, а не по истечении часа


def test_rework_requests_for_one_pull_request_are_merged(store: TicketStore) -> None:
    calls: list[int] = []

    def rework(pr_id: int) -> str | None:
        calls.append(pr_id)
        return "done" if pr_id == 7 else None  # по PR 8 новых замечаний нет

    w = Watcher(
        store=store,
        fetch=lambda: 0,
        process=Queue(store),
        interval_seconds=60,
        rework=rework,
        log=lambda line: None,
    )
    for _ in range(3):
        w.notify(Signal("rework", 7))
    w.notify(Signal("rework", 8))
    result = w.cycle()
    assert calls == [7, 8] and result.reworked == [(7, "done")]
    assert w.cycle().reworked == []  # запросы обработаны один раз


def test_rework_failure_and_missing_handler_do_not_stop_the_watcher(store: TicketStore) -> None:
    log: list[str] = []

    def broken(pr_id: int) -> str | None:
        raise RuntimeError("упало, токен СЕКРЕТ")

    w = Watcher(
        store=store,
        fetch=lambda: 0,
        process=Queue(store),
        interval_seconds=60,
        rework=broken,
        log=log.append,
        mask=lambda text: text.replace("СЕКРЕТ", "***"),
    )
    w.notify(Signal("rework", 7))
    assert w.cycle().reworked == []
    assert any("PR 7: ошибка RuntimeError" in line and "СЕКРЕТ" not in line for line in log)
    plain = Watcher(
        store=store, fetch=lambda: 0, process=Queue(store), interval_seconds=60, log=log.append
    )
    plain.notify(Signal("rework", 9))
    plain.cycle()
    assert any("не настроена" in line for line in log)


def test_stop_prevents_further_rework_requests(store: TicketStore) -> None:
    calls: list[int] = []

    def rework(pr_id: int) -> str | None:
        calls.append(pr_id)
        return "done"

    w = Watcher(
        store=store,
        fetch=lambda: 0,
        process=Queue(store),
        interval_seconds=60,
        rework=rework,
        log=lambda line: None,
    )
    w.notify(Signal("rework", 7))
    w.stop.set()
    w.cycle()
    assert calls == []


def test_pull_request_is_mapped_to_our_ticket(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    for name, content in (
        ("1001", json.dumps({"pr_id": 70001})),
        ("1002", json.dumps({"pr_id": 73651})),
        ("1003", "не json"),
    ):
        (runs / name).mkdir(parents=True)
        (runs / name / "publish.json").write_text(content, encoding="utf-8")
    assert find_ticket_by_pr(runs, 73651) == "1002"
    assert find_ticket_by_pr(runs, 99999) is None  # чужой PR
    assert find_ticket_by_pr(tmp_path / "нет", 1) is None


# --- команда ------------------------------------------------------------------------------------
def test_webhook_needs_a_secret(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, _ = cli_env
    set_tfs(monkeypatch)
    assert cli.main([*base, "watch", "--webhook", "--once"]) == 2
    assert "ARIO_WEBHOOK_SECRET" in capsys.readouterr().err
    monkeypatch.setenv("ARIO_WEBHOOK_SECRET", "короткий")
    assert cli.main([*base, "watch", "--webhook", "--once"]) == 2


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_watch_starts_and_stops_the_webhook_server(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ario.trace import Masker

    base, _ = cli_env
    set_tfs(monkeypatch)
    port = free_port()
    monkeypatch.setenv("ARIO_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ARIO_WEBHOOK_PORT", str(port))

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None: ...

    class Report:
        items: list[Any] = []

        def of(self, status: str) -> list[int]:
            return []

    monkeypatch.setattr(cli, "_build_orchestrator", lambda s, o: (object(), Masker([])))
    monkeypatch.setattr("ario.tfs.client.TfsClient", FakeClient)
    monkeypatch.setattr("ario.tfs.fetch.query_ids", lambda client, wiql: [])
    monkeypatch.setattr("ario.tfs.fetch.fetch_tickets", lambda *a, **k: Report())
    assert cli.main([*base, "watch", "--webhook", "--once"]) == 0
    out = capsys.readouterr().out
    assert f"Вебхук слушает 127.0.0.1:{port}" in out
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # после выхода порт свободен


def test_busy_webhook_port_is_a_clear_error(
    cli_env: tuple[list[str], Settings],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ario.trace import Masker

    base, _ = cli_env
    set_tfs(monkeypatch)
    monkeypatch.setenv("ARIO_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(cli, "_build_orchestrator", lambda s, o: (object(), Masker([])))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        monkeypatch.setenv("ARIO_WEBHOOK_PORT", str(busy.getsockname()[1]))
        assert cli.main([*base, "watch", "--webhook", "--once"]) == 2
    assert capsys.readouterr().err.strip()  # причина названа, без трейсбека


# --- доработка по сигналу PR -----------------------------------------------------------------
class ReworkOrchestrator:
    def __init__(self, outcome: str = "done") -> None:
        self.calls: list[tuple[str, str]] = []
        self.outcome = outcome

    def rework(self, ticket_id: str, remarks: str, on_recorded: Any = None) -> str:
        self.calls.append((ticket_id, remarks))
        if on_recorded is not None:
            on_recorded()
        return self.outcome


def prepare_pr(settings: Settings, ticket_id: str = "43031", pr_id: int = 73651) -> TicketStore:
    run_dir = settings.runs_dir / ticket_id
    run_dir.mkdir(parents=True)
    (run_dir / "publish.json").write_text(json.dumps({"pr_id": pr_id}), encoding="utf-8")
    store = TicketStore(settings.tickets_dir)
    put(store, ticket_id, age=1)
    store.move(ticket_id, "inbox", "done")
    return store


def test_pr_signal_starts_rework_only_with_new_remarks(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_pr(settings)
    orchestrator = ReworkOrchestrator()
    monkeypatch.setattr(cli, "_auto_export", lambda s, t: None)
    monkeypatch.setattr(cli, "_gather_pr_remarks", lambda s, t: ("Поправьте имя метода", {"1": 3}))
    assert cli._rework_for_pr(settings, store, orchestrator, 73651) == "done"  # type: ignore[arg-type]
    assert orchestrator.calls == [("43031", "Поправьте имя метода")]
    from ario.pr_remarks import load_handled

    assert load_handled(settings.runs_dir / "43031") == {"1": 3}  # учтённое запомнено


def test_pr_signal_without_new_remarks_does_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_pr(settings)
    orchestrator = ReworkOrchestrator()
    monkeypatch.setattr(cli, "_gather_pr_remarks", lambda s, t: ("", {}))
    assert cli._rework_for_pr(settings, store, orchestrator, 73651) is None  # type: ignore[arg-type]
    assert orchestrator.calls == []  # голос остался, а замечаний новых нет: доработки нет


def test_pr_signal_for_a_foreign_pr_or_an_unfinished_ticket_is_skipped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_pr(settings)
    orchestrator = ReworkOrchestrator()

    def must_not_be_called(s: Settings, t: str) -> tuple[str, dict[str, int]]:
        raise AssertionError("замечания не должны запрашиваться")

    monkeypatch.setattr(cli, "_gather_pr_remarks", must_not_be_called)
    assert cli._rework_for_pr(settings, store, orchestrator, 99999) is None  # type: ignore[arg-type]
    store.move("43031", "done", "needs-human")
    assert cli._rework_for_pr(settings, store, orchestrator, 73651) is None  # type: ignore[arg-type]
    assert orchestrator.calls == []


# --- исправления по ревью -----------------------------------------------------------------------
def test_a_stalled_connection_is_dropped_by_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ario.webhook.CONNECTION_TIMEOUT", 0.5)
    srv = WebhookServer(host="127.0.0.1", port=0, secret=SECRET, on_signal=lambda s: None)
    srv.start()
    try:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as conn:
            conn.sendall(
                f"POST {DEFAULT_PATH} HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n".encode()
                + f"Authorization: {auth(SECRET)}\r\n\r\n".encode()
            )  # обещали 500 байт тела и замолчали
            conn.settimeout(10)
            started = time.monotonic()
            data = conn.recv(1024)  # сервер сам обрывает по таймауту, а не ждёт вечно
            assert time.monotonic() - started < 8
            assert data == b"" or b"HTTP/1.0" in data or b"HTTP/1.1" in data
    finally:
        srv.stop()


def test_a_failing_signal_handler_gives_500_so_tfs_retries() -> None:
    def broken(signal: Signal) -> None:
        raise RuntimeError("обработчик упал")

    log: list[str] = []
    srv = WebhookServer(host="127.0.0.1", port=0, secret=SECRET, on_signal=broken, log=log.append)
    srv.start()
    try:
        status, _ = post(srv.port, {"eventType": "workitem.updated"}, authorization=auth(SECRET))
        assert status == 500
        assert any("сбой обработки сигнала" in line and "RuntimeError" in line for line in log)
    finally:
        srv.stop()


def test_query_string_does_not_change_the_path(
    server: tuple[WebhookServer, list[Signal], list[str]],
) -> None:
    srv, signals, _ = server
    status, _ = post(
        srv.port,
        {"eventType": "workitem.created"},
        path=DEFAULT_PATH + "?x=1",
        authorization=auth(SECRET),
    )
    assert status == 200 and len(signals) == 1


def test_webhook_port_must_be_in_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ario.config import ConfigError

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_WEBHOOK_PORT", "70000")
    with pytest.raises(ConfigError, match="65535"):
        Settings.load(empty)
    monkeypatch.setenv("ARIO_WEBHOOK_PORT", "8765")
    assert Settings.load(empty).webhook_port == 8765


def test_a_rework_signal_arriving_during_a_cycle_is_not_lost(store: TicketStore) -> None:
    calls: list[int] = []

    def rework(pr_id: int) -> str | None:
        calls.append(pr_id)
        return "done"

    holder: dict[str, Watcher] = {}

    def fetch() -> int:
        holder["w"].notify(Signal("rework", 7))  # сигнал приходит посреди цикла
        return 0

    w = Watcher(
        store=store,
        fetch=fetch,
        process=Queue(store),
        interval_seconds=3600,
        rework=rework,
        log=lambda line: None,
    )
    holder["w"] = w
    w.cycle()
    assert calls == []  # в этом цикле запрос уже не взят...
    w.cycle()
    assert calls == [7]  # ...но и не потерян: следующий цикл его обработает
