from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ario.config import Settings
from tests.llm_stub import script  # noqa: F401  (фикстура доступна всем тестам)

REPO_ROOT = Path(__file__).resolve().parents[1]

TICKET_TEXT = """---
id: 43031
type: Bug
title: IntegrationAIAgent. Не валидируется ID
state: Closed
area: SmartInstruments\\Antares
priority: 2
severity: 3 - Medium
tags: [Applied]
---
## Description
ПОРЯДОК ДЕЙСТВИЙ: отправить запрос с некорректным id.

## Discussion
### Галиева Эльвира (Galieva_EI) · 2025-09-24
Решили оставить как есть.
### Бочкарев Дмитрий (Bochkarev_DS) · 2025-09-23
Согласен.
"""


_SETTING_NAMES = frozenset(
    {
        "RX_PATH",
        "BASE_BRANCH",
        "BRANCH_TEMPLATE",
        "LLM_MODEL",
        "ANALYST_MODEL",
        "DEVELOPER_MODEL",
        "REVIEWER_MODEL",
        "ROUTER_MODEL",
        "OPENROUTER_API_KEY",
        "GIT_PAT",
    }
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Настройки разработчика (его .env, переменные ARIO_* и TFS_*) не влияют на тесты.

    `load_dotenv` пишет прямо в `os.environ`, поэтому после теста окружение восстанавливается:
    иначе переменная из реального .env, загруженная одним тестом, протекла бы в следующие.
    """
    for name in list(os.environ):
        if name.startswith(("ARIO_", "TFS_")) or name in _SETTING_NAMES:
            monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        rx_path=None,
        llm_model="stub",
        openrouter_api_key=None,
        guardrails_path=REPO_ROOT / "config" / "guardrails.yaml",
    )


@pytest.fixture
def inbox_ticket(settings: Settings) -> str:
    folder = settings.tickets_dir / "inbox"
    folder.mkdir(parents=True)
    (folder / "43031.md").write_text(TICKET_TEXT, encoding="utf-8")
    return "43031"
