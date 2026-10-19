"""Конфигурация доски (S22): файл board, поток на репозиторий, категории состояний, ссылки."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ario.board import BoardConfig
from ario.config import ConfigError, Settings
from ario.reporting import TicketFlow, TicketReporter, flow_problems
from ario.repos import RepoRegistry
from ario.ticket import Ticket
from ario.ticket_system import TfsTicketSystem, WriteResult, render_comment_html
from tests.test_ticket_system import FakeClient

BOARD_YAML = """\
board:
  url: https://tfs.example.test/Board/
  project: Antares
  query: "SELECT [System.Id] FROM WorkItems"
  max_items: 3
  pat_env: BOARD_READ
  flow: board-flow.yaml
default: rx
repositories:
  rx:
    path: rx-clone
    flow: rx-flow.yaml
    description: Основной репозиторий RX
  dsa:
    path: dsa-clone
"""

FLOW_RX = """\
states:
  pr_created: {to: Acceptance, from: [Development]}
comments: {done: false}
"""
FLOW_BOARD = """\
states:
  pr_created: {to: ReadyForTesting, from: [Development]}
"""


def write_board(tmp_path: Path, text: str = BOARD_YAML) -> Path:
    path = tmp_path / "config" / "board.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- файл доски ---------------------------------------------------------------------------------
def test_board_file_gives_the_board_settings_and_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOARD_READ", "read-token-123")
    monkeypatch.delenv("TFS_WRITE_PAT", raising=False)
    config = BoardConfig.load(write_board(tmp_path))
    registry = config.registry(tmp_path)
    assert registry.names == ["rx", "dsa"] and registry.default is not None
    assert registry.get("rx").description == "Основной репозиторий RX"
    assert registry.get("rx").flow_path == (tmp_path / "config" / "rx-flow.yaml").resolve()
    assert registry.get("dsa").flow_path is None
    base = Settings(
        data_dir=tmp_path,
        rx_path=None,
        llm_model="m",
        openrouter_api_key=None,
        guardrails_path=tmp_path / "g.yaml",
    )
    settings = config.apply(base)
    assert settings.tfs_url == "https://tfs.example.test/Board"  # без хвостового слеша
    assert settings.tfs_project == "Antares" and settings.tfs_max_items == 3
    assert settings.tfs_wiql == "SELECT [System.Id] FROM WorkItems"
    assert settings.tfs_pat == "read-token-123" and settings.tfs_write_pat is None
    assert settings.ticket_flow_path == (tmp_path / "config" / "board-flow.yaml").resolve()
    assert "read-token-123" not in repr(settings)


def test_settings_load_reads_the_board_and_refuses_two_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    for name in ("TFS_URL", "TFS_PROJECT", "ARIO_REPOS", "ARIO_BOARD"):
        monkeypatch.delenv(name, raising=False)
    board = write_board(tmp_path)
    monkeypatch.setenv("ARIO_BOARD", str(board))
    settings = Settings.load(empty)
    assert settings.board_path == board.resolve() and settings.tfs_project == "Antares"
    assert RepoRegistry.from_settings(settings).names == ["rx", "dsa"]
    monkeypatch.setenv("ARIO_REPOS", str(tmp_path / "repos.yaml"))
    with pytest.raises(ConfigError, match="ARIO_BOARD.*ARIO_REPOS"):
        Settings.load(empty)


def test_board_file_mistakes_fail_closed(tmp_path: Path) -> None:
    for text in (
        BOARD_YAML.replace("  project: Antares\n", ""),  # нет проекта
        BOARD_YAML.replace("default: rx", "default: нет"),
        BOARD_YAML.replace("max_items: 3", "max_items: 0"),
        BOARD_YAML.replace("pat_env: BOARD_READ", "pat: секрет"),  # токен в файле не хранится
        BOARD_YAML.replace("https://tfs.example.test/Board/", "ftp://x"),
        "board: {url: https://x, project: P}\nrepositories: {}\n",
    ):
        with pytest.raises(ConfigError):
            BoardConfig.load(write_board(tmp_path, text))
    with pytest.raises(ConfigError):
        BoardConfig.load(tmp_path / "нет.yaml")


# --- поток на репозиторий -----------------------------------------------------------------------
class Recorder:
    def __init__(self) -> None:
        self.moves: list[tuple[str, str]] = []
        self.comments: list[str] = []

    def comment(self, ticket_id: str, text: str, *, mention_author: bool = False) -> WriteResult:
        self.comments.append(text)
        return WriteResult("done")

    def transition(self, ticket_id: str, to_state: str, *, allowed_from: Any) -> WriteResult:
        self.moves.append((ticket_id, to_state))
        return WriteResult("done")


def context(repo_name: str | None) -> Any:
    ticket = Ticket(id="5", type="Bug", title="t", description="d", source="tfs")
    repo = SimpleNamespace(name=repo_name, branch_template="b/{id}") if repo_name else None
    events: list[Any] = []
    trace = SimpleNamespace(event=lambda *a, **k: events.append((a, k)))
    return SimpleNamespace(ticket=ticket, repo=repo, trace=trace, run_dir=Path("."))


def flow(tmp_path: Path, text: str) -> TicketFlow:
    path = tmp_path / "f.yaml"
    path.write_text(text, encoding="utf-8")
    return TicketFlow.load(path)


def test_reporter_takes_the_flow_of_the_ticket_repository(tmp_path: Path) -> None:
    recorder = Recorder()
    reporter = TicketReporter(recorder, flow(tmp_path, FLOW_BOARD), {"rx": flow(tmp_path, FLOW_RX)})
    reporter.stage_finished(context("rx"), "publish")
    reporter.stage_finished(context("dsa"), "publish")  # у dsa своего потока нет: поток доски
    reporter.stage_finished(context(None), "publish")
    assert [state for _, state in recorder.moves] == [
        "Acceptance",
        "ReadyForTesting",
        "ReadyForTesting",
    ]


def test_comment_switches_come_from_the_repository_flow(tmp_path: Path) -> None:
    recorder = Recorder()
    reporter = TicketReporter(recorder, flow(tmp_path, FLOW_BOARD), {"rx": flow(tmp_path, FLOW_RX)})
    reporter.run_finished(context("rx"), "done", None)  # в потоке rx: comments.done: false
    assert recorder.comments == []
    reporter.run_finished(context("dsa"), "done", None)
    assert len(recorder.comments) == 1


# --- категории состояний ------------------------------------------------------------------------
def test_from_accepts_a_category_of_the_current_state() -> None:
    client = FakeClient(state="ReadyForDev")
    ts = TfsTicketSystem(client)
    by_category = ts.transition("7", "Development", allowed_from=["category:Proposed"])
    assert by_category.ok and client.state_calls == [(7, "Development", 9)]
    lower = FakeClient(state="New")  # регистр категории не важен
    assert (
        TfsTicketSystem(lower).transition("7", "Development", allowed_from=["category:proposed"]).ok
    )


def test_a_category_does_not_let_other_states_through() -> None:
    client = FakeClient(state="Development")  # категория InProgress
    result = TfsTicketSystem(client).transition(
        "7", "Acceptance", allowed_from=["category:Proposed"]
    )
    assert result.status == "skipped" and "человек опередил" in result.detail
    assert client.state_calls == []
    unknown = FakeClient(state="Мистика")  # состояния нет у типа: категории нет, имя не совпало
    assert (
        TfsTicketSystem(unknown)
        .transition("7", "Acceptance", allowed_from=["category:Proposed"])
        .status
        == "skipped"
    )


def test_flow_schema_rejects_an_unknown_category(tmp_path: Path) -> None:
    text = "states:\n  analysis_started: {to: Analysis, from: ['category:Nonsense']}\n"
    path = tmp_path / "f.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="неизвестная категория"):
        TicketFlow.load(path)
    good = flow(
        tmp_path, "states:\n  analysis_started: {to: Analysis, from: ['category:Proposed']}\n"
    )
    assert good.states.analysis_started is not None


# --- проверка потока на старте ------------------------------------------------------------------
STATES = {"New": "Proposed", "Development": "InProgress", "Acceptance": "Resolved"}


def test_flow_problems_name_missing_states(tmp_path: Path) -> None:
    ok = flow(tmp_path, "states:\n  pr_created: {to: Acceptance, from: [Development]}\n")
    assert flow_problems(ok, STATES) == []
    bad = flow(
        tmp_path,
        "states:\n  pr_created: {to: Приёмка, from: [Development, Dev]}\n"
        "  analysis_started: {to: New, from: ['*', 'category:Proposed']}\n",
    )
    found = flow_problems(bad, STATES)
    assert len(found) == 2 and "Приёмка" in found[0] and "'Dev'" in found[1]


def test_reporter_problems_check_every_flow_against_the_board(tmp_path: Path) -> None:
    system = TfsTicketSystem(FakeClient())
    bad = flow(tmp_path, "states:\n  pr_created: {to: Приёмка, from: [Development]}\n")
    reporter = TicketReporter(system, flow(tmp_path, FLOW_RX), {"dsa": bad})
    problems = reporter.problems(["Bug"])
    assert len(problems) == 1 and "репозиторий dsa" in problems[0] and "Приёмка" in problems[0]
    assert TicketReporter(Recorder(), flow(tmp_path, FLOW_RX)).problems(["Bug"]) == []


# --- ссылки в комментариях ----------------------------------------------------------------------
def test_urls_become_links_without_letting_markup_through() -> None:
    url = "https://rxtfs.example.test/Git/_git/R/pullrequest/7"
    html = render_comment_html(f"PR: {url}.\nГотово", link_hosts=["rxtfs.example.test"])
    assert f'<a href="{url}">{url}</a>.<br>Готово' in html  # точка после адреса вне ссылки
    hostile = render_comment_html(
        '<script>x</script> http://a.test/?q="><img src=x onerror=y>', link_hosts=["a.test"]
    )
    assert "<script>" not in hostile and "<img" not in hostile
    assert "javascript:alert(1)" in render_comment_html("javascript:alert(1)")
    assert "<a " not in render_comment_html("javascript:alert(1)")
    assert "<a " not in render_comment_html("https://")


def test_a_state_without_a_category_explains_the_skip() -> None:
    client = FakeClient(state="Мистика")
    client.states["Мистика"] = ""  # сервер не вернул категорию
    result = TfsTicketSystem(client).transition(
        "7", "Acceptance", allowed_from=["category:Proposed"]
    )
    assert result.status == "skipped" and "укажите имена состояний" in result.detail


def test_problems_survive_an_unreachable_board(tmp_path: Path) -> None:
    from ario.tfs.client import TfsError

    client = FakeClient()

    def broken(item_type: str) -> dict[str, str]:
        raise TfsError("Нет связи с TFS")

    client.type_state_categories = broken  # type: ignore[method-assign]
    reporter = TicketReporter(TfsTicketSystem(client), flow(tmp_path, FLOW_RX))
    (message,) = reporter.problems(["Bug"])
    assert "не удалось сверить" in message and "Нет связи" in message


def test_a_broken_repository_flow_stops_the_start(tmp_path: Path) -> None:
    from dataclasses import replace

    from ario.reporting import build_reporter

    board = write_board(tmp_path)  # rx-flow.yaml рядом не лежит
    base = Settings(
        data_dir=tmp_path,
        rx_path=None,
        llm_model="m",
        openrouter_api_key=None,
        guardrails_path=tmp_path / "g.yaml",
        board_path=board,
    )
    with pytest.raises(ConfigError, match="rx-flow.yaml"):
        build_reporter(replace(base, ticket_flow_path=None), lambda text: text)
