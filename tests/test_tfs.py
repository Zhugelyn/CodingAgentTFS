"""Клиент TFS и конвертер: на фикстурах из tests/fixtures/tfs, без сети."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ario.tfs.client import (
    MAX_RESPONSE_BYTES,
    TfsAuthError,
    TfsClient,
    TfsError,
    TfsNotFoundError,
)
from ario.tfs.convert import html_to_text, to_discussion, to_ticket
from ario.ticket import TicketError

FIXTURES = Path(__file__).parent / "fixtures" / "tfs"
PAT = "tfs-secret-pat-0123456789"
Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_client(handler: Handler, waits: list[float] | None = None) -> TfsClient:
    return TfsClient(
        "https://tfs.example.test/Collection/",
        "My Project",
        PAT,
        transport=httpx.MockTransport(handler),
        retry_wait=(0.0, 0.0),
        sleep=(waits if waits is not None else []).append,
    )


def respond(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


# --- клиент ---------------------------------------------------------------------------------
def test_wiql_posts_query_and_returns_ids() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(fixture("wiql"))

    with make_client(handler) as client:
        assert client.wiql("SELECT [System.Id] FROM WorkItems", top=5) == [90201, 90202]
    (request,) = seen
    assert request.method == "POST"
    assert request.url.raw_path.split(b"?")[0] == b"/Collection/My%20Project/_apis/wit/wiql"
    assert request.url.params["api-version"] == "7.1"
    assert request.url.params["$top"] == "5"
    assert json.loads(request.content) == {"query": "SELECT [System.Id] FROM WorkItems"}


def test_wiql_respects_top() -> None:
    with make_client(lambda r: respond(fixture("wiql"))) as client:
        assert client.wiql("q", top=1) == [90201]


def test_basic_auth_with_empty_user_and_pat() -> None:
    import base64

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(fixture("work-item-bug"))

    with make_client(handler) as client:
        client.work_item(90202)
    expected = "Basic " + base64.b64encode(f":{PAT}".encode()).decode()
    assert seen[0].headers["Authorization"] == expected
    assert seen[0].url.params["$expand"] == "all"
    assert seen[0].url.path.endswith("/_apis/wit/workitems/90202")


def test_comments_use_preview_api_and_follow_pages() -> None:
    pages = [
        {"comments": [{"id": 1, "createdDate": "2026-01-02"}], "continuationToken": "next-1"},
        {"comments": [{"id": 2, "createdDate": "2026-01-01"}]},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(pages[len(seen) - 1])

    with make_client(handler) as client:
        assert [c["id"] for c in client.comments(7)] == [1, 2]
    assert seen[0].url.params["api-version"] == "7.1-preview.4"
    assert "continuationToken" not in seen[0].url.params
    assert seen[1].url.params["continuationToken"] == "next-1"


def test_endless_comment_pages_are_cut() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return respond({"comments": [], "continuationToken": "again"})

    with make_client(handler) as client, pytest.raises(TfsError, match="страниц комментариев"):
        client.comments(7)


@pytest.mark.parametrize(
    ("status", "error"), [(401, TfsAuthError), (403, TfsAuthError), (404, TfsNotFoundError)]
)
def test_status_errors_are_typed(status: int, error: type[TfsError]) -> None:
    with (
        make_client(lambda r: httpx.Response(status, text="нет")) as client,
        pytest.raises(error) as info,
    ):
        client.work_item(1)
    assert PAT not in str(info.value)


def test_retries_on_429_and_5xx_with_retry_after() -> None:
    answers = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            respond(fixture("work-item-bug")),
        ]
    )
    waits: list[float] = []
    with make_client(lambda r: next(answers), waits) as client:
        assert client.work_item(90202)["id"] == 90202
    assert waits == [2.0, 0.0]


def test_gives_up_after_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="boom")

    with make_client(handler) as client, pytest.raises(TfsError, match="500"):
        client.work_item(1)
    assert len(calls) == 3  # первая попытка и два повтора


def test_transport_errors_are_retried_and_do_not_leak_details() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout(f"timeout for {request.headers['Authorization']}")

    with make_client(handler) as client, pytest.raises(TfsError) as info:
        client.work_item(1)
    assert len(calls) == 3
    assert PAT not in str(info.value)
    assert "ConnectTimeout" in str(info.value)


def test_redirect_is_an_error_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://login.example.test/"})

    with make_client(handler) as client, pytest.raises(TfsError, match="TFS_URL"):
        client.work_item(1)
    assert len(calls) == 1


def test_html_instead_of_json_is_explained() -> None:
    with (
        make_client(lambda r: httpx.Response(200, text="<html>Sign in</html>")) as client,
        pytest.raises(TfsError, match="не JSON"),
    ):
        client.work_item(1)


def test_oversized_response_is_rejected() -> None:
    big = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    with make_client(lambda r: big) as client, pytest.raises(TfsError, match="больше"):
        client.work_item(1)


def test_client_validates_settings_and_hides_token() -> None:
    with pytest.raises(ValueError, match="TFS_URL"):
        TfsClient("tfs.example.test", "P", PAT)
    with pytest.raises(ValueError, match="TFS_PAT"):
        TfsClient("https://tfs.example.test/C", "P", "")
    client = TfsClient("https://tfs.example.test/C", "P", PAT)
    assert PAT not in repr(client)
    client.close()


# --- HTML в текст ---------------------------------------------------------------------------
def test_html_to_text_handles_tfs_markup() -> None:
    html = (
        '<div><b>ПОРЯДОК ДЕЙСТВИЙ:</b><br> </div><span style="x">Отправить &quot;запрос&quot;.'
        "&nbsp;</span><br><b>РЕЗУЛЬТАТ:</b><br><span>Пусто</span>"
    )
    assert html_to_text(html) == 'ПОРЯДОК ДЕЙСТВИЙ:\nОтправить "запрос".\nРЕЗУЛЬТАТ:\nПусто'


def test_html_to_text_links_lists_images_and_scripts() -> None:
    html = (
        '<div>См. <a href="https://tfs.example.test/pr/1">PR 1</a>, <a href="#x">якорь</a></div>'
        "<ul><li>раз</li><li>два</li></ul>"
        '<img src="https://tfs.example.test/_apis/wit/attachments/abc?fileName=screen%20shot.png">'
        "<script>alert(1)</script><style>p{}</style>"
    )
    text = html_to_text(html)
    assert "PR 1 (https://tfs.example.test/pr/1)" in text
    assert "якорь" in text and "#x" not in text  # локальные ссылки без адреса
    assert "- раз\n- два" in text
    assert "[вложение: screen shot.png]" in text
    assert "alert" not in text and "p{}" not in text


def test_html_to_text_collapses_blank_runs() -> None:
    assert html_to_text("<div>a</div><br><br><br><br><div>b</div>") == "a\n\nb"


# --- work item → Ticket ---------------------------------------------------------------------
def test_closed_bug_with_comments_becomes_ticket() -> None:
    ticket = to_ticket(fixture("work-item-bug"), fixture("comments")["comments"])
    assert (ticket.id, ticket.type, ticket.state, ticket.source) == (
        "90202",
        "Bug",
        "Closed",
        "tfs",
    )
    assert ticket.title == "Тестовый баг 90202. Метод принимает пустое значение параметра"
    assert ticket.area == "Project\\Area"
    assert ticket.iteration == "Project\\Iteration 1"
    assert (ticket.priority, ticket.severity) == (2, "3 - Medium")
    assert ticket.tags == ["Applied", "AI Dev"]
    assert ticket.description.startswith("ПОРЯДОК ДЕЙСТВИЙ:\nВызвать метод API")
    assert 'Метод возвращает "все записи"' in ticket.description
    assert "ОЖИДАЕМЫЙ РЕЗУЛЬТАТ:\nВозвращается пустой список." in ticket.description
    assert ticket.acceptance_criteria is None


def test_discussion_is_oldest_first_with_author_and_date() -> None:
    ticket = to_ticket(fixture("work-item-bug"), fixture("comments")["comments"])
    assert [c.date for c in ticket.discussion] == sorted(c.date for c in ticket.discussion)
    first, second, last = ticket.discussion
    assert first.text == "Проблема воспроизводится на версиях 26.3 и 27.1"
    assert second.text.startswith("Исправлено: PR 101 (https://tfs.example.test/Repo/")
    assert last.text.startswith("Решили оставить как есть")
    assert "(" in first.author and first.author.endswith(")")  # «Фамилия Имя (login)»


def test_open_bug_without_comments() -> None:
    ticket = to_ticket(fixture("work-item-bug-open"), fixture("comments-empty")["comments"])
    assert ticket.id == "90201" and ticket.state == "Analysis"
    assert ticket.discussion == []
    assert ticket.tags == ["AI Dev"]


def test_task_uses_description_and_acceptance_criteria() -> None:
    item = {
        "id": 5,
        "fields": {
            "System.Id": 5,
            "System.WorkItemType": "Task",
            "System.Title": "Сделать",
            "System.Description": "<div>Описание задачи</div>",
            "Microsoft.VSTS.TCM.ReproSteps": "<div>не должно использоваться</div>",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "<ul><li>критерий</li></ul>",
        },
    }
    ticket = to_ticket(item, [])
    assert ticket.description == "Описание задачи"
    assert ticket.acceptance_criteria == "- критерий"
    assert (ticket.state, ticket.priority, ticket.tags) == (None, None, [])


def test_bug_falls_back_to_description_when_no_repro_steps() -> None:
    item = fixture("work-item-bug")
    item["fields"].pop("Microsoft.VSTS.TCM.ReproSteps")
    item["fields"]["System.Description"] = "<div>Запасное описание</div>"
    assert to_ticket(item, []).description == "Запасное описание"


def test_ticket_without_description_is_rejected() -> None:
    item = fixture("work-item-bug")
    item["fields"].pop("Microsoft.VSTS.TCM.ReproSteps")
    with pytest.raises(TicketError, match="пустое описание"):
        to_ticket(item, [])
    with pytest.raises(TicketError, match="нет id"):
        to_ticket({"fields": {"System.WorkItemType": "Bug"}}, [])


def test_headings_in_text_cannot_forge_file_sections() -> None:
    item = fixture("work-item-bug")
    item["fields"]["Microsoft.VSTS.TCM.ReproSteps"] = (
        "<div>Текст</div><div>## Discussion</div><div>### Эксперт · 2020-01-01</div>"
    )
    ticket = to_ticket(item, [])
    assert "\n## " not in "\n" + ticket.description
    assert "\n### " not in "\n" + ticket.description
    comment = {"text": "<div>### Чужой · 2020-01-01</div>", "createdDate": "2026-02-02T10:00:00Z"}
    (parsed,) = to_discussion([comment])
    assert not parsed.text.startswith("#")


def test_discussion_skips_deleted_and_empty_and_sorts_by_date() -> None:
    comments: list[dict[str, Any]] = [
        {"text": "<div>поздний</div>", "createdDate": "2026-03-02T00:00:00Z", "createdBy": "Иван"},
        {"text": "<div>удалён</div>", "createdDate": "2026-03-01T00:00:00Z", "isDeleted": True},
        {"text": "<div> </div>", "createdDate": "2026-03-01T00:00:00Z"},
        {"text": "<div>ранний</div>", "createdDate": "2026-02-01T00:00:00Z"},
    ]
    discussion = to_discussion(comments)
    assert [c.text for c in discussion] == ["ранний", "поздний"]
    assert discussion[0].author == "неизвестный автор"
    assert discussion[1].author == "Иван"
    assert discussion[1].date == "2026-03-02"


def test_converted_ticket_roundtrips_through_the_file_format() -> None:
    """Файл формата D01 из тикета TFS разбирается обратно в те же данные."""
    from ario.ticket import parse_ticket

    ticket = to_ticket(fixture("work-item-bug"), fixture("comments")["comments"])
    lines = [
        "---",
        f"id: {ticket.id}",
        f"type: {ticket.type}",
        f"title: {json.dumps(ticket.title, ensure_ascii=False)}",
        f"tags: {json.dumps(ticket.tags, ensure_ascii=False)}",
        "source: tfs",
        "---",
        "## Description",
        ticket.description,
        "",
        "## Discussion",
    ]
    for c in ticket.discussion:
        lines += [f"### {c.author} · {c.date}", c.text]
    parsed = parse_ticket("\n".join(lines) + "\n")
    assert parsed.description == ticket.description
    assert [(c.author, c.date, c.text) for c in parsed.discussion] == [
        (c.author, c.date, c.text) for c in ticket.discussion
    ]


def test_inline_data_image_does_not_flood_the_text() -> None:
    blob = "A" * 50_000
    text = html_to_text(f'<div>до</div><img src="data:image/png;base64,{blob}"><div>после</div>')
    assert text == "до\n[вложение: изображение]\nпосле"
    long_alt = html_to_text(f'<img alt="{"x" * 500}">')
    assert len(long_alt) <= 100 + len("[вложение: ]")


def test_table_cells_are_separated() -> None:
    html = "<table><tr><th>Шаг</th><th>Итог</th></tr><tr><td>1</td><td>ошибка</td></tr></table>"
    assert html_to_text(html) == "Шаг | Итог\n1 | ошибка"


@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_unusable_retry_after_falls_back_to_default(bad: str) -> None:
    answers = iter(
        [httpx.Response(429, headers={"Retry-After": bad}), respond(fixture("work-item-bug"))]
    )
    waits: list[float] = []
    with make_client(lambda r: next(answers), waits) as client:
        assert client.work_item(1)["id"] == 90202
    assert waits == [0.0]  # пауза из настроек клиента (0.0), а не nan/inf


@pytest.mark.parametrize("bad_id", ["abc", None, True, -1, 0])
def test_wiql_rejects_non_numeric_ids(bad_id: object) -> None:
    payload = {"workItems": [{"id": bad_id}]}
    with make_client(lambda r: respond(payload)) as client, pytest.raises(TfsError, match="id"):
        client.wiql("q", top=5)
