"""Модель тикета и разбор файла `<id>.md` (формат — решение D01)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

MAX_TICKET_BYTES = 1_000_000
MAX_HEADER_CHARS = 300  # заголовок комментария длиннее не разбираем (защита от ReDoS)
_FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_COMMENT_HEADER = re.compile(r"^(?P<author>.+?)\s*·\s*(?P<date>\S+)\s*$")


class TicketError(ValueError):
    """Файл тикета не соответствует формату."""


class Comment(BaseModel):
    author: str
    date: str
    text: str


class Ticket(BaseModel):
    id: str
    type: str
    title: str
    state: str | None = None
    area: str | None = None
    iteration: str | None = None
    priority: int | None = None
    severity: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str = "folder"
    description: str
    acceptance_criteria: str | None = None
    discussion: list[Comment] = Field(default_factory=list)


_TOP_SECTIONS = frozenset({"Description", "Acceptance Criteria", "Discussion"})


def _split_sections(body: str, level: str, known: frozenset[str] | None = None) -> dict[str, str]:
    """Делит текст на секции по заголовкам уровня `level` (`##` или `###`).

    Если задан `known`, заголовки с другими именами остаются обычным текстом секции
    (например, `## ПОЯСНЕНИЕ` внутри Repro Steps не обрезает Description).
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        name = line[len(level) + 1 :].strip()
        if (
            line.startswith(level + " ")
            and not line.startswith(level + "#")
            and (known is None or name in known)
        ):
            current = name
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _parse_discussion(text: str) -> list[Comment]:
    comments: list[Comment] = []
    for header, content in _split_sections(text, "###").items():
        match = _COMMENT_HEADER.match(header) if len(header) <= MAX_HEADER_CHARS else None
        if match is None:
            raise TicketError(f"Заголовок комментария без автора и даты: {header!r}")
        comments.append(Comment(author=match["author"].strip(), date=match["date"], text=content))
    return comments


def parse_ticket(text: str) -> Ticket:
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise TicketError("Нет front matter (блок `---` в начале файла)")
    try:
        meta: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise TicketError(f"Некорректный YAML в front matter: {e}") from e
    if not isinstance(meta, dict):
        raise TicketError("Front matter должен быть словарём")
    sections = _split_sections(match.group(2), "##", _TOP_SECTIONS)
    description = sections.get("Description", "")
    if not description:
        raise TicketError("Нет обязательной секции `## Description`")
    missing = [k for k in ("id", "type", "title") if not meta.get(k)]
    if missing:
        raise TicketError(f"Нет обязательных полей: {', '.join(missing)}")
    meta["id"] = str(meta["id"])
    meta.pop("description", None)
    try:
        return Ticket(
            **meta,
            description=description,
            acceptance_criteria=sections.get("Acceptance Criteria") or None,
            discussion=_parse_discussion(sections.get("Discussion", "")),
        )
    except (ValidationError, TypeError) as e:
        raise TicketError(f"Некорректные поля тикета: {e}") from e


def load_ticket(path: Path) -> Ticket:
    try:
        if path.stat().st_size > MAX_TICKET_BYTES:
            raise TicketError(f"Файл тикета больше {MAX_TICKET_BYTES} байт")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise TicketError(f"Не удалось прочитать файл тикета: {e}") from e
    return parse_ticket(text)


def _escape_headings(text: str) -> str:
    """Строка, начинающаяся с `#`, стала бы заголовком секции файла: экранируем."""
    return "\n".join("\\" + line if line.startswith("#") else line for line in text.split("\n"))


def render_ticket(ticket: Ticket) -> str:
    """Собирает файл `<id>.md` формата D01 (обратное к `parse_ticket`)."""
    meta: dict[str, Any] = {"id": ticket.id, "type": ticket.type, "title": ticket.title}
    optional: dict[str, Any] = {
        "state": ticket.state,
        "area": ticket.area,
        "iteration": ticket.iteration,
        "priority": ticket.priority,
        "severity": ticket.severity,
        "tags": ticket.tags,
    }
    meta.update({key: value for key, value in optional.items() if value not in (None, [])})
    meta["source"] = ticket.source
    front = yaml.safe_dump(
        meta, allow_unicode=True, sort_keys=False, default_flow_style=False, width=10_000
    ).rstrip("\n")
    parts = [f"---\n{front}\n---", "## Description", _escape_headings(ticket.description)]
    if ticket.acceptance_criteria:
        parts += ["", "## Acceptance Criteria", _escape_headings(ticket.acceptance_criteria)]
    if ticket.discussion:
        parts += ["", "## Discussion"]
        for comment in ticket.discussion:
            author = " ".join(comment.author.split())
            date = " ".join(comment.date.split())
            parts += [f"### {author} · {date}", _escape_headings(comment.text)]
    return "\n".join(parts) + "\n"
