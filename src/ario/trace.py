"""Трассировка: JSON Lines на тикет, поля близки к OpenTelemetry (trace_id/span_id)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class TraceWriter:
    """Дописывает события в `traces/<id>/events.jsonl`; каждое событие сразу на диске."""

    def __init__(self, traces_dir: Path, ticket_id: str, trace_id: str) -> None:
        self._dir = traces_dir / ticket_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "events.jsonl"
        self._ticket_id = ticket_id
        self._trace_id = trace_id

    @property
    def path(self) -> Path:
        return self._path

    def event(
        self,
        kind: str,
        *,
        stage: str | None = None,
        agent: str | None = None,
        **data: Any,
    ) -> None:
        record = {
            "ts": utc_now(),
            "trace_id": self._trace_id,
            "span_id": uuid.uuid4().hex[:16],
            "ticket": self._ticket_id,
            "stage": stage,
            "agent": agent,
            "kind": kind,
            "data": data,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
