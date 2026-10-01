"""Лимиты: токены на запуск, время на тикет, повторы при сбое модели."""

from __future__ import annotations

import time
from collections.abc import Callable

from ario.errors import LimitExceeded

Clock = Callable[[], float]


class Deadline:
    """Лимит времени на тикет."""

    def __init__(self, seconds: float, clock: Clock = time.monotonic, elapsed: float = 0.0) -> None:
        """`elapsed` — сколько секунд тикет уже в работе (для возобновления после падения)."""
        self._clock = clock
        self._seconds = seconds
        self._end = clock() + seconds - elapsed

    def remaining(self) -> float:
        return self._end - self._clock()

    def check(self) -> None:
        if self.remaining() <= 0:
            minutes = self._seconds / 60
            raise LimitExceeded(f"Превышено время на тикет ({minutes:g} мин)")


class TokenBudget:
    """Лимит токенов на один запуск агента; `used` — накопленные prompt+completion."""

    def __init__(self, limit: int) -> None:
        self._limit = limit

    def check(self, used: int) -> None:
        if used > self._limit:
            raise LimitExceeded(f"Превышен лимит токенов на запуск агента: {used} > {self._limit}")


def is_retryable(exc: BaseException) -> bool:
    """Сбой модели, который имеет смысл повторить: 429, 5xx, таймаут/обрыв соединения."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    return isinstance(exc, TimeoutError | ConnectionError)


def call_with_retry[T](
    fn: Callable[[], T],
    attempts: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    base_delay: float = 2.0,
) -> T:
    """Вызывает `fn`, повторяя при временных сбоях. Исчерпание попыток → `LimitExceeded`."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if not is_retryable(e):
                raise
            last = e
            if attempt < attempts:
                sleep(base_delay * 2 ** (attempt - 1))
    raise LimitExceeded(f"Сбой модели после {attempts} попыток: {last}")
