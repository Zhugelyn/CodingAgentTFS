"""Имя ветки тикета по шаблону из настройки `BRANCH_TEMPLATE` (по умолчанию `ai/{id}`).

В шаблоне обязателен `{id}`; необязательный `{type}` подставляет тип тикета строчными буквами
(`Bug` → `bug`, `User Story` → `user-story`). Пример: `antares/ai-{id}` → `antares/ai-43031`.
"""

from __future__ import annotations

import re
import string

DEFAULT_BRANCH_TEMPLATE = "ai/{id}"
MAX_BRANCH_LENGTH = 100

_ALLOWED_FIELDS = {"id", "type"}
_VALID_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class BranchTemplateError(ValueError):
    """Шаблон имени ветки неверен."""


def _type_slug(ticket_type: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (ticket_type or "").lower()).strip("-") or "ticket"


def validate_branch_template(template: str) -> str:
    """Проверяет шаблон и возвращает его; ошибка — `BranchTemplateError` с объяснением."""
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as e:
        raise BranchTemplateError(
            f"некорректные фигурные скобки в шаблоне {template!r}: {e}"
        ) from e
    unknown = set(fields) - _ALLOWED_FIELDS
    if unknown:
        raise BranchTemplateError(
            f"неизвестные поля {sorted(unknown)} в шаблоне {template!r}; "
            "допустимы {id} и {type}"
        )
    if fields.count("id") != 1:
        raise BranchTemplateError(f"в шаблоне {template!r} должен быть ровно один {{id}}")
    sample = template.format(id="123", type="bug")
    problems = []
    if not _VALID_NAME.fullmatch(sample):
        problems.append("допустимы латинские буквы, цифры и символы . _ - /")
    if len(sample) > MAX_BRANCH_LENGTH:
        problems.append(f"длиннее {MAX_BRANCH_LENGTH} знаков")
    if ".." in sample or "//" in sample or "/." in sample or sample.endswith(("/", ".", ".lock")):
        problems.append("недопустимое сочетание точек и слэшей, git отвергнет такое имя")
    if problems:
        raise BranchTemplateError(f"шаблон {template!r} даёт имя {sample!r}: {'; '.join(problems)}")
    return template


def branch_name(
    ticket_id: str,
    template: str = DEFAULT_BRANCH_TEMPLATE,
    ticket_type: str | None = None,
) -> str:
    return template.format(id=ticket_id, type=_type_slug(ticket_type))
