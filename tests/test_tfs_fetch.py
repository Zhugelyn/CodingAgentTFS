"""`ario fetch`: рендер тикета, запись в inbox, пропуски, лимит, dry-run, настройки и CLI."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ario.config import ConfigError, Settings
from ario.store import FOLDERS, TicketStore
from ario.tfs.client import TfsClient
from ario.tfs.fetch import FetchItem, default_wiql, fetch_tickets, query_ids
from ario.ticket import Comment, Ticket, load_ticket, parse_ticket, render_ticket
from tests.conftest import REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures" / "tfs"
PAT = "tfs-secret-pat-0123456789"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeTfs:
    """Подменяет сервер: work item с любым id строится из образца, лишнего не знает."""

    def __init__(
        self,
        ids: list[int],
        broken: dict[int, int] | None = None,
        titles: dict[int, str] | None = None,
    ) -> None:
        self.ids = ids
        self.titles = titles or {}
        self.broken = broken or {}  # id → код ответа вместо work item
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/wiql"):
            return httpx.Response(200, json={"workItems": [{"id": i} for i in self.ids]})
        match = re.search(r"/workitems/(\d+)$", path, re.IGNORECASE)
        if match:
            item_id = int(match[1])
            if item_id in self.broken:
                return httpx.Response(self.broken[item_id], text="x")
            item = copy.deepcopy(fixture("work-item-bug"))
            item["id"] = item_id
            item["fields"]["System.Id"] = item_id
            item["fields"]["System.Title"] = self.titles.get(item_id, f"Баг {item_id}")
            return httpx.Response(200, json=item)
        if path.endswith("/comments"):
            return httpx.Response(200, json=fixture("comments"))
        return httpx.Response(404)

    def client(self) -> TfsClient:
        return TfsClient(
            "https://tfs.example.test/Collection",
            "Project",
            PAT,
            transport=httpx.MockTransport(self),
            retry_wait=(),
            sleep=lambda s: None,
        )


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    store = TicketStore(tmp_path / "tickets")
    store.ensure_dirs()
    return store


# --- render_ticket --------------------------------------------------------------------------
def test_render_roundtrips_through_parser() -> None:
    ticket = Ticket(
        id="7",
        type="Bug",
        title="Заголовок: с двоеточием, #решёткой и 'кавычками'",
        state="Active",
        area="Proj\\Area",
        iteration="Proj\\Iter 1",
        priority=2,
        severity="3 - Medium",
        tags=["Applied", "AI Dev"],
        source="tfs",
        description="Шаг 1\nШаг 2",
        acceptance_criteria="- критерий",
        discussion=[
            Comment(author="Иванов Иван (Ivanov_II)", date="2026-09-23", text="Первый"),
            Comment(author="Петрова Мария (Petrova_MA)", date="2026-09-24", text="Второй\nстрока"),
        ],
    )
    assert parse_ticket(render_ticket(ticket)) == ticket


def test_render_minimal_ticket_omits_empty_fields() -> None:
    text = render_ticket(Ticket(id="1", type="Task", title="T", description="D"))
    assert text == ("---\nid: '1'\ntype: Task\ntitle: T\nsource: folder\n---\n## Description\nD\n")
    assert parse_ticket(text).discussion == []


def test_render_escapes_headings_so_text_cannot_forge_sections() -> None:
    ticket = Ticket(
        id="1",
        type="Bug",
        title="T",
        description="Текст\n## Discussion\n### Чужой · 2020-01-01\nподделка",
        discussion=[Comment(author="A", date="2026-01-01", text="### Взлом · 2020-01-01")],
    )
    parsed = parse_ticket(render_ticket(ticket))
    assert len(parsed.discussion) == 1 and parsed.discussion[0].author == "A"
    assert "подделка" in parsed.description  # текст остался в описании, секций не прибавилось


# --- fetch_tickets --------------------------------------------------------------------------
def test_fetch_writes_ticket_to_inbox(store: TicketStore) -> None:
    fake = FakeTfs([90202])
    with fake.client() as client:
        report = fetch_tickets(client, [90202], store)
    assert [(i.ticket_id, i.status, i.detail) for i in report.items] == [
        ("90202", "fetched", "Баг 90202")
    ]
    ticket = load_ticket(store.path("90202", "inbox"))
    assert (ticket.id, ticket.source, ticket.title) == ("90202", "tfs", "Баг 90202")
    assert len(ticket.discussion) == 3
    assert ticket.discussion[0].text.startswith("Проблема воспроизводится")  # старые первыми
    assert not list((store.path("90202", "inbox").parent).glob(".*.tmp"))  # временных нет


@pytest.mark.parametrize("folder", FOLDERS)
def test_existing_ticket_in_any_folder_is_not_touched(store: TicketStore, folder: str) -> None:
    path = store.path("90202", folder)  # type: ignore[arg-type]
    path.write_text("СВОЁ СОДЕРЖИМОЕ", encoding="utf-8")
    fake = FakeTfs([90202])
    with fake.client() as client:
        report = fetch_tickets(client, [90202], store)
    assert [(i.status, i.detail) for i in report.items] == [("skipped", f"уже есть в {folder}/")]
    assert path.read_text(encoding="utf-8") == "СВОЁ СОДЕРЖИМОЕ"
    assert fake.requests == []  # в сеть за существующим тикетом не ходим


def test_limit_counts_only_new_tickets(store: TicketStore) -> None:
    store.path("1", "done").write_text("x", encoding="utf-8")
    store.path("2", "needs-human").write_text("x", encoding="utf-8")
    fake = FakeTfs([1, 2, 3, 4, 5])
    with fake.client() as client:
        report = fetch_tickets(client, [1, 2, 3, 4, 5], store, limit=2)
    assert [(i.ticket_id, i.status) for i in report.items] == [
        ("1", "skipped"),
        ("2", "skipped"),
        ("3", "fetched"),
        ("4", "fetched"),
    ]
    assert not store.path("5", "inbox").exists()


def test_dry_run_downloads_but_writes_nothing(store: TicketStore) -> None:
    fake = FakeTfs([90202])
    with fake.client() as client:
        report = fetch_tickets(client, [90202], store, dry_run=True)
    assert [(i.status, i.detail) for i in report.items] == [("would-fetch", "Баг 90202")]
    assert not store.path("90202", "inbox").exists()
    assert {r.method for r in fake.requests} == {"GET"}  # TFS только читается


def test_one_failure_does_not_stop_the_rest(store: TicketStore) -> None:
    fake = FakeTfs([1, 2, 3], broken={2: 404})
    seen: list[FetchItem] = []
    with fake.client() as client:
        report = fetch_tickets(client, [1, 2, 3], store, on_item=seen.append)
    assert [(i.ticket_id, i.status) for i in report.items] == [
        ("1", "fetched"),
        ("2", "failed"),
        ("3", "fetched"),
    ]
    assert report.failed and seen == report.items
    assert not store.path("2", "inbox").exists()


def test_auth_error_aborts_everything(store: TicketStore) -> None:
    from ario.tfs.client import TfsAuthError

    fake = FakeTfs([1, 2], broken={1: 401})
    with fake.client() as client, pytest.raises(TfsAuthError):
        fetch_tickets(client, [1, 2], store)
    assert not store.path("2", "inbox").exists()


def test_id_mismatch_and_invalid_id_are_failures(store: TicketStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        item = copy.deepcopy(fixture("work-item-bug"))  # всегда work item 90202
        return httpx.Response(
            200, json=item if "comments" not in request.url.path else {"comments": []}
        )

    client = TfsClient(
        "https://tfs.example.test/C", "P", PAT, transport=httpx.MockTransport(handler)
    )
    with client:
        report = fetch_tickets(client, [5, -3], store)
    assert [i.status for i in report.items] == ["failed", "failed"]
    assert "вместо 5" in report.items[0].detail
    assert not any(store.path(i, "inbox").exists() for i in ("5", "-3"))


def test_secrets_are_masked_in_saved_file_and_errors(store: TicketStore) -> None:
    from ario.trace import Masker

    masker = Masker([PAT])

    def handler(request: httpx.Request) -> httpx.Response:
        if "comments" in request.url.path:
            return httpx.Response(200, json={"comments": []})
        item = copy.deepcopy(fixture("work-item-bug"))
        item["fields"]["Microsoft.VSTS.TCM.ReproSteps"] = f"<div>ключ {PAT} в тексте</div>"
        return httpx.Response(200, json=item)

    client = TfsClient(
        "https://tfs.example.test/C", "P", PAT, transport=httpx.MockTransport(handler)
    )
    with client:
        fetch_tickets(client, [90202], store, mask=masker.mask_text)
    text = store.path("90202", "inbox").read_text(encoding="utf-8")
    assert PAT not in text and "***" in text


def test_ticket_over_size_limit_is_rejected(
    store: TicketStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ario.tfs.fetch.MAX_TICKET_BYTES", 100)
    fake = FakeTfs([90202])
    with fake.client() as client:
        report = fetch_tickets(client, [90202], store)
    assert report.items[0].status == "failed" and "больше" in report.items[0].detail
    assert not store.path("90202", "inbox").exists()


# --- запрос отбора --------------------------------------------------------------------------
def test_default_query_matches_decision_d05() -> None:
    query = default_wiql()
    assert "[System.WorkItemType] = 'Bug'" in query
    assert "[System.Tags] CONTAINS 'AI dev'" in query
    assert "[System.AreaPath] UNDER 'SmartInstruments\\Antares'" in query
    assert "[System.State] NOT IN ('Closed', 'Removed', 'Done')" in query
    assert "@project" in query and query.endswith("ORDER BY [System.ChangedDate] DESC")


def test_query_ids_uses_custom_wiql_or_default() -> None:
    fake = FakeTfs([3, 2, 1])
    with fake.client() as client:
        assert query_ids(client, None) == [3, 2, 1]
        assert query_ids(client, "SELECT [System.Id] FROM WorkItems") == [3, 2, 1]
    sent = [json.loads(r.content)["query"] for r in fake.requests]
    assert sent == [default_wiql(), "SELECT [System.Id] FROM WorkItems"]


# --- настройки ------------------------------------------------------------------------------
def load_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Settings:
    for name in ("TFS_URL", "TFS_PROJECT", "TFS_PAT", "TFS_WIQL", "TFS_MAX_ITEMS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    return Settings.load(empty)


def test_settings_read_tfs_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = load_settings(
        monkeypatch,
        tmp_path,
        TFS_URL=" https://tfs.example.test/C ",
        TFS_PROJECT="P",
        TFS_PAT=PAT,
        TFS_MAX_ITEMS="3",
    )
    assert (s.tfs_url, s.tfs_project, s.tfs_pat, s.tfs_max_items) == (
        "https://tfs.example.test/C",
        "P",
        PAT,
        3,
    )
    assert s.tfs_wiql is None
    assert PAT not in repr(s)  # токен не попадает в repr настроек


def test_settings_tfs_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = load_settings(monkeypatch, tmp_path)
    assert (s.tfs_url, s.tfs_project, s.tfs_pat, s.tfs_max_items) == (None, None, None, 5)


@pytest.mark.parametrize("bad", ["0", "-1", "много"])
def test_settings_reject_bad_max_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: str
) -> None:
    with pytest.raises(ConfigError, match="TFS_MAX_ITEMS"):
        load_settings(monkeypatch, tmp_path, TFS_MAX_ITEMS=bad)


# --- команда ario fetch ---------------------------------------------------------------------
@pytest.fixture
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[FakeTfs, dict[str, str]], list[str]]:
    import truststore

    import ario.tfs.client as client_module

    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: None)
    real = client_module.TfsClient

    def prepare(fake: FakeTfs, extra: dict[str, str]) -> list[str]:
        def factory(url: str, project: str, pat: str) -> TfsClient:
            return real(
                url,
                project,
                pat,
                transport=httpx.MockTransport(fake),
                retry_wait=(),
                sleep=lambda s: None,
            )

        monkeypatch.setattr(client_module, "TfsClient", factory)
        env = {
            "ARIO_DATA_DIR": str(tmp_path / "data"),
            "ARIO_GUARDRAILS": str(REPO_ROOT / "config" / "guardrails.yaml"),
            "TFS_URL": "https://tfs.example.test/Collection",
            "TFS_PROJECT": "Project",
            "TFS_PAT": PAT,
            "TFS_MAX_ITEMS": "2",
            **extra,
        }
        for name in ("TFS_WIQL",):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        empty = tmp_path / "empty.env"
        empty.write_text("", encoding="utf-8")
        return ["--env-file", str(empty), "fetch"]

    return prepare


def test_cli_fetch_by_query_respects_limit(
    cli_env: Callable[[FakeTfs, dict[str, str]], list[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli

    base = cli_env(FakeTfs([11, 12, 13]), {})
    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert "[взят] 11: Баг 11" in out and "[взят] 12: Баг 12" in out and "13" not in out
    assert "Итого: взято 2, пропущено 0, ошибок 0" in out
    inbox = tmp_path / "data" / "tickets" / "inbox"
    assert sorted(p.name for p in inbox.glob("*.md")) == ["11.md", "12.md"]
    # повторный запуск берёт следующий, а скачанные пропускает
    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert "[пропущен] 11: уже есть в inbox/" in out and "[взят] 13: Баг 13" in out


def test_cli_fetch_by_id_dry_run(
    cli_env: Callable[[FakeTfs, dict[str, str]], list[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli

    base = cli_env(FakeTfs([]), {})
    assert cli.main([*base, "--id", "21", "22", "23", "--dry-run"]) == 0  # явные id вне лимита
    out = capsys.readouterr().out
    assert out.count("[будет взят]") == 3 and "Итого: будет взято 3" in out
    assert not (tmp_path / "data" / "tickets" / "inbox").exists()


def test_cli_fetch_exit_codes(
    cli_env: Callable[[FakeTfs, dict[str, str]], list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli

    base = cli_env(FakeTfs([]), {})
    assert cli.main(base) == 0  # запрос ничего не вернул
    assert "нет" in capsys.readouterr().out
    assert cli.main([*base, "--id", "31"]) == 0
    capsys.readouterr()
    # сбой одного тикета: код 1
    bad = cli_env(FakeTfs([], broken={41: 404}), {})
    assert cli.main([*bad, "--id", "41"]) == 1
    assert "[ОШИБКА] 41" in capsys.readouterr().out
    # PAT отклонён: код 2, токена нет в выводе
    denied = cli_env(FakeTfs([], broken={51: 401}), {})
    assert cli.main([*denied, "--id", "51"]) == 2
    captured = capsys.readouterr()
    assert "TFS_PAT" in captured.err and PAT not in captured.out + captured.err
    # не заданы настройки: код 2
    monkeypatch.delenv("TFS_PAT")
    assert cli.main(base) == 2
    assert "TFS_PAT" in capsys.readouterr().err


def test_non_positive_ids_are_rejected_without_network(store: TicketStore) -> None:
    fake = FakeTfs([])
    with fake.client() as client:
        report = fetch_tickets(client, [0, -3], store)
    assert [i.status for i in report.items] == ["failed", "failed"]
    assert fake.requests == []


def test_cli_hides_terminal_control_characters(
    cli_env: Callable[[FakeTfs, dict[str, str]], list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli

    esc = chr(27)
    base = cli_env(FakeTfs([], titles={61: f"Заголовок {esc}[2J{esc}]0;взлом"}), {})
    assert cli.main([*base, "--id", "61", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert esc not in out and "Заголовок ?[2J?]0;взлом" in out


def test_cli_warns_when_all_candidates_were_already_taken(
    cli_env: Callable[[FakeTfs, dict[str, str]], list[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ario.cli as cli
    from ario.tfs.fetch import CANDIDATES_LIMIT

    done = tmp_path / "data" / "tickets" / "done"
    done.mkdir(parents=True)
    ids = list(range(1, CANDIDATES_LIMIT + 1))
    for i in ids:
        (done / f"{i}.md").write_text("x", encoding="utf-8")
    base = cli_env(FakeTfs(ids), {})
    assert cli.main(base) == 0
    assert "TFS_WIQL" in capsys.readouterr().err
