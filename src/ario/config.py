"""Конфигурация из переменных окружения и `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    rx_path: Path | None
    llm_model: str
    openrouter_api_key: str | None
    guardrails_path: Path

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
            llm_model=os.environ.get("LLM_MODEL", "openrouter/qwen/qwen3.8-27b:free"),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            guardrails_path=Path(
                os.environ.get("ARIO_GUARDRAILS") or "config/guardrails.yaml"
            ).resolve(),
        )
