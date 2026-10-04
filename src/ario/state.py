"""Состояние прогона: `runs/<id>/state.json` (память прогона, возобновление)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ario.trace import utc_now

RunStatus = Literal["running", "done", "needs_human"]
StageStatus = Literal["pending", "running", "done", "failed", "skipped"]


class StageState(BaseModel):
    status: StageStatus = "pending"
    started_at: str | None = None
    finished_at: str | None = None


class RunState(BaseModel):
    ticket_id: str
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: RunStatus = "running"
    started_at: str = Field(default_factory=utc_now)  # справочно; лимит времени считается иначе
    active_seconds: float = 0.0  # накопленное время работы над тикетом без простоев между запусками
    reason: str | None = None
    stages: dict[str, StageState] = Field(default_factory=dict)

    def stage(self, name: str) -> StageState:
        return self.stages.setdefault(name, StageState())

    def start_stage(self, name: str) -> None:
        st = self.stage(name)
        st.status, st.started_at, st.finished_at = "running", utc_now(), None

    def finish_stage(self, name: str, *, ok: bool) -> None:
        st = self.stage(name)
        st.status, st.finished_at = ("done" if ok else "failed"), utc_now()


class StateStore:
    def __init__(self, runs_dir: Path) -> None:
        self._runs_dir = runs_dir

    def run_dir(self, ticket_id: str) -> Path:
        return self._runs_dir / ticket_id

    def path(self, ticket_id: str) -> Path:
        return self.run_dir(ticket_id) / "state.json"

    def load(self, ticket_id: str) -> RunState | None:
        path = self.path(ticket_id)
        if not path.exists():
            return None
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, state: RunState) -> None:
        path = self.path(state.ticket_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)  # атомарно: падение процесса не оставит битый файл
