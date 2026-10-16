"""Профили репозиториев: куда агенты вносят правки и какие знания им подмешивать (S15, D08).

Профили описываются в `config/repos.yaml` (путь: `ARIO_REPOS`). Если файла нет, профиль `rx`
собирается из `RX_PATH`, `BASE_BRANCH`, `BRANCH_TEMPLATE`, `ARIO_BUILD_COMMAND`: поведение
прежнее. Профиль выбирается один раз на тикет (по area path и тегам) и пишется в `state.json`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ario.branches import DEFAULT_BRANCH_TEMPLATE, BranchTemplateError, validate_branch_template
from ario.config import ConfigError, Settings, parse_command
from ario.ticket import Ticket

LEGACY_PROFILE = "rx"  # имя профиля, собранного из `.env`
NO_REPO = "Репозиторий не настроен (задайте RX_PATH или профили в ARIO_REPOS)"


class RepoSelectionError(Exception):
    """Для тикета нельзя однозначно выбрать репозиторий; тикет не берётся в работу."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PublishConfig:
    """Куда оркестратор отправляет ветку тикета и где открывает PR (S17)."""

    url: str  # адрес коллекции git-сервера, без проекта (например, https://host/Collection)
    project: str
    repository: str
    target_branch: str  # в какую ветку открывается PR
    pat_env: str = "GIT_PAT"  # имя переменной окружения с токеном (сам токен в профиле не хранится)
    remote: str = "origin"
    draft: bool = False
    commit_template: str = "{id}: {title}"
    pr_title_template: str = "{id}: {title}"
    author_name: str | None = None  # автор коммита; пусто: настройки git на ноде
    author_email: str | None = None


@dataclass(frozen=True)
class RepoProfile:
    name: str
    path: Path
    base_branch: str = "master"
    branch_template: str = DEFAULT_BRANCH_TEMPLATE
    build_command: tuple[str, ...] = ()  # пусто: `dotnet build` проекта
    knowledge_dir: Path = Path(".")  # где лежат AGENTS.md, agents/, skills/ этого репозитория
    write_deny_extra: tuple[str, ...] = ()  # правила запрета записи сверх общих (guardrails)
    area_paths: tuple[str, ...] = ()  # префиксы area path, по которым выбирается профиль
    tags: tuple[str, ...] = ()  # теги тикета, по которым выбирается профиль
    publish: PublishConfig | None = None  # push и PR; без блока ветка остаётся локальной
    flow_path: Path | None = None  # поток «этап → состояние доски» этого репозитория (S22)
    description: str = ""  # для чего репозиторий: по нему тикет относят к репозиторию (S23)


