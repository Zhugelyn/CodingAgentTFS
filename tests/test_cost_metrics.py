# ruff: noqa: F811, F401  (фикстуры других модулей подключаются импортом)
"""Цена прогона по данным провайдера: запрос, трасса, `ario trace`, evals и спаны."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ario.agents.backend import OpenHandsBackend
from ario.evals import CaseResult, render_report
from ario.otel_export import build_spans
from ario.trace_report import render_summary, summarize
from tests.llm_stub import finish, response
from tests.test_openhands_backend import make_request, rx  # noqa: F401


def ev(kind: str, t: float, stage: str | None = None, agent: str | None = None, **data: Any) -> Any:
    return {
        "ts": f"2026-10-08T10:00:{t:06.3f}+00:00",
        "trace_id": "a" * 32,
        "ticket": "1",
        "stage": stage,
        "agent": agent,
        "kind": kind,
        "data": data,
    }


def events() -> list[Any]:
    return [
        ev("run_started", 0.0),
        ev("stage_started", 1.0, "analysis"),
        ev("agent_started", 1.1, "analysis", "analyst", model="openrouter/m", max_steps=10),
        ev(
            "agent_action",
            2.0,
            "analysis",
            "analyst",
            tool="read_file",
            context_tokens=1000,
            prompt_total=1000,
            completion_total=50,
            cost_total=0.0010,
        ),
        ev("agent_observation", 2.5, "analysis", "analyst", tool="read_file"),
        ev(
            "agent_action",
            4.0,
            "analysis",
            "analyst",
            tool="finish",
            context_tokens=2000,
            prompt_total=3000,
            completion_total=90,
            cost_total=0.0035,
        ),
        ev(
            "agent_finished",
            5.0,
            "analysis",
            "analyst",
            steps=2,
            prompt_tokens=3000,
            completion_tokens=90,
            cache_read_tokens=1000,
            cost_usd=0.0035,
        ),
        ev("stage_finished", 5.1, "analysis", seconds=4.1),
        ev("run_finished", 6.0, outcome="done", total_seconds=6.0),
    ]


def test_ario_trace_shows_cost_per_stage_and_total() -> None:
    text = render_summary(summarize(events()))
    assert "цена $0.0035" in text
    assert "Цена по данным провайдера (все попытки): $0.0035" in text


def test_ario_trace_has_no_cost_line_when_provider_gave_none() -> None:
    old = [e for e in events()]
    for e in old:
        e["data"].pop("cost_usd", None)
    assert "Цена по данным провайдера" not in render_summary(summarize(old))


def test_spans_carry_cost_per_call_and_per_agent() -> None:
    spans = {s.name: dict(s.attributes or {}) for s in build_spans(events())}
    assert spans["agent analyst"]["ario.usage.cost_usd"] == 0.0035
    assert spans["llm call 1"]["llm.cost.total"] == 0.001
    assert spans["llm call 2"]["llm.cost.total"] == 0.0025  # разность накопленных значений


def test_eval_report_shows_cost() -> None:
    text = render_report(
        [
            CaseResult("1", "done", seconds=60, steps=5, prompt_tokens=2000, cost_usd=0.0123),
            CaseResult("2", "needs-human", seconds=30, steps=2, prompt_tokens=500, cost_usd=0.0007),
        ]
    )
    assert "$0.0123" in text and "$0.0007" in text
    assert "Цена по данным провайдера: $0.0130" in text


def test_openrouter_models_ask_for_real_price(
    rx: Path,
    tmp_path: Path,
    script: Callable[..., list[dict[str, Any]]],  # noqa: F811
) -> None:
    calls = script(response("read_file", {"path": "src/A.cs"}), finish("ок"))
    OpenHandsBackend("openrouter/stub-model", "sk-test", retry_wait=(0, 0)).run(
        make_request(rx, tmp_path)
    )
    assert calls[0].get("extra_body") == {"usage": {"include": True}}


def test_other_providers_do_not_get_openrouter_extras(
    rx: Path,
    tmp_path: Path,
    script: Callable[..., list[dict[str, Any]]],  # noqa: F811
) -> None:
    calls = script(response("read_file", {"path": "src/A.cs"}), finish("ок"))
    OpenHandsBackend("openai/stub-model", "sk-test", retry_wait=(0, 0)).run(
        make_request(rx, tmp_path)
    )
    assert not calls[0].get("extra_body")
