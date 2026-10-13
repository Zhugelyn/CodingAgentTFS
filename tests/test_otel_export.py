"""Экспорт трассы в OpenTelemetry: структура спанов, токены по вызовам, отсутствие текста."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ario.config import ConfigError, Settings, check_sdk_telemetry
from ario.otel_export import ExportError, build_spans, export_spans
from tests.conftest import REPO_ROOT

TRACE = "ae6b0837dd084720a962c29767df41d2"
SECRET = "СЕКРЕТНЫЙ-ТЕКСТ-КОД-RX"


def ev(kind: str, t: float, stage: str | None = None, agent: str | None = None, **data: Any) -> Any:
    return {
        "ts": f"2026-10-08T10:00:{t:06.3f}+00:00",
        "trace_id": TRACE,
        "span_id": f"s{t}",
        "ticket": "43702",
        "stage": stage,
        "agent": agent,
        "kind": kind,
        "data": data,
    }


def action(t: float, tool: str, prompt_total: int, completion_total: int, ctx: int) -> Any:
    return ev(
        "agent_action",
        t,
        "analysis",
        "analyst",
        tool=tool,
        arguments=SECRET,
        thought=SECRET,
        reasoning=SECRET,
        context_tokens=ctx,
        prompt_total=prompt_total,
        completion_total=completion_total,
    )


def obs(t: float, tool: str) -> Any:
    return ev("agent_observation", t, "analysis", "analyst", tool=tool, text=SECRET)


def events() -> list[Any]:
    return [
        ev("run_started", 0.0, resumed=False),
        ev("stage_started", 1.0, "analysis"),
        ev(
            "agent_started",
            1.1,
            "analysis",
            "analyst",
            model="openrouter/m",
            max_steps=10,
            task=SECRET,
            instructions=SECRET,
            tools=["read_file"],
        ),
        action(3.0, "invoke_skill", 1000, 50, 1000),  # два вызова в одном ответе модели
        action(3.001, "read_file", 1000, 50, 1000),
        obs(3.5, "invoke_skill"),
        obs(4.0, "read_file"),
        ev("budget_nudge", 4.1, "analysis", "analyst", steps=5, left=5),
        action(7.0, "search_text", 3500, 120, 2500),
        obs(8.0, "search_text"),
        ev(
            "agent_finished",
            9.0,
            "analysis",
            "analyst",
            steps=3,
            prompt_tokens=3500,
            completion_tokens=120,
            cache_read_tokens=2800,
        ),
        ev(
            "analysis_verdict",
            9.1,
            "analysis",
            "analyst",
            decision="suitable",
            risk="low",
            steps=3,
            prompt_tokens=3500,
            completion_tokens=120,
            reason=SECRET,
        ),
        ev("stage_finished", 9.2, "analysis", seconds=8.2),
        ev("stage_started", 10.0, "development"),
        ev("build_output", 11.0, "development", "developer", ok=True, errors=0, text=SECRET),
        ev("stage_failed", 12.0, "development", reason=SECRET, seconds=2.0),
        ev("run_finished", 12.5, outcome="needs-human", reason=SECRET, total_seconds=12.5),
    ]


def by_name(spans: Sequence[ReadableSpan]) -> dict[str, ReadableSpan]:
    return {s.name: s for s in spans}


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def secs(span: ReadableSpan) -> float:
    assert span.start_time is not None and span.end_time is not None
    return float(span.end_time - span.start_time) / 1e9


def parent_name(spans: Sequence[ReadableSpan], span: ReadableSpan) -> str | None:
    if span.parent is None:
        return None
    return next(s.name for s in spans if s.context.span_id == span.parent.span_id)


def test_hierarchy_and_attributes() -> None:
    spans = build_spans(events())
    named = by_name(spans)
    assert parent_name(spans, named["ticket 43702"]) is None
    assert parent_name(spans, named["run 1"]) == "ticket 43702"
    assert parent_name(spans, named["stage analysis"]) == "run 1"
    assert parent_name(spans, named["agent analyst"]) == "stage analysis"
    assert parent_name(spans, named["llm call 1"]) == "agent analyst"
    assert parent_name(spans, named["tool read_file"]) == "agent analyst"
    assert {s.context.trace_id for s in spans} == {int(TRACE, 16)}
    agent = named["agent analyst"].attributes or {}
    assert agent["ario.model"] == "openrouter/m"
    assert agent["ario.usage.input_tokens"] == 3500
    assert agent["ario.usage.cached_input_tokens"] == 2800
    assert agent["ario.usage.cache_hit_ratio"] == 0.8
    stage = named["stage analysis"].attributes or {}
    assert (stage["ario.status"], stage["ario.analysis.decision"]) == ("done", "suitable")
    failed = named["stage development"]
    assert (failed.attributes or {})["ario.status"] == "failed"
    assert (failed.attributes or {})["ario.build.ok"] is True
    assert not failed.status.is_ok
    root = named["ticket 43702"].attributes or {}
    assert root["ario.outcome"] == "needs-human" and root["ario.runs"] == 1


def test_tokens_are_split_per_llm_call_and_parallel_calls_are_merged() -> None:
    spans = [s for s in build_spans(events()) if s.name.startswith("llm call")]
    assert [s.name for s in spans] == ["llm call 1", "llm call 2"]
    first, second = (attrs(s) for s in spans)
    assert (first["llm.token_count.prompt"], first["llm.token_count.completion"]) == (1000, 50)
    assert first["ario.tool_calls"] == 2 and first["ario.context_tokens"] == 1000
    assert (second["llm.token_count.prompt"], second["gen_ai.usage.output_tokens"]) == (2500, 70)
    # сумма по вызовам равна итогу агента
    assert first["llm.token_count.prompt"] + second["llm.token_count.prompt"] == 3500


def test_llm_span_covers_the_wait_for_the_model() -> None:
    spans = by_name(build_spans(events()))
    first, second = spans["llm call 1"], spans["llm call 2"]
    assert secs(first) == pytest.approx(1.9, abs=0.01)  # 1.1 → 3.0
    assert secs(second) == pytest.approx(3.0, abs=0.01)  # 4.0 → 7.0


def test_tool_spans_pair_actions_with_observations() -> None:
    tools = [s for s in build_spans(events()) if s.name.startswith("tool ")]
    durations = {s.name: secs(s) for s in tools}
    assert durations["tool invoke_skill"] == pytest.approx(0.5, abs=0.01)
    assert durations["tool read_file"] == pytest.approx(1.0, abs=0.01)
    assert durations["tool search_text"] == pytest.approx(1.0, abs=0.01)


def test_no_text_is_exported() -> None:
    dump = ""
    for span in build_spans(events()):
        dump += json.dumps(dict(span.attributes or {}), ensure_ascii=False, default=str)
        dump += span.name
        for event in span.events:
            dump += event.name + json.dumps(dict(event.attributes or {}), default=str)
    assert SECRET not in dump
    assert "openrouter/m" in dump  # а числа и имена моделей на месте


def test_markers_become_span_events_with_numbers_only() -> None:
    agent = by_name(build_spans(events()))["agent analyst"]
    (nudge,) = agent.events
    assert nudge.name == "budget_nudge" and dict(nudge.attributes or {}) == {"steps": 5, "left": 5}


def test_span_ids_are_deterministic() -> None:
    first = [(s.context.trace_id, s.context.span_id) for s in build_spans(events())]
    second = [(s.context.trace_id, s.context.span_id) for s in build_spans(events())]
    assert first == second and len({i for _, i in first}) == len(first)


def test_interrupted_run_has_valid_error_spans() -> None:
    cut = [
        e for e in events() if e["kind"] not in ("agent_finished", "stage_finished", "run_finished")
    ]
    spans = build_spans(cut[:9])  # процесс убит посреди этапа analysis
    named = by_name(spans)
    assert all(secs(s) >= 0 for s in spans)
    assert not named["agent analyst"].status.is_ok
    assert (named["stage analysis"].attributes or {})["ario.status"] == "interrupted"
    assert (named["run 1"].attributes or {})["ario.outcome"] == "interrupted"


def test_resumed_runs_share_trace_and_get_separate_run_spans() -> None:
    more = events() + [
        ev("run_reopened", 20.0),
        ev("run_started", 20.1, resumed=True),
        ev("stage_skipped", 20.2, "analysis"),
        ev("run_finished", 21.0, outcome="done", total_seconds=1.0),
    ]
    named = by_name(build_spans(more))
    assert {"run 1", "run 2"} <= set(named)
    assert (named["run 2"].attributes or {})["ario.resumed"] is True
    assert (named["ticket 43702"].attributes or {})["ario.outcome"] == "done"
    assert (named["ticket 43702"].attributes or {})["ario.runs"] == 2


def test_empty_trace_gives_no_spans() -> None:
    assert build_spans([]) == []


# --- отправка -------------------------------------------------------------------------------
class FakeExporter(SpanExporter):
    def __init__(self, result: SpanExportResult = SpanExportResult.SUCCESS, boom: bool = False):
        self.result, self.boom, self.closed = result, boom, False
        self.sent: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.boom:
            raise ConnectionError("http://secret-host:4318 refused")
        self.sent += list(spans)
        return self.result

    def shutdown(self) -> None:
        self.closed = True


def test_export_sends_all_spans_and_closes() -> None:
    exporter = FakeExporter()
    spans = build_spans(events())
    assert export_spans(spans, "http://x/v1/traces", exporter) == len(spans)
    assert len(exporter.sent) == len(spans) and exporter.closed


def test_export_failures_become_export_error_without_details() -> None:
    with pytest.raises(ExportError) as info:
        export_spans(build_spans(events()), "http://x", FakeExporter(boom=True))
    assert "secret-host" not in str(info.value)
    with pytest.raises(ExportError, match="не принял"):
        export_spans(build_spans(events()), "http://x", FakeExporter(SpanExportResult.FAILURE))


# --- защита от утечки через SDK ---------------------------------------------------------------
@pytest.mark.parametrize(
    "var",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "LMNR_PROJECT_API_KEY",
        "LMNR_BASE_URL",
    ],
)
def test_sdk_telemetry_vars_are_refused(var: str) -> None:
    with pytest.raises(ConfigError, match=var):
        check_sdk_telemetry({var: "http://collector"})


def test_sdk_telemetry_guard_allows_safe_cases() -> None:
    check_sdk_telemetry({})
    check_sdk_telemetry({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "})  # пустое значение
    check_sdk_telemetry({"ARIO_OTLP_ENDPOINT": "http://localhost:6006/v1/traces"})
    check_sdk_telemetry(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c", "ARIO_ALLOW_SDK_TELEMETRY": "1"}
    )


def test_settings_reads_otlp_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_OTLP_ENDPOINT", " http://localhost:6006/v1/traces ")
    assert Settings.load(empty).otlp_endpoint == "http://localhost:6006/v1/traces"


# --- команда ----------------------------------------------------------------------------------
@pytest.fixture
def cli_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    folder = tmp_path / "data" / "traces" / "43702"
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events()) + "\n", encoding="utf-8"
    )
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARIO_GUARDRAILS", str(REPO_ROOT / "config" / "guardrails.yaml"))
    monkeypatch.delenv("ARIO_OTLP_ENDPOINT", raising=False)
    return ["--env-file", str(empty), "export-otel", "43702"]


def test_cli_dry_run_shows_composition(
    cli_trace: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    import ario.cli as cli

    assert cli.main([*cli_trace, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "LLM: 2" in out and "TOOL: 3" in out and "AGENT: 1" in out and "не экспортируется" in out


def test_cli_export_needs_endpoint_and_reports_result(
    cli_trace: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ario.cli as cli
    import ario.otel_export as module

    assert cli.main(cli_trace) == 2
    assert "ARIO_OTLP_ENDPOINT" in capsys.readouterr().err
    sent: list[str] = []

    def fake_export(spans: Sequence[ReadableSpan], endpoint: str) -> int:
        sent.append(endpoint)
        return len(spans)

    monkeypatch.setattr(module, "export_spans", fake_export)
    assert cli.main([*cli_trace, "--endpoint", "http://localhost:6006/v1/traces"]) == 0
    assert sent == ["http://localhost:6006/v1/traces"]
    assert "Отправлено спанов" in capsys.readouterr().out

    def refusing(spans: Sequence[ReadableSpan], endpoint: str) -> int:
        raise ExportError("нет связи")

    monkeypatch.setattr(module, "export_spans", refusing)
    assert cli.main([*cli_trace, "--endpoint", "http://x"]) == 2


def test_cli_unknown_ticket_and_auto_export_never_fails_a_run(
    cli_trace: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ario.cli as cli
    import ario.otel_export as module

    assert cli.main([*cli_trace[:-1], "99999", "--dry-run"]) == 2
    settings = Settings.load(Path(cli_trace[1]))
    settings = Settings(**{**settings.__dict__, "otlp_endpoint": "http://x"})

    def broken(spans: Sequence[ReadableSpan], endpoint: str) -> int:
        raise OSError("boom")

    monkeypatch.setattr(module, "export_spans", broken)
    capsys.readouterr()
    cli._auto_export(settings, "43702")  # не бросает
    assert "экспорт метрик не удался" in capsys.readouterr().err


def test_agent_commands_refuse_sdk_telemetry_but_reading_commands_work(
    cli_trace: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ario.cli as cli

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://corporate-agent:4317")
    env_file = cli_trace[:2]
    assert cli.main([*env_file, "run", "43702"]) == 2
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in capsys.readouterr().err
    assert cli.main([*env_file, "resume", "43702"]) == 2
    assert cli.main([*env_file, "eval"]) == 2
    capsys.readouterr()
    # чтение трассы и безопасный экспорт SDK не задействуют
    assert cli.main([*cli_trace, "--dry-run"]) == 0
    assert cli.main([*env_file, "trace", "43702"]) == 0


def test_restarted_ticket_with_new_trace_id_gets_separate_traces() -> None:
    second = [dict(e, trace_id="b" * 32) for e in events()]
    spans = build_spans(events() + second)
    assert {s.context.trace_id for s in spans} == {int(TRACE, 16), int("b" * 32, 16)}
    keys = [(s.context.trace_id, s.context.span_id) for s in spans]
    assert len(set(keys)) == len(keys)  # идентификаторы запусков не совпадают
    assert len([s for s in spans if s.name == "ticket 43702"]) == 2


def test_malformed_events_are_skipped_not_fatal() -> None:
    broken: list[Any] = [
        {"kind": "stage_started", "data": {}},  # без ts
        {"ts": "не дата", "kind": "stage_started", "data": {}},
        "не словарь",
        ev("stage_finished", 1.0, "analysis", seconds="много"),
    ]
    spans = build_spans(broken + events())
    assert any(s.name == "agent analyst" for s in spans)
    assert build_spans(broken[:3]) == []  # без единого годного события спанов нет


def test_tool_names_from_the_model_are_sanitized() -> None:
    evil = "x" * 200 + " " + SECRET
    evs = events()
    evs.insert(5, action(3.002, evil, 1000, 50, 1000))
    names = [s.name for s in build_spans(evs) if s.name.startswith("tool ")]
    tool_attrs = [(s.attributes or {}).get("tool.name") for s in build_spans(evs)]
    assert all(len(n) <= len("tool ") + 64 for n in names)
    assert not any(SECRET in str(v) for v in names + tool_attrs)


def test_calls_without_usage_are_split_by_time() -> None:
    evs = [
        ev("run_started", 0.0),
        ev("stage_started", 1.0, "analysis"),
        ev("agent_started", 1.1, "analysis", "analyst", model="m", max_steps=10),
        action(3.0, "read_file", 0, 0, 0),
        action(3.001, "search_text", 0, 0, 0),  # тот же ответ модели
        action(9.0, "read_file", 0, 0, 0),  # другой ответ
        ev("agent_finished", 10.0, "analysis", "analyst", steps=3),
    ]
    llm = [s for s in build_spans(evs) if s.name.startswith("llm call")]
    assert [(s.attributes or {})["ario.tool_calls"] for s in llm] == [2, 1]
