"""Разбор трассы тикета: краткая сводка (`ario trace <id>`) и пошаговая лента (`--steps`)."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Event = dict[str, Any]

_STATUS_TITLES = {
    "done": "готов",
    "failed": "отказ",
    "skipped": "пропущен (уже был выполнен)",
    "not_needed": "не нужен",
    "interrupted": "прерван (процесс остановлен до конца этапа)",
}


def load_events(path: Path) -> tuple[list[Event], int]:
    """Читает `events.jsonl`; битые строки (обрыв записи убитым процессом) пропускает.

    Возвращает (события, число пропущенных строк).
    """
    events: list[Event] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict) and "kind" in record:
            events.append(record)
        else:
            skipped += 1
    return events, skipped


@dataclass
class StageSummary:
    name: str
    status: str = "interrupted"
    seconds: float | None = None
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0  # цена этапа по данным провайдера
    cache_read_tokens: int = 0  # токены запросов, прочитанные провайдером из кэша
    max_context: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)  # ошибки, повторы, срабатывания защит, итоги


@dataclass
class TraceSummary:
    ticket: str
    runs: int
    resumed: int
    outcome: str | None
    reason: str | None
    total_seconds: float | None
    started: str | None
    finished: str | None
    stages: list[StageSummary]
    skipped_lines: int = 0


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # ESC и прочие управляющие символы


def _short(text: object, limit: int = 140) -> str:
    # тексты файлов RX и ответов модели недоверенные: ANSI/OSC-последовательности не должны
    # управлять терминалом оператора
    one_line = " ".join(_CONTROL.sub("", str(text)).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def summarize(events: list[Event], skipped_lines: int = 0) -> TraceSummary:
    stages: dict[str, StageSummary] = {}
    runs = resumed = 0
    outcome: str | None = None
    reason: str | None = None
    total: float | None = None

    def stage_of(event: Event) -> StageSummary | None:
        name = event.get("stage")
        if not name:
            return None
        return stages.setdefault(name, StageSummary(name))

    for e in events:
        kind, data = e["kind"], e.get("data") or {}
        st = stage_of(e)
        if kind == "run_started":
            runs += 1
            resumed += bool(data.get("resumed"))
        elif kind == "run_finished":
            outcome, reason, total = (
                data.get("outcome"),
                data.get("reason"),
                data.get("total_seconds"),
            )
        elif st is None:
            continue
        elif kind == "stage_started":
            st.status = "interrupted"
        elif kind == "stage_finished":
            st.status, st.seconds = "done", data.get("seconds")
        elif kind == "stage_failed":
            st.status, st.seconds = "failed", data.get("seconds")
            st.notes.append(f"причина отказа: {_short(data.get('reason'))}")
        elif kind == "stage_skipped":
            st.status = "skipped"
        elif kind == "stage_not_needed":
            st.status = "not_needed"
        elif kind == "agent_action":
            st.tools[data.get("tool", "?")] += 1
            st.max_context = max(st.max_context, int(data.get("context_tokens") or 0))
        elif kind == "agent_finished":
            st.steps += int(data.get("steps") or 0)
            st.prompt_tokens += int(data.get("prompt_tokens") or 0)
            st.completion_tokens += int(data.get("completion_tokens") or 0)
            st.cache_read_tokens += int(data.get("cache_read_tokens") or 0)
            st.cost_usd += float(data.get("cost_usd") or 0)
        elif kind == "agent_error":
            st.notes.append(
                f"ошибка инструмента {data.get('tool')}: {_short(data.get('error'), 110)}"
            )
        elif kind == "llm_retry":
            st.notes.append(
                f"повтор запроса к модели {data.get('attempt')}/{data.get('max_attempts')}: "
                f"{_short(data.get('error'), 90)}"
            )
        elif kind == "verdict_retry":
            st.notes.append(f"вердикт не прошёл проверку: {_short(data.get('error'), 110)}")
        elif kind == "guardrail_blocked":
            st.notes.append(f"сработала защита: {_short(data.get('reason'))}")
        elif kind == "build_output":
            ok = "успешна" if data.get("ok") else f"не удалась, ошибок: {data.get('errors')}"
            st.notes.append(f"сборка {ok}")
        elif kind == "analysis_verdict":
            st.notes.append(f"вердикт analyst: {data.get('decision')}, риск {data.get('risk')}")
        elif kind == "review_verdict":
            st.notes.append(
                f"вердикт reviewer: {data.get('verdict')}, замечаний {data.get('issues')}"
            )
        elif kind in ("branch_created", "branch_reused"):
            how = "создана" if kind == "branch_created" else "продолжена"
            st.notes.append(f"ветка {data.get('branch')} ({how})")
        elif kind == "development_report" and data.get("problems"):
            st.notes.append(f"проблемы: {_short('; '.join(data['problems']))}")

    return TraceSummary(
        ticket=str(events[0].get("ticket")) if events else "?",
        runs=runs,
        resumed=resumed,
        outcome=outcome,
        reason=reason,
        total_seconds=total,
        started=events[0]["ts"] if events else None,
        finished=events[-1]["ts"] if events else None,
        stages=list(stages.values()),
        skipped_lines=skipped_lines,
    )


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    minutes, seconds = divmod(int(round(value)), 60)
    return f"{minutes} мин {seconds:02d} с" if minutes else f"{seconds} с"


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _percent(part: int, total: int) -> str:
    return f"{part * 100 // total}%" if total else "0%"


def _fmt_tokens(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def render_summary(s: TraceSummary) -> str:
    lines = [f"Тикет {s.ticket}"]
    if s.outcome:
        title = "готов (done)" if s.outcome == "done" else "нужен человек (needs-human)"
        lines.append(f"Итог: {title}; рабочее время {_fmt_seconds(s.total_seconds)}")
    else:
        lines.append("Итог: прогон не завершён (процесс прерван или ещё идёт)")
    if s.reason:
        lines.append(f"Причина: {_short(s.reason, 300)}")
    lines.append(
        f"Запусков: {s.runs}" + (f", из них возобновлений: {s.resumed}" if s.resumed else "")
    )
    total_cost = sum(st.cost_usd for st in s.stages)
    if total_cost:
        lines.append(f"Цена по данным провайдера (все попытки): {_fmt_usd(total_cost)}")
    if s.skipped_lines:
        lines.append(f"Пропущено повреждённых строк трассы: {s.skipped_lines}")
    lines.append("")
    for st in s.stages:
        head = f"> {st.name}: {_STATUS_TITLES.get(st.status, st.status)}"
        if st.seconds is not None:
            head += f", {_fmt_seconds(st.seconds)}"
        lines.append(head)
        if st.steps:
            lines.append(
                f"    шагов {st.steps}, токены: запросы {_fmt_tokens(st.prompt_tokens)} + "
                f"ответы {_fmt_tokens(st.completion_tokens)}, максимум контекста "
                f"{_fmt_tokens(st.max_context)}"
            )
            lines.append(
                f"    из кэша провайдера: {_fmt_tokens(st.cache_read_tokens)} токенов запросов"
                f" ({_percent(st.cache_read_tokens, st.prompt_tokens)}), "
                f"цена {_fmt_usd(st.cost_usd)}"
            )
        if st.tools:
            tools = ", ".join(f"{name} ({n})" for name, n in st.tools.most_common())
            lines.append(f"    инструменты: {tools}")
        lines += [f"    - {note}" for note in st.notes]
    return "\n".join(lines) + "\n"


def render_steps(events: list[Event], stage: str | None = None, *, full: bool = False) -> str:
    """Лента шагов агента: что вызвал, что получил, сколько контекста, сколько ждал."""
    lines: list[str] = []
    step = 0
    for e in events:
        if stage and e.get("stage") != stage:
            continue
        kind, data = e["kind"], e.get("data") or {}
        clock = str(e["ts"])[11:19]
        where = f"{e.get('stage') or '-'}"
        limit = 600 if full else 120
        if kind == "agent_started":
            step = 0
            lines.append(
                f"{clock} [{where}] агент {e.get('agent')} запущен, модель {data.get('model')}"
            )
            if full:
                lines.append(f"    инструменты: {', '.join(data.get('tools') or [])}")
                lines.append(f"    навыки: {', '.join(data.get('skills') or []) or 'нет'}")
                lines.append(f"    задача: {_short(data.get('task'), 1500)}")
        elif kind == "agent_action":
            step += 1
            ctx = data.get("context_tokens")
            tail = f" | контекст {_fmt_tokens(int(ctx))}" if ctx else ""
            arguments = _short(data.get("arguments"), limit)
            lines.append(f"{clock} [{where}] #{step} {data.get('tool')} {arguments}{tail}")
            if full and data.get("thought"):
                lines.append(f"    мысль: {_short(data['thought'], 800)}")
        elif kind == "agent_observation":
            lines.append(f"    -> {_short(data.get('text'), limit)}")
        elif kind == "agent_error":
            lines.append(f"    ! ошибка инструмента: {_short(data.get('error'), limit)}")
        elif kind == "llm_retry":
            lines.append(
                f"{clock} [{where}] ожидание модели, повтор {data.get('attempt')}/"
                f"{data.get('max_attempts')}: {_short(data.get('error'), 90)}"
            )
        elif kind == "verdict_retry":
            lines.append(f"{clock} [{where}] повтор: {_short(data.get('error'), limit)}")
        elif kind == "guardrail_blocked":
            lines.append(f"{clock} [{where}] ЗАЩИТА: {_short(data.get('reason'), 200)}")
        elif kind == "build_output":
            lines.append(
                f"{clock} [{where}] сборка ok={data.get('ok')} ошибок={data.get('errors')}"
            )
        elif kind == "agent_finished":
            lines.append(
                f"{clock} [{where}] агент завершил: шагов {data.get('steps')}, "
                f"токены {_fmt_tokens(int(data.get('prompt_tokens') or 0))}+"
                f"{_fmt_tokens(int(data.get('completion_tokens') or 0))}"
            )
        elif kind in ("stage_started", "stage_finished", "stage_failed"):
            lines.append(f"{clock} [{where}] {kind}")
    return "\n".join(lines) + "\n" if lines else "Нет событий для показа.\n"
