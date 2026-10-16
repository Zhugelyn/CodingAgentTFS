"""Конфигурация из переменных окружения и `.env`."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from ario.branches import DEFAULT_BRANCH_TEMPLATE, BranchTemplateError, validate_branch_template


class ConfigError(ValueError):
    """Неверная настройка в `.env`."""


def _branch_template(raw: str | None) -> str:
    try:
        return validate_branch_template(
            raw.strip() if raw and raw.strip() else DEFAULT_BRANCH_TEMPLATE
        )
    except BranchTemplateError as e:
        raise ConfigError(f"BRANCH_TEMPLATE: {e}") from e


DEFAULT_TFS_MAX_ITEMS = 5
DEFAULT_POLL_MINUTES = 5
DEFAULT_WEBHOOK_PORT = 8765
DEFAULT_WEBHOOK_HOST = "127.0.0.1"


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes")


# OpenHands SDK сам шлёт трассы со ВСЕМ содержимым запросов (код RX, тикеты), если задана
# любая из этих переменных; свой безопасный экспорт идёт через ARIO_OTLP_ENDPOINT (решение D07)
_SDK_TELEMETRY_VARS = (
    "OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "LMNR_PROJECT_API_KEY",
)


def check_sdk_telemetry(environ: Mapping[str, str]) -> None:
    """Отказ, если SDK начнёт слать содержимое запросов во внешнюю телеметрию."""
    if _flag(environ.get("ARIO_ALLOW_SDK_TELEMETRY")):
        return
    found = [
        name
        for name, value in environ.items()
        if value.strip() and (name in _SDK_TELEMETRY_VARS or name.startswith("LMNR_"))
    ]
    if found:
        raise ConfigError(
            f"Заданы {', '.join(sorted(found))}: SDK отправит во внешнюю телеметрию тексты "
            "запросов к модели (код RX, тикеты). Уберите переменные или для безопасного "
            "экспорта метрик используйте ARIO_OTLP_ENDPOINT. Осознанный обход: "
            "ARIO_ALLOW_SDK_TELEMETRY=1."
        )


# ANALYST_MODEL, DEVELOPER_MODEL, REVIEWER_MODEL, ROUTER_MODEL (выбор репозитория, S23)
ROLES = ("analyst", "developer", "reviewer", "router")


def _positive_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{name}: нужно целое число не меньше 1, получено {raw!r}")
    return value


def _port(name: str, raw: str | None, default: int) -> int:
    value = _positive_int(name, raw, default)
    if value > 65535:
        raise ConfigError(f"{name}: порт должен быть от 1 до 65535, получено {value}")
    return value


def _optional_path(raw: str | None) -> Path | None:
    """Путь к необязательному файлу настроек только явный: файл рядом не подхватывается сам."""
    return Path(raw.strip()).resolve() if raw and raw.strip() else None


def parse_command(text: str | None) -> tuple[str, ...]:
    """Разбирает строку команды из `.env`; кавычки вокруг частей снимаются, `\\` остаются."""
    if not text or not text.strip():
        return ()
    parts = shlex.split(text, posix=os.name != "nt")
    return tuple(part.strip('"') for part in parts)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    rx_path: Path | None
    llm_model: str
    openrouter_api_key: str | None
    guardrails_path: Path
    repos_path: Path | None = None  # профили репозиториев (repos.yaml); нет файла: профиль из .env
    base_branch: str = "master"
    branch_template: str = DEFAULT_BRANCH_TEMPLATE  # имя ветки тикета, например antares/ai-{id}
    project_dir: Path = field(default_factory=Path.cwd)  # AGENTS.md, agents/, skills/
    build_command: tuple[str, ...] = ()  # штатная команда сборки RX; пусто: dotnet build проекта
    tfs_url: str | None = None  # адрес коллекции Azure DevOps Server, без проекта
    tfs_project: str | None = None
    tfs_pat: str | None = field(default=None, repr=False)  # секрет: не попадает в repr
    # запись на доску (S16): выключена, пока не задан TFS_WRITE; токен записи можно держать
    # отдельно от токена чтения (TFS_WRITE_PAT), иначе пишет TFS_PAT
    publish: bool = False  # push и PR (S17): выключено, пока не задан ARIO_PUBLISH=1
    tfs_write: bool = False
    tfs_write_pat: str | None = field(default=None, repr=False)
    ticket_flow_path: Path | None = (
        None  # поток «этап → состояние» (образец ticket-flow.yaml.example)
    )
    tfs_wiql: str | None = None  # пусто: запрос по умолчанию (ario.tfs.fetch.default_wiql)
    poll_minutes: int = DEFAULT_POLL_MINUTES  # как часто `ario watch` опрашивает TFS
    webhook_host: str = DEFAULT_WEBHOOK_HOST  # `ario watch --webhook`: где слушать
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    webhook_secret: str | None = field(default=None, repr=False)  # пароль Basic-авторизации
    tfs_max_items: int = DEFAULT_TFS_MAX_ITEMS  # сколько новых тикетов берёт один `ario fetch`
    board_path: Path | None = (
        None  # файл конфигурации доски (S22); задан: TFS_* и ARIO_REPOS не нужны
    )
    work_item_types: tuple[str, ...] = ("Bug",)  # типы тикетов, по которым проверяется поток
    otlp_llm_content: bool = False  # живая выгрузка текста запросов к модели (только локально)
    allow_remote_llm_content: bool = False  # осознанный обход ограничения «только localhost»
    otlp_endpoint: str | None = None  # OTLP/HTTP приёмник (Phoenix): автоэкспорт метрик прогона
    # модель роли, если она отличается от `llm_model`: {"analyst": "...", "developer": "..."}
    role_models: dict[str, str] = field(default_factory=dict)

    @property
    def tickets_dir(self) -> Path:
        return self.data_dir / "tickets"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def traces_dir(self) -> Path:
        return self.data_dir / "traces"

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        load_dotenv(env_file or find_dotenv(usecwd=True))
        settings = cls._from_environment()
        if settings.board_path is None:
            return settings
        if settings.repos_path is not None:
            raise ConfigError("Заданы и ARIO_BOARD, и ARIO_REPOS: оставьте что-то одно")
        from ario.board import BoardConfig  # board.py сам импортирует config

        return BoardConfig.load(settings.board_path).apply(settings)

    @classmethod
    def _from_environment(cls) -> Settings:
        rx = os.environ.get("RX_PATH")
        return cls(
            board_path=_optional_path(os.environ.get("ARIO_BOARD")),
            data_dir=Path(os.environ.get("ARIO_DATA_DIR") or ".").resolve(),
            rx_path=Path(rx) if rx else None,
            llm_model=os.environ.get("LLM_MODEL", "openrouter/qwen/qwen3.8-27b"),
            role_models={
                role: model
                for role in ROLES
                if (model := (os.environ.get(f"{role.upper()}_MODEL") or "").strip())
            },
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            base_branch=os.environ.get("BASE_BRANCH") or "master",
            branch_template=_branch_template(os.environ.get("BRANCH_TEMPLATE")),
            project_dir=Path(os.environ.get("ARIO_PROJECT_DIR") or ".").resolve(),
            build_command=parse_command(os.environ.get("ARIO_BUILD_COMMAND")),
            tfs_url=(os.environ.get("TFS_URL") or "").strip() or None,
            tfs_project=(os.environ.get("TFS_PROJECT") or "").strip() or None,
            tfs_pat=(os.environ.get("TFS_PAT") or "").strip() or None,
            tfs_wiql=(os.environ.get("TFS_WIQL") or "").strip() or None,
            publish=_flag(os.environ.get("ARIO_PUBLISH")),
            tfs_write=_flag(os.environ.get("TFS_WRITE")),
            tfs_write_pat=(os.environ.get("TFS_WRITE_PAT") or "").strip() or None,
            ticket_flow_path=_optional_path(os.environ.get("ARIO_TICKET_FLOW")),
            otlp_endpoint=(os.environ.get("ARIO_OTLP_ENDPOINT") or "").strip() or None,
            otlp_llm_content=_flag(os.environ.get("ARIO_OTLP_LLM_CONTENT")),
            allow_remote_llm_content=_flag(os.environ.get("ARIO_ALLOW_REMOTE_LLM_CONTENT")),
            poll_minutes=_positive_int(
                "ARIO_POLL_MINUTES",
                os.environ.get("ARIO_POLL_MINUTES"),
                DEFAULT_POLL_MINUTES,
            ),
            webhook_host=(os.environ.get("ARIO_WEBHOOK_HOST") or "").strip()
            or DEFAULT_WEBHOOK_HOST,
            webhook_port=_port(
                "ARIO_WEBHOOK_PORT",
                os.environ.get("ARIO_WEBHOOK_PORT"),
                DEFAULT_WEBHOOK_PORT,
            ),
            webhook_secret=(os.environ.get("ARIO_WEBHOOK_SECRET") or "").strip() or None,
            tfs_max_items=_positive_int(
                "TFS_MAX_ITEMS", os.environ.get("TFS_MAX_ITEMS"), DEFAULT_TFS_MAX_ITEMS
            ),
            repos_path=_optional_path(os.environ.get("ARIO_REPOS")),
            guardrails_path=Path(
                os.environ.get("ARIO_GUARDRAILS") or "config/guardrails.yaml"
            ).resolve(),
        )
