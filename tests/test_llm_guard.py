# ruff: noqa: F811, F401  (фикстура rx подключается импортом)
"""Жёсткий предел времени запроса к модели: зависший запрос не держит прогон бесконечно."""

from __future__ import annotations

import contextvars
import importlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from litellm.exceptions import Timeout

from ario.agents.backend import OpenHandsBackend
from ario.errors import LimitExceeded
from ario.guardrails import Deadline
from ario.llm_guard import guarded, hard_call_limit
from tests.test_openhands_backend import make_request, rx

FAR = 3600.0


def test_result_and_errors_pass_through() -> None:
    assert guarded(lambda x: x * 2, 5, lambda: FAR)(21) == 42
    boom = ValueError("как есть")

    def failing() -> None:
        raise boom

    with pytest.raises(ValueError) as info:
        guarded(failing, 5, lambda: FAR)()
    assert info.value is boom  # то же исключение, чтобы повторы SDK работали по типу


def test_a_hanging_request_is_cut_by_the_hard_limit() -> None:
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(Timeout, match="не завершился"):
        guarded(lambda **kw: release.wait(30), 0.2, lambda: FAR)(model="openrouter/x")
    assert time.monotonic() - started < 5
    release.set()  # брошенный поток доживает и не мешает


def test_context_variables_reach_the_request_thread() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("v", default="нет")
    var.set("сессия 43031")  # так в запрос попадают session.id и метки телеметрии
    assert guarded(lambda: var.get(), 5, lambda: FAR)() == "сессия 43031"


def test_expired_ticket_time_stops_before_the_request_is_sent() -> None:
    called: list[int] = []
    with pytest.raises(LimitExceeded, match="время на тикет"):
        guarded(lambda: called.append(1), 5, lambda: 0.0)()
    assert called == []


def test_ticket_time_running_out_during_the_wait_is_a_limit_not_a_model_failure() -> None:
    release = threading.Event()
    left = iter([0.3, -1.0, -1.0])  # на входе время есть, к концу ожидания его уже нет
    with pytest.raises(LimitExceeded, match="время на тикет"):
        guarded(lambda: release.wait(30), 5, lambda: next(left))()
    release.set()


def test_the_wait_is_bounded_by_the_remaining_ticket_time() -> None:
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(Timeout):
        guarded(
            lambda: release.wait(30), 100, lambda: 0.2 if time.monotonic() - started < 5 else 0
        )()
    assert time.monotonic() - started < 5
    release.set()


def test_the_guard_patches_the_sdk_reference_and_restores_it() -> None:
    module: Any = importlib.import_module("openhands.sdk.llm.llm")
    original = module.litellm_completion
    with hard_call_limit(5, Deadline(100)):
        assert module.litellm_completion is not original
    assert module.litellm_completion is original
    with pytest.raises(RuntimeError), hard_call_limit(5, Deadline(100)):
        raise RuntimeError  # и при ошибке внутри
    assert module.litellm_completion is original


def test_backend_does_not_hang_on_a_stuck_model_call(
    rx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", lambda **kw: release.wait(60))
    request = make_request(rx, tmp_path)
    request.call_timeout = 0.3
    backend = OpenHandsBackend("openai/stub-model", "sk-test", retry_wait=(0, 0))
    started = time.monotonic()
    with pytest.raises(LimitExceeded, match="Сбой модели"):
        backend.run(request)
    assert time.monotonic() - started < 20  # раньше такой запрос висел бесконечно
    release.set()
