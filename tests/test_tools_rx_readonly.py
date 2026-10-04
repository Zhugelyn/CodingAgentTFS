"""Проверки на реальном RX (только чтение). Включаются переменной ARIO_TEST_RX."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ario.tools import GitRepo
from tests.test_guardrails import CONFIG

_RX = os.environ.get("ARIO_TEST_RX")

pytestmark = pytest.mark.skipif(not _RX, reason="ARIO_TEST_RX не задан")


def test_rx_git_read_only() -> None:
    repo = GitRepo(Path(_RX or "."), CONFIG.env)
    before = repo.changed_files()
    assert repo.current_branch()
    assert repo.branch_exists("master")
    assert "##" in repo.status()
    assert repo.log(3)
    repo.diff()  # не падает на реальном репозитории
    assert repo.changed_files() == before  # ничего не изменили
