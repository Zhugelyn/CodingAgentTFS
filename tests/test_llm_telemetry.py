# ruff: noqa: F811, F401  (фикстуры других модулей подключаются импортом)
"""Живая выгрузка запросов к модели: только локально, с масками, с привязкой к тикету."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ario import llm_telemetry
from ario.cli import main
from ario.llm_telemetry import (
    LlmTracingError,
    check_llm_content_settings,
    endpoint_is_local,
    llm_context,
)
from ario.trace import Masker
from tests.conftest import REPO_ROOT
from tests.test_openhands_backend import backend, make_request, rx

SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_instrumentation() -> Iterator[None]:
    yield
    llm_telemetry.shutdown()  # инструментация не должна протекать в другие тесты


@pytest.mark.parametrize(
    ("endpoint", "local"),
    [
        ("http://localhost:6006/v1/traces", True),
        ("http://127.0.0.1:6006/v1/traces", True),
        ("http://LOCALHOST:6006", True),
        ("https://phoenix.corp.example/v1/traces", False),
        ("http://192.168.1.5:6006/v1/traces", False),
        ("http://localhost.evil.example/v1/traces", False),
        ("не адрес", False),
    ],
)
def test_only_local_endpoints_are_local(endpoint: str, local: bool) -> None:
    assert endpoint_is_local(endpoint) is local


def test_settings_check_refuses_remote_and_missing_endpoint() -> None:
    check_llm_content_settings("http://localhost:6006/v1/traces", allow_remote=False)
    with pytest.raises(LlmTracingError, match="ARIO_OTLP_ENDPOINT"):
        check_llm_content_settings(None, allow_remote=False)
    with pytest.raises(LlmTracingError, match="локальным"):
        check_llm_content_settings("https://phoenix.corp.example/v1/traces", allow_remote=False)
    check_llm_content_settings("https://phoenix.corp.example/v1/traces", allow_remote=True)


def messages_dump(exporter: InMemorySpanExporter) -> str:
    return json.dumps(
        [dict(span.attributes or {}) for span in exporter.get_finished_spans()],
        ensure_ascii=False,
        default=str,
    )


def test_llm_call_becomes_a_span_with_text_session_and_masked_secrets() -> None:
    import litellm

    exporter = InMemorySpanExporter()
    llm_telemetry.enable(
        "http://localhost:6006/v1/traces", Masker([SECRET]), exporter=exporter, batch=False
    )
    assert llm_telemetry.is_active()
    with llm_context("43702", "analysis", "analyst"):
        litellm.completion(
            model="openai/stub",
            messages=[{"role": "user", "content": f"Прочитай файл. Ключ {SECRET}"}],
            mock_response="Готово",
        )
    (span,) = exporter.get_finished_spans()
    dump = messages_dump(exporter)
    assert "Прочитай файл" in dump and "Готово" in dump  # текст запроса и ответа на месте
    assert SECRET not in dump and "***" in dump  # секрет замаскирован
    attrs = dict(span.attributes or {})
    assert attrs["session.id"] == "43702"
    assert attrs["openinference.span.kind"] == "LLM"
    assert "analysis" in str(attrs.get("tag.tags")) and "analyst" in dump
    assert (span.resource.attributes or {})[
        "openinference.project.name"
    ] == llm_telemetry.LLM_PROJECT


def test_calls_outside_the_context_have_no_ticket_session() -> None:
    import litellm

    exporter = InMemorySpanExporter()
    llm_telemetry.enable("http://localhost:6006", Masker([]), exporter=exporter, batch=False)
    litellm.completion(
        model="openai/stub", messages=[{"role": "user", "content": "x"}], mock_response="y"
    )
    (span,) = exporter.get_finished_spans()
    assert "session.id" not in dict(span.attributes or {})


def test_sdk_reference_is_rebound_and_restored() -> None:
    import litellm

    sdk: Any = importlib.import_module("openhands.sdk.llm.llm")
    original = litellm.completion
    assert sdk.litellm_completion is original
    llm_telemetry.enable(
        "http://localhost:6006", Masker([]), exporter=InMemorySpanExporter(), batch=False
    )
    assert litellm.completion is not original
    assert sdk.litellm_completion is litellm.completion  # SDK вызывает уже обёртку
    llm_telemetry.shutdown()
    assert litellm.completion is original and sdk.litellm_completion is original
    assert not llm_telemetry.is_active()


def test_context_is_a_noop_when_tracing_is_off() -> None:
    assert not llm_telemetry.is_active()
    with llm_context("1", "analysis", "analyst"):
        pass  # ничего не требует и ничего не делает


def test_enable_twice_does_not_double_wrap() -> None:
    import litellm

    exporter = InMemorySpanExporter()
    llm_telemetry.enable("http://localhost:6006", Masker([]), exporter=exporter, batch=False)
    wrapped = litellm.completion
    llm_telemetry.enable("http://localhost:6006", Masker([]), exporter=exporter, batch=False)
    assert litellm.completion is wrapped


# --- команда ----------------------------------------------------------------------------------
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **values: str) -> list[str]:
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARIO_GUARDRAILS", str(REPO_ROOT / "config" / "guardrails.yaml"))
    for name in ("ARIO_OTLP_ENDPOINT", "ARIO_OTLP_LLM_CONTENT", "ARIO_ALLOW_REMOTE_LLM_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return ["--env-file", str(empty)]


def test_cli_refuses_content_tracing_without_local_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = env(tmp_path, monkeypatch, ARIO_OTLP_LLM_CONTENT="1")
    assert main([*base, "run", "1"]) == 2
    assert "ARIO_OTLP_ENDPOINT" in capsys.readouterr().err
    base = env(
        tmp_path,
        monkeypatch,
        ARIO_OTLP_LLM_CONTENT="1",
        ARIO_OTLP_ENDPOINT="https://phoenix.corp.example/v1/traces",
    )
    assert main([*base, "resume", "1"]) == 2
    assert "локальным" in capsys.readouterr().err
    assert not llm_telemetry.is_active()


def test_cli_reading_commands_do_not_need_content_tracing_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = env(tmp_path, monkeypatch, ARIO_OTLP_LLM_CONTENT="1")  # без endpoint, но команда читает
    assert main([*base, "trace", "99"]) == 2  # нет трассы: обычная ошибка, а не отказ настройки
    assert "нет трассы" in capsys.readouterr().err


# --- исправления по ревью -----------------------------------------------------------------------
def test_masking_covers_events_and_lists_of_strings() -> None:
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import SpanContext, TraceFlags

    inner = InMemorySpanExporter()
    exporter = llm_telemetry.MaskingExporter(inner, Masker([SECRET]))
    span = ReadableSpan(
        name="completion",
        context=SpanContext(1, 2, False, TraceFlags(TraceFlags.SAMPLED)),
        attributes={"tag.tags": ("analysis", f"токен {SECRET}"), "n": 5},
        events=[Event("exception", {"exception.message": f"401 for {SECRET}"}, timestamp=1)],
        start_time=1,
        end_time=2,
    )
    exporter.export([span])
    (out,) = inner.get_finished_spans()
    dump = json.dumps(
        [dict(out.attributes or {}), [dict(e.attributes or {}) for e in out.events]],
        ensure_ascii=False,
        default=str,
    )
    assert SECRET not in dump and "***" in dump
    assert dict(out.attributes or {})["n"] == 5  # числа остаются как есть


def test_enable_fails_loudly_when_instrumentation_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openinference.instrumentation.litellm import LiteLLMInstrumentor

    monkeypatch.setattr(LiteLLMInstrumentor, "instrument", lambda self, **kwargs: None)
    monkeypatch.setattr(
        LiteLLMInstrumentor, "is_instrumented_by_opentelemetry", property(lambda self: False)
    )
    with pytest.raises(LlmTracingError, match="не включилась"):
        llm_telemetry.enable("http://localhost:6006", Masker([]), exporter=InMemorySpanExporter())
    assert not llm_telemetry.is_active()

    def boom(self: Any, **kwargs: Any) -> None:
        raise RuntimeError("секрет внутри " + SECRET)

    monkeypatch.setattr(LiteLLMInstrumentor, "instrument", boom)
    with pytest.raises(LlmTracingError) as info:
        llm_telemetry.enable("http://localhost:6006", Masker([]), exporter=InMemorySpanExporter())
    assert SECRET not in str(info.value) and not llm_telemetry.is_active()


def test_sdk_imported_after_a_restart_still_gets_the_original_back() -> None:
    import litellm

    sdk: Any = importlib.import_module("openhands.sdk.llm.llm")
    original = litellm.completion
    for _ in range(2):  # включение и выключение подряд не накапливают обёртки
        llm_telemetry.enable(
            "http://localhost:6006", Masker([]), exporter=InMemorySpanExporter(), batch=False
        )
        assert sdk.litellm_completion is litellm.completion
        llm_telemetry.shutdown()
        assert sdk.litellm_completion is original and litellm.completion is original


def test_unreachable_receiver_is_reported_at_start() -> None:
    import socket

    with socket.socket() as probe:  # свободный порт, на котором никто не слушает
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(LlmTracingError, match="не отвечает"):
        llm_telemetry.enable(f"http://127.0.0.1:{port}/v1/traces", Masker([]))
    assert not llm_telemetry.is_active()


def test_backend_runs_the_agent_inside_the_ticket_context(
    rx: Path, tmp_path: Path, script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    import ario.agents.backend as backend_module
    from tests.llm_stub import finish, response

    seen: list[tuple[str, str, str]] = []

    def recording(ticket: str, stage: str, role: str) -> contextlib.AbstractContextManager[None]:
        seen.append((ticket, stage, role))
        return contextlib.nullcontext()

    monkeypatch.setattr(backend_module, "llm_context", recording)
    script(response("read_file", {"path": "src/A.cs"}), finish("ок"))
    backend().run(make_request(rx, tmp_path))
    assert seen == [("1", "analysis", "analyst")]


def test_bad_guardrails_path_is_a_setting_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = env(
        tmp_path,
        monkeypatch,
        ARIO_OTLP_LLM_CONTENT="1",
        ARIO_OTLP_ENDPOINT="http://localhost:6006/v1/traces",
        ARIO_GUARDRAILS=str(tmp_path / "нет.yaml"),
    )
    assert main([*base, "run", "1"]) == 2
    assert "Ошибка настройки" in capsys.readouterr().err
