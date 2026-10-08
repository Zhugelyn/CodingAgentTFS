"""Загрузка тикетов из TFS в `tickets/inbox/` (команда `ario fetch`).

Файлы пишутся в формате D01 и дальше идут обычным потоком `ario run <id>`. В TFS ничего не
меняется. Тикет, который уже лежит в любой папке состояний, повторно не скачивается и не
перезаписывается: так не теряются тикеты в работе и результаты прогонов.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ario.fsutil import atomic_write_text
from ario.store import InvalidTicketIdError, TicketStore, validate_ticket_id
from ario.tfs.client import TfsAuthError, TfsClient, TfsError
from ario.tfs.convert import to_ticket
from ario.ticket import MAX_TICKET_BYTES, TicketError, parse_ticket, render_ticket

CANDIDATES_LIMIT = 100  # сколько id берём из запроса: часть может быть уже скачана

# Отбор по умолчанию (решение D05); полностью заменяется переменной TFS_WIQL.
WIQL_TYPE = "Bug"
WIQL_TAG = "AI dev"
WIQL_AREA = "SmartInstruments\\Antares"
WIQL_CLOSED_STATES = ("Closed", "Removed", "Done")


def default_wiql() -> str:
    states = ", ".join(f"'{state}'" for state in WIQL_CLOSED_STATES)
    return (
        "SELECT [System.Id] FROM WorkItems "
        "WHERE [System.TeamProject] = @project "
        f"AND [System.WorkItemType] = '{WIQL_TYPE}' "
        f"AND [System.Tags] CONTAINS '{WIQL_TAG}' "
        f"AND [System.AreaPath] UNDER '{WIQL_AREA}' "
        f"AND [System.State] NOT IN ({states}) "
        "ORDER BY [System.ChangedDate] DESC"
    )


Status = Literal["fetched", "would-fetch", "skipped", "failed"]


@dataclass
class FetchItem:
    ticket_id: str
    status: Status
    detail: str = ""  # заголовок тикета, причина пропуска или текст ошибки


@dataclass
class FetchReport:
    items: list[FetchItem] = field(default_factory=list)

    def of(self, status: Status) -> list[FetchItem]:
        return [item for item in self.items if item.status == status]

    @property
    def failed(self) -> bool:
        return bool(self.of("failed"))


def fetch_tickets(
    client: TfsClient,
    ids: Sequence[int],
    store: TicketStore,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    mask: Callable[[str], str] = lambda text: text,
    on_item: Callable[[FetchItem], None] | None = None,
) -> FetchReport:
    """Скачивает тикеты по списку id (в порядке списка).

    `limit` — максимум НОВЫХ тикетов за вызов; пропущенные (уже есть) в лимит не входят.
    Ошибка доступа (PAT отклонён) прерывает всё; сбой одного тикета записывается и не мешает
    остальным. Тексты ошибок и файл тикета проходят `mask` (секреты не попадают в inbox).
    """
    report = FetchReport()

    def add(item: FetchItem) -> None:
        report.items.append(item)
        if on_item:
            on_item(item)

    taken = 0
    for raw_id in ids:
        if limit is not None and taken >= limit:
            break
        ticket_id = str(raw_id)
        try:
            if raw_id <= 0:
                raise InvalidTicketIdError(f"Недопустимый id work item: {raw_id}")
            validate_ticket_id(ticket_id)
        except InvalidTicketIdError as e:
            add(FetchItem(ticket_id, "failed", str(e)))
            continue
        existing = store.locations(ticket_id)
        if existing:
            add(FetchItem(ticket_id, "skipped", f"уже есть в {existing[0]}/"))
            continue
        try:
            text, title = _download(client, raw_id, ticket_id, mask)
        except TfsAuthError:
            raise
        except (TfsError, TicketError) as e:
            add(FetchItem(ticket_id, "failed", mask(str(e))))
            continue
        taken += 1
        if dry_run:
            add(FetchItem(ticket_id, "would-fetch", title))
            continue
        store.ensure_dirs()
        atomic_write_text(store.path(ticket_id, "inbox"), text)
        add(FetchItem(ticket_id, "fetched", title))
    return report


def _download(
    client: TfsClient, raw_id: int, ticket_id: str, mask: Callable[[str], str]
) -> tuple[str, str]:
    ticket = to_ticket(client.work_item(raw_id), client.comments(raw_id))
    if ticket.id != ticket_id:
        raise TicketError(f"TFS вернул work item {ticket.id} вместо {ticket_id}")
    text = mask(render_ticket(ticket))
    if len(text.encode("utf-8")) > MAX_TICKET_BYTES:
        raise TicketError(f"Тикет {ticket_id} больше {MAX_TICKET_BYTES} байт")
    parse_ticket(text)  # то, что ляжет в inbox, должно разбираться обычным парсером
    return text, ticket.title


def query_ids(client: TfsClient, wiql: str | None) -> list[int]:
    return client.wiql(wiql or default_wiql(), CANDIDATES_LIMIT)
