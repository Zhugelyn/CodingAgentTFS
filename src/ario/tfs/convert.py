"""Преобразование work item и комментариев TFS в модель `Ticket` (формат D01)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ario.ticket import Comment, Ticket, TicketError

_BLOCK_TAGS = frozenset(
    {"div", "p", "ul", "ol", "table", "tr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIPPED_TAGS = frozenset({"script", "style", "head"})
_BLANK_RUNS = re.compile(r"\n{3,}")
MAX_ATTACHMENT_NAME = 100


class _TextExtractor(HTMLParser):
    """HTML из полей TFS (div, b, br, span, a, списки, картинки) в обычный текст."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0
        self._links: list[str | None] = []
        self._open = False  # в текущей строке уже есть текст

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag == "br":
            self._parts.append("\n")
            self._open = False
        elif tag in _BLOCK_TAGS:
            self._break()
        elif tag == "li":
            self._break()
            self._parts.append("- ")
            self._open = True
        elif tag in ("td", "th"):
            if self._open:  # соседние ячейки строки не должны слипаться
                self._parts.append(" | ")
        elif tag == "a":
            self._links.append(dict(attrs).get("href"))
        elif tag == "img" and not self._skip:
            self._parts.append(f"[вложение: {_attachment_name(dict(attrs))}]")
            self._open = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip = max(self._skip - 1, 0)
        elif tag in _BLOCK_TAGS or tag == "li":
            self._break()
        elif tag == "a" and self._links:
            href = self._links.pop()
            if href and href.startswith(("http://", "https://")):
                self._parts.append(f" ({href})")
                self._open = True

    def _break(self) -> None:
        """Граница блока: перевод строки, если строка ещё не закончена (пустые не плодим)."""
        if self._open:
            self._parts.append("\n")
            self._open = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._parts.append(data)
        visible = data.rstrip(" \xa0\t")
        if visible.strip("\n"):
            self._open = not visible.endswith("\n")
        elif "\n" in data:
            self._open = False

    def text(self) -> str:
        return "".join(self._parts)


def _attachment_name(attrs: dict[str, str | None]) -> str:
    src = attrs.get("src") or ""
    parsed = urlparse(src)
    name = ""
    if parsed.scheme in ("http", "https"):  # data:-адрес — это сама картинка, имени у неё нет
        name = (parse_qs(parsed.query).get("fileName") or [""])[0] or unquote(
            parsed.path.rsplit("/", 1)[-1]
        )
    label = " ".join((attrs.get("alt") or name or "изображение").split())
    return label[:MAX_ATTACHMENT_NAME]


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in parser.text().splitlines()]
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def _neutralize_headings(text: str) -> str:
    """Строки, начинающиеся с `#`, экранируются: иначе они стали бы секциями файла тикета."""
    return "\n".join("\\" + line if line.startswith("#") else line for line in text.split("\n"))


def _clean(html: Any) -> str:
    return _neutralize_headings(html_to_text(html)) if isinstance(html, str) else ""


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _optional_line(value: Any) -> str | None:
    return _one_line(value) if value else None


def _person(value: Any) -> str:
    name = value.get("displayName") if isinstance(value, dict) else value
    return _one_line(name) if name else "неизвестный автор"


def _tags(raw: Any) -> list[str]:
    return [t.strip() for t in str(raw or "").split(";") if t.strip()]


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def to_discussion(comments: list[dict[str, Any]]) -> list[Comment]:
    """Комментарии от старых к новым (TFS отдаёт новые первыми); пустые и удалённые пропускаются."""
    ordered = sorted(comments, key=lambda c: str(c.get("createdDate") or ""))
    discussion: list[Comment] = []
    for comment in ordered:
        text = _clean(comment.get("text"))
        if comment.get("isDeleted") or not text:
            continue
        date = str(comment.get("createdDate") or "")[:10] or "неизвестно"
        discussion.append(
            Comment(author=_person(comment.get("createdBy")), date=_one_line(date), text=text)
        )
    return discussion


def to_ticket(work_item: dict[str, Any], comments: list[dict[str, Any]]) -> Ticket:
    fields: dict[str, Any] = work_item.get("fields") or {}
    item_type = _one_line(fields.get("System.WorkItemType") or "")
    title = _one_line(fields.get("System.Title") or "")
    raw_id = fields.get("System.Id", work_item.get("id"))
    if not raw_id or not item_type or not title:
        raise TicketError("В work item нет id, типа или заголовка")
    repro = _clean(fields.get("Microsoft.VSTS.TCM.ReproSteps"))
    plain = _clean(fields.get("System.Description"))
    description = (repro or plain) if item_type.casefold() == "bug" else (plain or repro)
    if not description:
        raise TicketError(f"У work item {raw_id} пустое описание (Repro Steps / Description)")
    return Ticket(
        id=str(raw_id),
        type=item_type,
        title=title,
        state=_optional_line(fields.get("System.State")),
        area=_optional_line(fields.get("System.AreaPath")),
        iteration=_optional_line(fields.get("System.IterationPath")),
        priority=_optional_int(fields.get("Microsoft.VSTS.Common.Priority")),
        severity=_optional_line(fields.get("Microsoft.VSTS.Common.Severity")),
        tags=_tags(fields.get("System.Tags")),
        source="tfs",
        description=description,
        acceptance_criteria=_clean(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")) or None,
        discussion=to_discussion(comments),
    )