class _Match(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area_path: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class _Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1)
    project: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    target_branch: str | None = None  # по умолчанию base_branch репозитория
    pat_env: str = Field(default="GIT_PAT", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    remote: str = Field(default="origin", pattern=r"^[\w][\w.-]*$")
    draft: bool = False
    commit_template: str = "{id}: {title}"
    pr_title_template: str = "{id}: {title}"
    author_name: str | None = None
    author_email: str | None = None

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("адрес git-сервера должен начинаться с https://")
        return value.rstrip("/")


class _Repo(BaseModel):
    model_config = ConfigDict(extra="forbid")  # опечатка в ключе не должна молча менять поведение
    path: str
    base_branch: str = "master"
    branch_template: str = DEFAULT_BRANCH_TEMPLATE
    build_command: str = ""
    knowledge_dir: str = "."
    write_deny_extra: list[str] = Field(default_factory=list)
    match: _Match = Field(default_factory=_Match)
    publish: _Publish | None = None
    flow: str | None = None  # файл потока состояний; пусто: поток доски
    description: str = ""

    @field_validator("path")
    @classmethod
    def _path_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("путь к репозиторию не задан")
        return value


class _ReposFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: str | None = None
    repos: dict[str, _Repo] = Field(min_length=1)  # без репозиториев работа не имеет смысла


def _publish(name: str, raw: _Publish | None, base_branch: str) -> PublishConfig | None:
    if raw is None:
        return None
    for field_name in ("commit_template", "pr_title_template"):
        try:  # шаблон проверяется сразу, а не на первом тикете
            getattr(raw, field_name).format(id="1", title="t")
        except (KeyError, IndexError, ValueError) as e:
            raise ConfigError(f"repos.yaml, репозиторий {name}: publish.{field_name}: {e}") from e
    return PublishConfig(
        url=raw.url,
        project=raw.project,
        repository=raw.repository,
        target_branch=raw.target_branch or base_branch,
        pat_env=raw.pat_env,
        remote=raw.remote,
        draft=raw.draft,
        commit_template=raw.commit_template,
        pr_title_template=raw.pr_title_template,
        author_name=raw.author_name,
        author_email=raw.author_email,
    )


def _profile(name: str, raw: _Repo, base: Path, project_dir: Path) -> RepoProfile:
    try:
        template = validate_branch_template(raw.branch_template)
    except BranchTemplateError as e:
        raise ConfigError(f"repos.yaml, репозиторий {name}: branch_template: {e}") from e
    return RepoProfile(
        name=name,
        path=(base / raw.path).resolve(),  # относительный путь считается от папки repos.yaml
        base_branch=raw.base_branch,
        branch_template=template,
        build_command=parse_command(raw.build_command),
        knowledge_dir=(project_dir / raw.knowledge_dir).resolve(),  # от ARIO_PROJECT_DIR
        write_deny_extra=tuple(raw.write_deny_extra),
        area_paths=tuple(raw.match.area_path),
        tags=tuple(raw.match.tags),
        publish=_publish(name, raw.publish, raw.base_branch),
        flow_path=(base / raw.flow).resolve() if raw.flow else None,
        description=raw.description.strip(),
    )


def _prefix_match(area: str | None, prefixes: tuple[str, ...]) -> bool:
    if not area:
        return False
    value = area.strip().lower().rstrip("\\/")
    for prefix in prefixes:
        p = prefix.strip().lower().rstrip("\\/")
        if p and (value == p or value.startswith((p + "\\", p + "/"))):
            return True
    return False


class RepoRegistry:
    def __init__(self, profiles: Mapping[str, RepoProfile], default: str | None = None) -> None:
        if default is not None and default not in profiles:
            raise ConfigError(f"repos.yaml: default={default}, но такого репозитория нет")
        self._profiles = dict(profiles)
        self._default = default

    @property
    def names(self) -> list[str]:
        return list(self._profiles)

    @property
    def profiles(self) -> list[RepoProfile]:
        return list(self._profiles.values())

    @property
    def default(self) -> RepoProfile | None:
        return self._profiles[self._default] if self._default else None

    def get(self, name: str) -> RepoProfile:
        try:
            return self._profiles[name]
        except KeyError:
            known = ", ".join(self._profiles) or "нет"
            raise RepoSelectionError(
                f"Репозиторий {name} не описан в профилях (есть: {known})"
            ) from None

    def select(self, ticket: Ticket, override: str | None = None) -> RepoProfile | None:
        """Профиль для тикета; `None`, если профилей нет вообще (репозиторий не настроен)."""
        if override:
            return self.get(override)
        if not self._profiles:
            return None
        tags = {t.strip().lower() for t in ticket.tags}
        matches = [
            p
            for p in self._profiles.values()
            if _prefix_match(ticket.area, p.area_paths) or tags & {t.lower() for t in p.tags}
        ]
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise RepoSelectionError(
                f"Тикет подходит сразу к нескольким репозиториям ({names}): уточните правила "
                "match в repos.yaml или укажите --repo"
            )
        if matches:
            return matches[0]
        if self.default is not None:
            return self.default
        raise RepoSelectionError(
            "Тикет не подошёл ни к одному репозиторию и default не задан: "
            f"area path={ticket.area!r}, теги={ticket.tags}"
        )

    @classmethod
    def load(cls, path: Path, project_dir: Path) -> RepoRegistry:
        """Читает `repos.yaml`; ошибка схемы — `ConfigError` (fail closed)."""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            parsed = _ReposFile.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as e:
            raise ConfigError(f"{path}: {e}") from e
        return cls.from_repositories(parsed.repos, parsed.default, path.parent, project_dir)

    @classmethod
    def from_repositories(
        cls,
        repositories: Mapping[str, _Repo],
        default: str | None,
        base: Path,
        project_dir: Path,
    ) -> RepoRegistry:
        """Реестр из разобранного блока репозиториев (`repos:` или `repositories:`)."""
        profiles = {
            name: _profile(name, raw, base, project_dir) for name, raw in repositories.items()
        }
        return cls(profiles, default or (next(iter(profiles)) if len(profiles) == 1 else None))

    @classmethod
    def from_settings(cls, settings: Settings) -> RepoRegistry:
        """Файл доски или `repos.yaml`, если заданы; иначе профиль `rx` из настроек `.env`."""
        if settings.board_path is not None:
            from ario.board import BoardConfig

            return BoardConfig.load(settings.board_path).registry(settings.project_dir)
        if settings.repos_path is not None:
            return cls.load(settings.repos_path, settings.project_dir)
        if settings.rx_path is None:
            return cls({})
        legacy = RepoProfile(
            name=LEGACY_PROFILE,
            path=settings.rx_path,
            base_branch=settings.base_branch,
            branch_template=settings.branch_template,
            build_command=settings.build_command,
            knowledge_dir=settings.project_dir,
        )
        return cls({LEGACY_PROFILE: legacy}, LEGACY_PROFILE)
