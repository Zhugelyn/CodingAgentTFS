from __future__ import annotations

from pathlib import Path

import pytest

from ario.config import Settings


def test_role_models_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "m/default")
    monkeypatch.setenv("ANALYST_MODEL", "m/strong")
    monkeypatch.setenv("DEVELOPER_MODEL", "  ")
    monkeypatch.delenv("REVIEWER_MODEL", raising=False)
    settings = Settings.load(env)
    assert settings.llm_model == "m/default"
    assert settings.role_models == {"analyst": "m/strong"}  # пустые и незаданные не попадают
