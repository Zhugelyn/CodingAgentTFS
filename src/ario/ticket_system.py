"""Система тикетов: запись оркестратора на доску (комментарии, смена состояния).

Оркестратору всё равно, чем писать: интерфейс `TicketSystem` реализуют прямой клиент TFS
(`TfsTicketSystem`), заглушка `NullTicketSystem` (запись выключена) и, позже, адаптер к MCP.
Сбой записи не поднимается исключением: результат возвращается как `WriteResult`, потому что
недоступная доска не должна ронять прогон.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ario.tfs.client import TfsConflictError, TfsError

MAX_COMMENT_CHARS = 6000
MAX_FAILURES = 2  # подряд сбоев записи, после которых доску до конца прогона не трогаем
ANY_STATE = "*"  # в списке допустимых исходных состояний: менять из любого

Status = Literal["done", "skipped", "failed"]


@dataclass(frozen=True)
class WriteResult:
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done"


class TicketSystem(Protocol):
    def comment(self, ticket_id: str, text: str, *, mention_author: bool = False) -> WriteResult:
        """Добавляет комментарий; `mention_author` отмечает автора тикета (придёт уведомление)."""
        ...

    def transition(
        self, ticket_id: str, to_state: str, *, allowed_from: Sequence[str]
    ) -> WriteResult:
        """Переводит тикет в `to_state`, только если текущее состояние в `allowed_from`."""
        ...


class NullTicketSystem:
    """Запись выключена или не настроена: ничего не меняет, называет причину в результате."""

    def __init__(self, reason: str = "запись в TFS выключена (TFS_WRITE)") -> None:
        self.reason = reason

    def comment(self, ticket_id: str, text: str, *, mention_author: bool = False) -> WriteResult:
        return WriteResult("skipped", self.reason)

    def transition(
        self, ticket_id: str, to_state: str, *, allowed_from: Sequence[str]
    ) -> WriteResult:
        return WriteResult("skipped", self.reason)


def render_comment_html(text: str, mention: tuple[str, str] | None = None) -> str:
    """Текст → HTML комментария TFS. Всё экранируется: текст может нести недоверенное."""
    body = html.escape(text.strip()[:MAX_COMMENT_CHARS], quote=False).replace("\n", "<br>")
    if mention is None:
        return body
    name, identity_id = mention
    tag = (
        f'<a href="#" data-vss-mention="version:2.0,{html.escape(identity_id)}">'
        f"@{html.escape(name, quote=False)}</a>"
    )
    return f"{tag}<br>{body}"


class TfsTicketSystem:
    """Запись в Azure DevOps Server через `TfsClient` (доска, не git-сервер)."""

    def __init__(self, client: _Client, mask: Callable[[str], str] = lambda text: text) -> None:
        self._client = client
        self._mask = mask
        self._states: dict[str, list[str]] = {}
        self._failures = 0

    def comment(self, ticket_id: str, text: str, *, mention_author: bool = False) -> WriteResult:
        return self._tracked(lambda: self._comment(ticket_id, text, mention_author))

    def transition(
        self, ticket_id: str, to_state: str, *, allowed_from: Sequence[str]
    ) -> WriteResult:
        return self._tracked(lambda: self._transition(ticket_id, to_state, allowed_from))

    def _tracked(self, write: Callable[[], WriteResult]) -> WriteResult:
        """Недоступная доска не должна тормозить прогон: после серии сбоев запись прекращается."""
        if self._failures >= MAX_FAILURES:
            return WriteResult("skipped", "доска недоступна: запись прекращена до конца прогона")
        result = write()
        if result.status == "failed":
            self._failures += 1
        elif result.ok:
            self._failures = 0
        return result

    def _comment(self, ticket_id: str, text: str, mention_author: bool) -> WriteResult:
        try:
            item_id = int(ticket_id)
            text = self._mask(text)
            mention: tuple[str, str] | None = None
            note = ""
            if mention_author:
                name, identity_id = self._author(item_id)
                if name and identity_id:
                    mention = (name, identity_id)
                elif name:  # без идентификатора уведомление не придёт: хотя бы подпись в тексте
                    text, note = f"@{name}: {text}", "; без уведомления: нет id автора"
                else:
                    note = "; автора тикета определить не удалось"
            comment_id = self._client.add_comment(item_id, render_comment_html(text, mention))
        except (TfsError, ValueError) as e:
            return WriteResult("failed", self._mask(str(e)))
        return WriteResult("done", f"комментарий {comment_id}{note}")

    def _author(self, item_id: int) -> tuple[str, str]:
        """Имя и идентификатор автора тикета (`System.CreatedBy`); пустые строки, если их нет."""
        author = self._client.work_item_brief(item_id).get("fields", {}).get("System.CreatedBy")
        if not isinstance(author, dict):
            return "", ""
        return str(author.get("displayName") or ""), str(author.get("id") or "")

    def _transition(
        self, ticket_id: str, to_state: str, allowed_from: Sequence[str]
    ) -> WriteResult:
        try:
            item_id = int(ticket_id)
            item = self._client.work_item_brief(item_id)
            fields = item.get("fields", {})
            current = str(fields.get("System.State", ""))
            if current == to_state:
                return WriteResult("skipped", f"уже в состоянии {to_state}")
            if ANY_STATE not in allowed_from and current not in allowed_from:
                return WriteResult(
                    "skipped", f"состояние {current!r} не из {list(allowed_from)}: человек опередил"
                )
            item_type = str(fields.get("System.WorkItemType", ""))
            known = self._type_states(item_type)
            if to_state not in known:
                return WriteResult(
                    "skipped", f"состояния {to_state!r} нет у типа {item_type!r}: есть {known}"
                )
            rev = item.get("rev")
            if not isinstance(rev, int) or isinstance(rev, bool):
                return WriteResult("failed", "в ответе TFS нет ревизии тикета")
            self._client.set_state(item_id, to_state, rev)
        except TfsConflictError:
            return WriteResult("skipped", "тикет изменили во время записи: состояние не тронуто")
        except (TfsError, ValueError) as e:
            return WriteResult("failed", self._mask(str(e)))
        return WriteResult("done", f"{current} → {to_state}")

    def _type_states(self, item_type: str) -> list[str]:
        if item_type not in self._states:
            self._states[item_type] = self._client.type_states(item_type)
        return self._states[item_type]


class _Client(Protocol):
    """То, что нужно от `TfsClient` (удобно подменять в тестах)."""

    def work_item_brief(self, item_id: int) -> dict[str, Any]: ...

    def type_states(self, item_type: str) -> list[str]: ...

    def add_comment(self, item_id: int, html: str) -> int: ...

    def set_state(self, item_id: int, state: str, rev: int) -> None: ...
