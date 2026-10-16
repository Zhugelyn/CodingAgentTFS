"""Имена этапов потока: единый источник для этапов агентов, отчёта на доске и разбора трассы."""

from __future__ import annotations

ROUTING = "routing"  # выбор репозитория тикета (S23): перед analyst
ANALYSIS = "analysis"
DEVELOPMENT = "development"
REVIEW = "review"
PUBLISH = "publish"


# Доработка по замечаниям человека (PR): попытки 3, 4, 5 (раунды 1, 2, 3). Попытки 1 и 2 — исходная
# разработка и доработка по замечаниям reviewer.
FIRST_HUMAN_ATTEMPT = 3
MAX_HUMAN_REWORKS = 3


def human_attempt(round_number: int) -> int:
    """Номер попытки developer и reviewer для раунда доработки по замечаниям человека."""
    return FIRST_HUMAN_ATTEMPT + round_number - 1


def human_round(attempt: int) -> int | None:
    """Номер раунда доработки по номеру попытки; `None`, если попытка не человеческая."""
    return attempt - FIRST_HUMAN_ATTEMPT + 1 if attempt >= FIRST_HUMAN_ATTEMPT else None


def rework_file(round_number: int) -> str:
    """Замечания человека раунда лежат в `runs/<id>/rework-<n>.md`."""
    return f"rework-{round_number}.md"


def development_stage(attempt: int) -> str:
    return DEVELOPMENT if attempt == 1 else f"{DEVELOPMENT}-{attempt}"


def review_stage(attempt: int) -> str:
    return REVIEW if attempt == 1 else f"{REVIEW}-{attempt}"
