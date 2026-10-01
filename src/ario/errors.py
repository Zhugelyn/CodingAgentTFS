"""Общие исключения."""

from __future__ import annotations


class StageFailed(Exception):
    """Этап штатно отказал: тикет уходит в `needs-human/` с этой причиной."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LimitExceeded(StageFailed):
    """Превышен лимит (шаги, токены, время) или исчерпаны повторы при сбое модели."""


class InjectionDetected(StageFailed):
    """Недоверенный текст сработал на правило injection-сканера."""


class GuardViolation(ValueError):
    """Действие агента запрещено границами путей или команд.

    Не останавливает тикет: сообщение возвращается агенту как ошибка инструмента.
    """
