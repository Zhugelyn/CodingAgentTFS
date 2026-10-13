"""Экспорт трассы тикета в OpenTelemetry (OTLP): только метрики и тайминги, без текста.

Источник: `traces/<id>/events.jsonl` (решение D07). Трасса тикета превращается в одну
OTel-трассу: тикет → прогон → этап → агент → вызовы модели и инструментов. Идентификаторы
спанов детерминированы, поэтому повторный экспорт не плодит дубли.

В спаны попадают только числа, имена инструментов и моделей, статусы и время. Текст
промптов, мыслей модели, аргументов и результатов инструментов, код, тикеты и причины отказов
не экспортируются никогда.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event as SpanEvent
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

DEFAULT_PROJECT = "ArioCodingAgent"
SCOPE = InstrumentationScope("ario.trace_export", "1")
# Атрибуты `llm.model_name` и `gen_ai.request.model` не ставим: по ним Phoenix сам оценивает
# цену по прайсу модели без скидки за кэш и показывает сумму в десятки раз выше реальной.
# Реальная цена лежит в `ario.cost_usd`, модель в `ario.model`.
SAME_RESPONSE_NS = 50_000_000  # без usage вызовы одного ответа приходят с разницей в миллисекунды
# события, которые становятся событиями спана (в них только числа и тип)
_MARKERS = frozenset({"llm_retry", "budget_nudge", "limit_wrap_up", "verdict_retry"})

Event = dict[str, Any]
Attrs = dict[str, Any]


class ExportError(RuntimeError):
    """Не удалось отправить спаны в OTLP-приёмник."""


def _ns(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1_000_000) * 1000


def _trace_id(raw: str) -> int:
    try:
        if len(raw) == 32:
            value = int(raw, 16)
            if value:
                return value
    except ValueError:
        pass
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:16], "big") or 1


def _span_id(trace_raw: str, key: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{trace_raw}|{key}".encode()).digest()[:8], "big") or 1


_LABEL = re.compile(r"[^A-Za-z0-9_.\-]")
_MODEL = re.compile(r"[^A-Za-z0-9_.:/@+\-]")


def _label(value: Any, limit: int = 64) -> str:
    """Имена инструментов, ролей и этапов: только безопасные символы и ограниченная длина.

    Имя инструмента задаёт модель, поэтому через него нельзя выпускать произвольный текст.
    """
    return _LABEL.sub("_", str(value))[:limit] or "?"


def _model(value: Any) -> str:
    return _MODEL.sub("_", str(value))[:120]


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _valid(events: Sequence[Event]) -> list[Event]:
    """Только разбираемые события: битая запись не должна ронять экспорт всей трассы."""
    result: list[Event] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            _ns(str(event["ts"]))
            str(event["kind"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        result.append(event)
    return result


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --- промежуточная модель ---------------------------------------------------------------------
@dataclass
class _Call:
    tool: str
    start: int
    data: Event
    end: int | None = None
    error: bool = False


@dataclass
class _Agent:
    role: str
    start: int
    model: str = ""
    max_steps: int = 0
    end: int | None = None
    finished: bool = False
    steps: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    cost: float = 0.0
    calls: list[_Call] = field(default_factory=list)
    marks: list[tuple[int, str, Attrs]] = field(default_factory=list)


@dataclass
class _Stage:
    name: str
    start: int
    end: int | None = None
    status: str = "interrupted"
    attrs: Attrs = field(default_factory=dict)
    agents: list[_Agent] = field(default_factory=list)


@dataclass
class _Run:
    start: int
    resumed: bool = False
    end: int | None = None
    outcome: str = "interrupted"
    stages: list[_Stage] = field(default_factory=list)
    marks: list[tuple[int, str, Attrs]] = field(default_factory=list)


def _parse(events: Sequence[Event]) -> list[_Run]:
    runs: list[_Run] = []
    run: _Run | None = None
    stage: _Stage | None = None
    agent: _Agent | None = None
    pending: dict[str, deque[_Call]] = defaultdict(deque)
    last = 0
    carry: list[tuple[int, str, Attrs]] = []  # отметки до начала прогона

    def close_agent(at: int) -> None:
        nonlocal agent
        if agent is not None and agent.end is None:
            agent.end = at
        agent = None
        pending.clear()

    def close_stage(at: int) -> None:
        nonlocal stage
        close_agent(at)
        if stage is not None and stage.end is None:
            stage.end = at
        stage = None

    def close_run(at: int) -> None:
        nonlocal run
        close_stage(at)
        if run is not None and run.end is None:
            run.end = at
        run = None

    def new_run(at: int, *, resumed: bool = False) -> _Run:
        nonlocal run
        run = _Run(start=at, resumed=resumed, marks=list(carry))
        carry.clear()
        runs.append(run)
        return run

    def ensure_run(at: int) -> _Run:
        return run if run is not None else new_run(at)

    for event in events:
        kind = str(event.get("kind"))
        at = _ns(str(event["ts"]))
        last = at
        data: Event = event.get("data") or {}
        name = _label(event.get("stage") or "")
        if kind == "run_started":
            close_run(at)
            new_run(at, resumed=bool(data.get("resumed")))
        elif kind == "run_finished":
            current = ensure_run(at)
            close_stage(at)
            current.end = at
            current.outcome = _label(data.get("outcome") or "unknown")
            run = None
        elif kind == "stage_started":
            current = ensure_run(at)
            close_stage(at)
            stage = _Stage(name=name or "stage", start=at)
            current.stages.append(stage)
        elif kind in ("stage_finished", "stage_failed"):
            if stage is not None:
                close_agent(at)
                stage.end = at
                stage.status = "done" if kind == "stage_finished" else "failed"
                stage.attrs["ario.seconds"] = _float(data.get("seconds"))
                stage = None
        elif kind in ("stage_skipped", "stage_not_needed"):
            current = ensure_run(at)
            current.stages.append(
                _Stage(
                    name=name or "stage",
                    start=at,
                    end=at,
                    status="skipped" if kind == "stage_skipped" else "not_needed",
                )
            )
        elif kind == "agent_started":
            if stage is None:
                stage = _Stage(name=name or "stage", start=at)
                ensure_run(at).stages.append(stage)
            close_agent(at)
            agent = _Agent(
                role=_label(event.get("agent") or "agent"),
                start=at,
                model=_model(data.get("model") or ""),
                max_steps=_int(data.get("max_steps")),
            )
            stage.agents.append(agent)
        elif kind == "agent_finished" and agent is not None:
            agent.steps = _int(data.get("steps"))
            agent.prompt = _int(data.get("prompt_tokens"))
            agent.completion = _int(data.get("completion_tokens"))
            agent.cached = _int(data.get("cache_read_tokens"))
            agent.cost = _float(data.get("cost_usd"))
            agent.finished = True
            close_agent(at)
        elif kind == "agent_action" and agent is not None:
            call = _Call(tool=_label(data.get("tool") or "?"), start=at, data=data)
            agent.calls.append(call)
            pending[call.tool].append(call)
        elif kind in ("agent_observation", "agent_error") and agent is not None:
            queue = pending.get(_label(data.get("tool") or "?"))
            if queue:
                call = queue.popleft()
                call.end = at
                call.error = call.error or kind == "agent_error"
        elif kind in _MARKERS:
            counts: Attrs = {k: _int(v) for k, v in data.items() if k != "error"}
            target = agent.marks if agent is not None else ensure_run(at).marks
            target.append((at, kind, counts))
        elif kind == "guardrail_blocked":
            guard: Attrs = {"guard": _label(data.get("guard") or "limit")}
            (agent.marks if agent is not None else ensure_run(at).marks).append((at, kind, guard))
        elif stage is not None:
            _stage_result(stage, kind, data)
        elif kind in ("ticket_invalid", "ticket_not_taken", "run_reopened"):
            (run.marks if run is not None else carry).append((at, kind, {}))
    if carry:
        ensure_run(last)
    close_run(last)
    return runs


def _stage_result(stage: _Stage, kind: str, data: Event) -> None:
    """Итоги этапов: только числа и короткие признаки, без текста."""
    if kind == "analysis_verdict":
        stage.attrs["ario.analysis.decision"] = _label(data.get("decision") or "")
        stage.attrs["ario.analysis.risk"] = _label(data.get("risk") or "")
    elif kind == "review_verdict":
        stage.attrs["ario.review.verdict"] = _label(data.get("verdict") or "")
        stage.attrs["ario.review.issues"] = _int(data.get("issues"))
    elif kind == "build_output":
        stage.attrs["ario.build.ok"] = bool(data.get("ok"))
        stage.attrs["ario.build.errors"] = _int(data.get("errors"))
    elif kind == "development_report":
        files = data.get("files_edited")
        stage.attrs["ario.files_edited"] = len(files) if isinstance(files, list) else 0
        stage.attrs["ario.build.ok"] = bool(data.get("build_ok"))
    elif kind in ("branch_created", "branch_reused"):
        stage.attrs["ario.branch.reused"] = kind == "branch_reused"


# --- сборка спанов ----------------------------------------------------------------------------
class _Builder:
    def __init__(self, trace_raw: str, project: str, ticket: str) -> None:
        self._raw = trace_raw
        self._trace = _trace_id(trace_raw)
        self._resource = Resource.create(
            {
                "service.name": "ario-coding-agent",
                "openinference.project.name": project,
                "ario.ticket.id": ticket,
            }
        )
        self.spans: list[ReadableSpan] = []

    def context(self, key: str) -> SpanContext:
        return SpanContext(
            self._trace, _span_id(self._raw, key), False, TraceFlags(TraceFlags.SAMPLED)
        )

    def add(
        self,
        name: str,
        key: str,
        parent: str | None,
        start: int,
        end: int,
        attrs: Attrs,
        *,
        kind: str,
        error: bool = False,
        marks: Sequence[tuple[int, str, Attrs]] = (),
    ) -> None:
        attributes = {"openinference.span.kind": kind, **attrs}
        self.spans.append(
            ReadableSpan(
                name=name,
                context=self.context(key),
                parent=self.context(parent) if parent else None,
                resource=self._resource,
                attributes=attributes,
                events=[SpanEvent(label, a, timestamp=t) for t, label, a in marks],
                kind=SpanKind.INTERNAL,
                status=Status(StatusCode.ERROR) if error else Status(StatusCode.OK),
                start_time=start,
                end_time=max(end, start),
                instrumentation_scope=SCOPE,
            )
        )


def _agent_spans(b: _Builder, agent: _Agent, stage_key: str, index: int, end: int) -> None:
    key = f"{stage_key}/agent/{agent.role}#{index}"
    agent_end = agent.end if agent.end is not None else end
    cache_ratio = round(agent.cached / agent.prompt, 3) if agent.prompt else 0.0
    b.add(
        f"agent {agent.role}",
        key,
        stage_key,
        agent.start,
        agent_end,
        {
            "gen_ai.agent.name": agent.role,
            "ario.model": agent.model,
            "ario.steps": agent.steps or len(agent.calls),
            "ario.max_steps": agent.max_steps,
            "ario.usage.input_tokens": agent.prompt,
            "ario.usage.output_tokens": agent.completion,
            "ario.usage.cached_input_tokens": agent.cached,
            "ario.usage.cache_hit_ratio": cache_ratio,
            "ario.usage.cost_usd": agent.cost,
        },
        kind="AGENT",
        error=not agent.finished,
        marks=agent.marks,
    )
    # вызовы модели: действия с одинаковым накопленным prompt_total пришли в одном ответе
    groups: list[list[_Call]] = []
    for call in agent.calls:
        total = _int(call.data.get("prompt_total"))
        previous = groups[-1][-1] if groups else None
        same = (
            previous is not None
            and _int(groups[-1][0].data.get("prompt_total")) == total
            and (total > 0 or call.start - previous.start <= SAME_RESPONSE_NS)
        )
        if same:
            groups[-1].append(call)
        else:
            groups.append([call])
    prev_prompt = prev_completion = 0
    prev_cost = 0.0
    boundary = agent.start
    for number, group in enumerate(groups, 1):
        first = group[0]
        prompt_total = _int(first.data.get("prompt_total"))
        completion_total = _int(first.data.get("completion_total"))
        prompt = max(prompt_total - prev_prompt, 0)
        completion = max(completion_total - prev_completion, 0)
        cost_total = _float(first.data.get("cost_total"))
        cost = max(round(cost_total - prev_cost, 6), 0.0)
        b.add(
            f"llm call {number}",
            f"{key}/llm/{number}",
            key,
            boundary,
            first.start,
            {
                "gen_ai.usage.input_tokens": prompt,
                "gen_ai.usage.output_tokens": completion,
                "ario.model": agent.model,
                "llm.token_count.prompt": prompt,
                "llm.token_count.completion": completion,
                "llm.token_count.total": prompt + completion,
                "ario.context_tokens": _int(first.data.get("context_tokens")),
                "ario.step": number,
                "ario.tool_calls": len(group),
                "llm.cost.total": cost,
                "ario.cost_usd": cost,
            },
            kind="LLM",
        )
        prev_prompt, prev_completion, prev_cost = prompt_total, completion_total, cost_total
        ends = [c.end for c in group if c.end is not None]
        boundary = max(ends) if ends else first.start
    for number, call in enumerate(agent.calls, 1):
        b.add(
            f"tool {call.tool}",
            f"{key}/tool/{number}",
            key,
            call.start,
            call.end if call.end is not None else call.start,
            {"tool.name": call.tool, "ario.tool.error": call.error},
            kind="TOOL",
            error=call.error,
        )


def build_spans(events: Sequence[Event], project: str = DEFAULT_PROJECT) -> list[ReadableSpan]:
    """Превращает события трассы тикета в спаны OTel (без текста).

    Тикет, перезапущенный с нуля, получает новый `trace_id`, а файл событий общий: каждая
    трасса строится отдельно, иначе идентификаторы спанов разных запусков совпали бы.
    """
    by_trace: dict[str, list[Event]] = {}
    for event in _valid(events):
        key = str(event.get("trace_id") or event.get("ticket") or "?")
        by_trace.setdefault(key, []).append(event)
    spans: list[ReadableSpan] = []
    for trace_raw, group in by_trace.items():
        spans += _build_trace(group, trace_raw, project)
    return spans


def _build_trace(events: Sequence[Event], trace_raw: str, project: str) -> list[ReadableSpan]:
    first = events[0]
    ticket = _label(first.get("ticket") or "?")
    runs = _parse(events)
    b = _Builder(trace_raw, project, ticket)
    start, end = _ns(str(first["ts"])), _ns(str(events[-1]["ts"]))
    outcome = next((r.outcome for r in reversed(runs) if r.outcome != "interrupted"), "interrupted")
    b.add(
        f"ticket {ticket}",
        "ticket",
        None,
        start,
        end,
        {
            "ario.ticket.id": ticket,
            "ario.runs": len(runs),
            "ario.outcome": outcome,
            "ario.resumed": any(r.resumed for r in runs),
        },
        kind="CHAIN",
        error=outcome != "done",
    )
    for run_no, run in enumerate(runs, 1):
        run_key = f"run{run_no}"
        run_end = run.end if run.end is not None else end
        b.add(
            f"run {run_no}",
            run_key,
            "ticket",
            run.start,
            run_end,
            {"ario.outcome": run.outcome, "ario.resumed": run.resumed},
            kind="CHAIN",
            error=run.outcome != "done",
            marks=run.marks,
        )
        for stage in run.stages:
            stage_key = f"{run_key}/stage/{stage.name}"
            stage_end = stage.end if stage.end is not None else run_end
            b.add(
                f"stage {stage.name}",
                stage_key,
                run_key,
                stage.start,
                stage_end,
                {"ario.stage": stage.name, "ario.status": stage.status, **stage.attrs},
                kind="CHAIN",
                error=stage.status in ("failed", "interrupted"),
            )
            for index, agent in enumerate(stage.agents, 1):
                _agent_spans(b, agent, stage_key, index, stage_end)
    return b.spans


def export_spans(
    spans: Sequence[ReadableSpan],
    endpoint: str,
    exporter: SpanExporter | None = None,
) -> int:
    """Отправляет спаны по OTLP/HTTP; возвращает их число. Бросает `ExportError`."""
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=endpoint, timeout=15)
    try:
        result = exporter.export(list(spans))
    except Exception as e:  # сеть, TLS, сериализация
        raise ExportError(f"Не удалось отправить спаны ({type(e).__name__})") from None
    finally:
        exporter.shutdown()
    if result != SpanExportResult.SUCCESS:
        raise ExportError(f"Приёмник {endpoint} не принял спаны")
    return len(spans)
