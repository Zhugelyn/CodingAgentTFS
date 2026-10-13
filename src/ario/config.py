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


ROLES = ("analyst", "developer", "reviewer")  # ANALYST_MODEL, DEVELOPER_MODEL, REVIEWER_MODEL


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
    base_branch: str = "master"
    branch_template: str = DEFAULT_BRANCH_TEMPLATE  # имя ветки тикета, например antares/ai-{id}
    project_dir: Path = field(default_factory=Path.cwd)  # AGENTS.md, agents/, skills/
    build_command: tuple[str, ...] = ()  # штатная команда сборки RX; пусто: dotnet build проекта
    tfs_url: str | None = None  # адрес коллекции Azure DevOps Server, без проекта
    tfs_project: str | None = None
    tfs_pat: str | None = field(default=None, repr=False)  # секрет: не попадает в repr
    tfs_wiql: str | None = None  # пусто: запрос по умолчанию (ario.tfs.fetch.default_wiql)
    tfs_max_items: int = DEFAULT_TFS_MAX_ITEMS  # сколько новых тикетов берёт один `ario fetch`
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
        rx = os.environ.get("RX_PATH")
        return cls(
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
            otlp_endpoint=(os.environ.get("ARIO_OTLP_ENDPOINT") or "").strip() or None,
            otlp_llm_content=_flag(os.environ.get("ARIO_OTLP_LLM_CONTENT")),
            allow_remote_llm_content=_flag(os.environ.get("ARIO_ALLOW_REMOTE_LLM_CONTENT")),
            tfs_max_items=_positive_int(
                "TFS_MAX_ITEMS", os.environ.get("TFS_MAX_ITEMS"), DEFAULT_TFS_MAX_ITEMS
            ),
            guardrails_path=Path(
                os.environ.get("ARIO_GUARDRAILS") or "config/guardrails.yaml"
            ).resolve(),
        )
