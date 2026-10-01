from __future__ import annotations

from pathlib import Path

import pytest

from ario.config import Settings

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
