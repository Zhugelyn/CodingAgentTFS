"""Имена этапов потока: единый источник для этапов агентов, отчёта на доске и разбора трассы."""

from __future__ import annotations

ANALYSIS = "analysis"
DEVELOPMENT = "development"
REVIEW = "review"
PUBLISH = "publish"


def development_stage(attempt: int) -> str:
    return DEVELOPMENT if attempt == 1 else f"{DEVELOPMENT}-{attempt}"


def review_stage(attempt: int) -> str:
    return REVIEW if attempt == 1 else f"{REVIEW}-{attempt}"
