"""Безопасный вывод недоверенного текста в PR и комментарии доски (S25, риск R-4).

Текст, который написала модель или автор тикета, не должен становиться активной разметкой:
картинкой с внешним адресом, подменной ссылкой, упоминанием человека. Свободный текст выводится
внутри блока кода, а структуру (заголовки, списки, ссылки) собирает код.
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# невидимые и меняющие направление текста знаки: прячут ссылки и подменяют порядок символов
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_BACKTICKS = re.compile(r"`+")
_MENTION = re.compile(r"@(?=<)")
MIN_FENCE = 3


def clean(text: str) -> str:
    """Убирает управляющие и невидимые знаки; перевод строки и табуляция остаются."""
    return _INVISIBLE.sub("", _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n")))


def _defuse(text: str) -> str:
    """Разрывает `@<идентификатор>`: в упоминание он превратиться не должен."""
    return _MENTION.sub("@ ", clean(text))


def _longest_run(text: str) -> int:
    return max((len(match.group()) for match in _BACKTICKS.finditer(text)), default=0)


def code_block(text: str) -> str:
    """Многострочный текст как блок кода: ограждение длиннее любой цепочки обратных кавычек
    внутри, поэтому закрыть блок изнутри нельзя."""
    body = _defuse(text).strip("\n")
    fence = "`" * max(MIN_FENCE, _longest_run(body) + 1)
    return f"{fence}\n{body}\n{fence}"


def inline_code(text: str) -> str:
    """Одна строка как код в строке (заголовок тикета и т.п.)."""
    body = " ".join(_defuse(text).split())
    fence = "`" * (_longest_run(body) + 1)
    pad = " " if body.startswith("`") or body.endswith("`") else ""
    return f"{fence}{pad}{body}{pad}{fence}"


def close_open_fence(text: str) -> str:
    """Если обрезка оборвала блок кода посередине, дописывает закрывающее ограждение."""
    opener: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) >= MIN_FENCE and set(stripped) == {"`"}:
            if opener is None:
                opener = stripped
            elif stripped == opener:
                opener = None
    return text if opener is None else f"{text}\n{opener}"
