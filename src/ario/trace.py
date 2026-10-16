"""Трассировка: JSON Lines на тикет, поля близки к OpenTelemetry (trace_id/span_id).

Формат события (одна строка `traces/<id>/events.jsonl`):
`ts` (UTC, мс), `trace_id` (один на тикет, переживает возобновления), `span_id`, `ticket`,
`stage` (этап или null), `agent` (роль или null), `kind` (см. `EVENT_KINDS`), `data` (поля
события). Секреты маскируются при записи (`Masker`).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ario.guardrails.config import EnvConfig

# Реестр видов событий: имя → что означает. Тест сверяет его с кодом, поэтому новое событие
# нужно описать здесь.
EVENT_KINDS: dict[str, str] = {
    # поток тикета (оркестратор)
    "run_started": "прогон начат или возобновлён (resumed)",
    "run_reopened": "тикет возвращён из needs-human командой resume",
    "run_finished": "прогон завершён (outcome, reason, total_seconds)",
    "ticket_invalid": "файл тикета не разобран",
    "repo_selected": "для тикета выбран профиль репозитория (repo, source: правило или model)",
    "rework_requested": "запрошена доработка по замечаниям человека (round, chars)",
    "pr_replaced": "прежний PR не активен (закрыт или отменён): открывается новый (previous_pr)",
    "pr_comment_failed": "ответ в PR после доработки не записан; этап продолжен (reason)",
    "branch_switched": "перед анализом клон переключён на базовую ветку (from, to)",
    "published": "ветка отправлена и PR открыт (branch, committed, pr_id, pr_reused, pr_url)",
    "ticket_system": "запись на доску (action: state|comment, on, status, detail, to)",
    "observer_failed": "наблюдатель за прогоном (отчёт на доске) дал сбой; прогон продолжен",
    "ticket_not_taken": "тикет не взят в работу (проверка перед стартом)",
    "stage_started": "этап начат",
    "stage_finished": "этап завершён (seconds)",
    "stage_failed": "этап отказал (reason, seconds)",
    "stage_skipped": "этап уже был выполнен до возобновления",
    "stage_not_needed": "этап не нужен по условию (например, доработка после approve)",
    "guardrail_blocked": "сработала защита: injection, лимит шагов, контекста или времени",
    "stub": "заглушка этапа (тесты)",
    # агент (бэкенд OpenHands)
    "agent_started": "запуск агента: модель, инструкции, инструменты, навыки, задача",
    "agent_message": "сообщение модели или пользователя в диалоге",
    "agent_action": "вызов инструмента (arguments, thought, токены контекста)",
    "agent_observation": "результат инструмента",
    "agent_error": "ошибка вызова инструмента (искажённые аргументы и т.п.)",
    "agent_finished": "агент завершил работу (steps, суммарные токены)",
    "llm_retry": "повтор запроса к модели (rate limit, 5xx, таймаут)",
    "verdict_retry": "итоговый ответ не прошёл проверку, агенту дан один повтор",
    "budget_nudge": "агенту напомнили о бюджете шагов (сообщение в конец истории)",
    "error_nudge": "инструмент не удался 2 раза подряд: агенту дана подсказка (tool, streak)",
    "limit_wrap_up": "лимит шагов исчерпан: агенту дан короткий финальный запрос на итоговый ответ",
    # результаты этапов
    "routing_verdict": "вердикт классификатора репозитория (repo, confidence, reason)",
    "analysis_verdict": "вердикт analyst",
    "review_verdict": "вердикт reviewer",
    "branch_created": "создана ветка ai/<id>",
    "branch_reused": "продолжена существующая ветка ai/<id>",
    "build_output": "результат сборки (ok, errors, хвост вывода)",
    "development_report": "итог этапа developer",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# --- маскирование секретов ---------------------------------------------------------------------
MASK = "***"
MIN_SECRET_LENGTH = 6  # короткие значения («1», «true») не маскируем: испортят трассу

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), MASK),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1" + MASK),
    # `password=значение`: маскируется только значение, похожее на секрет (не короче 8 знаков
    # и с цифрой). Так обычный код вроде `var token = CancellationToken.None;` не портится.
    (
        re.compile(
            r"(?i)((?:api[_-]?key|token|password|passwd|secret)[\"']?\s*[=:]\s*[\"']?)"
            r"((?=[^\s\"',;]*\d)[^\s\"',;]{8,})"
        ),
        r"\1" + MASK,
    ),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + MASK + r"\2"),
]


class Masker:
    """Скрывает секреты в тексте: известные значения (ключ API, переменные-секреты окружения)
    и типичные формы (`sk-...`, `Bearer ...`, `password=...`, `https://user:pass@host`)."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}
        self._secrets = sorted(values, key=len, reverse=True)  # длинные первыми

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        env_config: EnvConfig,
        extra: Iterable[str | None] = (),
    ) -> Masker:
        name = re.compile(env_config.secret_name_pattern)
        values = [v for k, v in environ.items() if name.search(k)]
        values += [v for v in extra if v]
        return cls(values)

    def mask_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def mask(self, value: Any) -> Any:
        """Рекурсивно маскирует строки в словарях, списках и кортежах."""
        if isinstance(value, str):
            return self.mask_text(value)
        if isinstance(value, dict):
            return {k: self.mask(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.mask(v) for v in value]
        return value


class TraceWriter:
    """Дописывает события в `traces/<id>/events.jsonl`; каждое событие сразу на диске."""

    def __init__(
        self,
        traces_dir: Path,
        ticket_id: str,
        trace_id: str,
        masker: Masker | None = None,
    ) -> None:
        self._dir = traces_dir / ticket_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "events.jsonl"
        self._ticket_id = ticket_id
        self._trace_id = trace_id
        self._masker = masker
        self._finish_partial_line()

    def _finish_partial_line(self) -> None:
        """Убитый процесс мог оборвать последнюю строку: новое событие не должно к ней липнуть."""
        if not self._path.exists() or self._path.stat().st_size == 0:
            return
        with self._path.open("rb+") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                f.write(b"\n")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def ticket_id(self) -> str:
        return self._ticket_id

    def event(
        self,
        kind: str,
        *,
        stage: str | None = None,
        agent: str | None = None,
        **data: Any,
    ) -> None:
        record: dict[str, Any] = {
            "ts": utc_now(),
            "trace_id": self._trace_id,
            "span_id": uuid.uuid4().hex[:16],
            "ticket": self._ticket_id,
            "stage": stage,
            "agent": agent,
            "kind": kind,
            "data": data,
        }
        if self._masker is not None:
            record = self._masker.mask(record)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
