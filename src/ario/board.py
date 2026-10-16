"""Конфигурация доски: откуда берутся тикеты и в каких репозиториях ведётся работа (S22).

Один файл (`ARIO_BOARD`, образец `config/board.yaml.example`) описывает доску TFS и
репозитории, которые она обслуживает: у каждого репозитория свои настройки и свой поток
состояний. Одна доска на машину. Токены в файле не хранятся: только имена переменных окружения.
Без `ARIO_BOARD` работают прежние `TFS_*` и `ARIO_REPOS` из `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ario.config import DEFAULT_TFS_MAX_ITEMS, ConfigError, Settings
from ario.repos import RepoRegistry, _Repo

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


class _Board(BaseModel):
    model_config = ConfigDict(extra="forbid")  # опечатка в ключе не должна молча менять поведение
    url: str = Field(min_length=1)  # адрес коллекции Azure DevOps Server, без проекта
    project: str = Field(min_length=1)
    query: str | None = None  # WIQL отбора тикетов; пусто: запрос по умолчанию
    max_items: int = Field(default=DEFAULT_TFS_MAX_ITEMS, ge=1)  # новых тикетов за один fetch
    pat_env: str = Field(default="TFS_PAT", pattern=_ENV_NAME)  # токен чтения
    write_pat_env: str = Field(default="TFS_WRITE_PAT", pattern=_ENV_NAME)  # токен записи
    flow: str | None = None  # поток состояний по умолчанию для всех репозиториев
    work_item_types: list[str] = Field(default_factory=lambda: ["Bug"], min_length=1)

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("адрес доски должен начинаться с https://")
        return value.rstrip("/")


class _BoardFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board: _Board
    default: str | None = None
    repositories: dict[str, _Repo] = Field(min_length=1)  # без репозиториев работа не имеет смысла


@dataclass(frozen=True)
class BoardConfig:
    path: Path
    file: _BoardFile

    @classmethod
    def load(cls, path: Path) -> BoardConfig:
        """Читает файл доски; ошибка схемы — `ConfigError` (fail closed)."""
        try:
            parsed = _BoardFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as e:
            raise ConfigError(f"{path}: {e}") from e
        if parsed.default is not None and parsed.default not in parsed.repositories:
            raise ConfigError(f"{path}: default={parsed.default}, но такого репозитория нет")
        return cls(path.resolve(), parsed)

    @property
    def flow_path(self) -> Path | None:
        flow = self.file.board.flow
        return (self.path.parent / flow).resolve() if flow else None

    def registry(self, project_dir: Path) -> RepoRegistry:
        return RepoRegistry.from_repositories(
            self.file.repositories, self.file.default, self.path.parent, project_dir
        )

    def apply(self, settings: Settings) -> Settings:
        """Настройки доски из файла поверх `TFS_*` из `.env`; токены читаются по имени."""
        board = self.file.board
        return replace(
            settings,
            tfs_url=board.url,
            tfs_project=board.project,
            tfs_pat=(os.environ.get(board.pat_env) or "").strip() or None,
            tfs_write_pat=(os.environ.get(board.write_pat_env) or "").strip() or None,
            tfs_wiql=board.query.strip() if board.query and board.query.strip() else None,
            tfs_max_items=board.max_items,
            ticket_flow_path=self.flow_path,
            work_item_types=tuple(board.work_item_types),
        )
