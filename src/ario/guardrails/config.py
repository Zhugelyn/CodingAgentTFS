"""Схема `guardrails.yaml`."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # опечатка в ключе не должна молча ослаблять защиту


class LimitsConfig(_Strict):
    max_steps: dict[str, int]
    max_context_tokens: int = Field(gt=0)
    ticket_timeout_seconds: int = Field(gt=0)
    model_retries: int = Field(ge=1)


class PatternRule(_Strict):
    id: str
    regex: str


class InjectionConfig(_Strict):
    patterns: list[PatternRule]


class PathsConfig(_Strict):
    write_deny: list[str]
    read_deny: list[str]


class EnvConfig(_Strict):
    secret_name_pattern: str
    keep_name_pattern: str | None = None  # имена, которые остаются, даже если похожи на секреты


class GuardrailsConfig(_Strict):
    limits: LimitsConfig
    injection: InjectionConfig
    paths: PathsConfig
    env: EnvConfig


def load_config(path: Path) -> GuardrailsConfig:
    """Читает `guardrails.yaml`. Нет файла или ошибка схемы — исключение (fail closed)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return GuardrailsConfig.model_validate(data)
