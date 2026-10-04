"""Папки состояний тикета: inbox → in-progress → done | needs-human."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ario.trace import utc_now

Folder = Literal["inbox", "in-progress", "done", "needs-human"]
FOLDERS: tuple[Folder, ...] = ("inbox", "in-progress", "done", "needs-human")


_TICKET_ID = re.compile(r"[A-Za-z0-9_-]+")


class InvalidTicketIdError(ValueError):
    """Идентификатор тикета содержит недопустимые символы (защита от выхода из каталога)."""


_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
MAX_TICKET_ID_LENGTH = 64


def validate_ticket_id(ticket_id: str) -> str:
    if (
        not _TICKET_ID.fullmatch(ticket_id)
        or len(ticket_id) > MAX_TICKET_ID_LENGTH
        or ticket_id.casefold() in _RESERVED_NAMES  # имена устройств Windows
    ):
        raise InvalidTicketIdError(f"Недопустимый id тикета: {ticket_id!r}")
    return ticket_id


class TicketNotFoundError(FileNotFoundError):
    """Тикет не найден ни в одной папке состояний."""


class TicketStore:
    def __init__(self, tickets_dir: Path, mask: Callable[[str], str] | None = None) -> None:
        self._dir = tickets_dir
        self._mask = mask or (lambda text: text)

    def ensure_dirs(self) -> None:
        for folder in FOLDERS:
            (self._dir / folder).mkdir(parents=True, exist_ok=True)

    def path(self, ticket_id: str, folder: Folder) -> Path:
        return self._dir / folder / f"{ticket_id}.md"

    def comments_path(self, ticket_id: str, folder: Folder) -> Path:
        return self._dir / folder / f"{ticket_id}.comments.md"

    def locations(self, ticket_id: str) -> list[Folder]:
        return [f for f in FOLDERS if self.path(ticket_id, f).exists()]

    def locate(self, ticket_id: str) -> Folder:
        for folder in FOLDERS:
            if self.path(ticket_id, folder).exists():
                return folder
        raise TicketNotFoundError(f"Тикет {ticket_id} не найден в {self._dir}")

    def move(self, ticket_id: str, src: Folder, dst: Folder) -> None:
        if src == dst:
            return
        self.ensure_dirs()
        shutil.move(self.path(ticket_id, src), self.path(ticket_id, dst))
        comments = self.comments_path(ticket_id, src)
        if comments.exists():
            shutil.move(comments, self.comments_path(ticket_id, dst))

    def add_comment(
        self, ticket_id: str, folder: Folder, agent: str, verdict: str, text: str
    ) -> None:
        """Дописывает запись в `<id>.comments.md` (формат — решение D01)."""
        with self.comments_path(ticket_id, folder).open("a", encoding="utf-8") as f:
            f.write(
                f"## [{agent}] {utc_now()} · verdict: {verdict}\n{self._mask(text).strip()}\n\n"
            )
